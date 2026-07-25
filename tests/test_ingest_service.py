"""Tests for ingest_service.py: eligibility, verification, destination
naming, collision analysis, and dry-run plan generation end-to-end."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mams.config import AppConfig
from mams.db import connect, migrate
from mams.ingest_repository import PlanNotApprovableError, get_current_plan_for_media_file
from mams.ingest_service import IngestPlanError, approve, generate_plan
from mams.resolution_repository import assign_identity, upsert_external_identity

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"


@pytest.fixture()
def connection(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "mams.db"
    migrate(db_path, REPO_MIGRATIONS_DIR)
    conn = connect(db_path)
    yield conn
    conn.close()


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        raw={
            "ingest": {
                "incoming_roots": [str(tmp_path / "Incoming")],
                "movie_destination_category": "movies",
                "tv_destination_category": "tv",
                "kids_movie_destination_category": "kids_movies",
                "kids_tv_destination_category": "kids_shows",
            },
            "nas": {
                "categories": {
                    "movies": str(tmp_path / "NAS" / "Movies"),
                    "kids_movies": str(tmp_path / "NAS" / "KidsMovies"),
                    "tv": str(tmp_path / "NAS" / "TV"),
                    "kids_shows": str(tmp_path / "NAS" / "KidsShows"),
                }
            },
        }
    )


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _seed_media_file(
    connection: sqlite3.Connection,
    tmp_path: Path,
    *,
    name: str = "Alien.mkv",
    state: str = "ACTIVE",
    duration_seconds: float | None = 7020.0,
    video_tracks: int = 1,
    audio_tracks: int = 1,
    media_info_error: str | None = None,
    probed: bool = True,
) -> int:
    row = connection.execute("SELECT id FROM libraries WHERE category = 'incoming'").fetchone()
    if row is not None:
        library_id = row["id"]
    else:
        library_id = _lastrowid(
            connection.execute(
                "INSERT INTO libraries (category, root_path) VALUES ('incoming', ?)", (str(tmp_path / "Incoming"),)
            )
        )
    scan_id = _lastrowid(connection.execute("INSERT INTO scan_runs DEFAULT VALUES"))
    incoming_file = tmp_path / "Incoming" / name
    incoming_file.parent.mkdir(parents=True, exist_ok=True)
    incoming_file.write_bytes(b"\0" * 1024)
    absolute_path = str(incoming_file)
    media_file_id = _lastrowid(
        connection.execute(
            """
            INSERT INTO media_files (
                library_id, absolute_path, relative_path, filename, extension, parent_directory, layout,
                size_bytes, state, duration_seconds, media_info_error, media_info_probed_at, container,
                first_seen_scan_id, last_seen_scan_id
            ) VALUES (?, ?, ?, ?, ?, ?, 'unknown', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                library_id, absolute_path, name, name, ".mkv", str(tmp_path / "Incoming"),
                1_000_000, state, duration_seconds, media_info_error,
                "2024-01-01T00:00:00" if probed else None, "Matroska", scan_id, scan_id,
            ),
        )
    )
    for _ in range(video_tracks):
        connection.execute(
            "INSERT INTO video_tracks (media_file_id, track_index) VALUES (?, 0)", (media_file_id,)
        )
    for _ in range(audio_tracks):
        connection.execute(
            "INSERT INTO audio_tracks (media_file_id, track_index) VALUES (?, 0)", (media_file_id,)
        )
    return media_file_id


def _seed_candidate(connection: sqlite3.Connection, *, media_file_id: int, candidate_type: str = "MOVIE") -> int:
    return _lastrowid(
        connection.execute(
            "INSERT INTO identification_candidates (media_file_id, candidate_type, parsed_title, confidence, parser_version) "
            "VALUES (?, ?, 'Alien', 'HIGH', 1)",
            (media_file_id, candidate_type),
        )
    )


def _resolve_movie_identity(connection: sqlite3.Connection, *, media_file_id: int, candidate_id: int, method: str = "AUTO") -> int:
    identity = upsert_external_identity(connection, media_type="MOVIE", provider_id=348, title="Alien", release_year=1979)
    assignment = assign_identity(
        connection, media_file_id=media_file_id, identification_candidate_id=candidate_id,
        external_identity_id=identity.id, resolution_attempt_id=None, assignment_method=method, confidence="HIGH",
    )
    return assignment.id


def test_generate_plan_ready_for_review(connection: sqlite3.Connection, tmp_path: Path) -> None:
    media_file_id = _seed_media_file(connection, tmp_path)
    candidate_id = _seed_candidate(connection, media_file_id=media_file_id)
    _resolve_movie_identity(connection, media_file_id=media_file_id, candidate_id=candidate_id)
    config = _config(tmp_path)

    plan = generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")

    assert plan.status == "READY_FOR_REVIEW"
    assert plan.destination_filename == "Alien (1979).mkv"
    assert plan.destination_directory == str(tmp_path / "NAS" / "Movies" / "Alien (1979)")
    action_types = [a.action_type for a in plan.actions]
    assert action_types == ["VALIDATE_SOURCE", "VERIFY_MEDIA", "CREATE_DIRECTORY", "MOVE", "REFRESH_INVENTORY", "REQUEST_PLEX_REFRESH"]
    assert all(a.details == {"execution_state": "PROPOSED_NOT_EXECUTED"} for a in plan.actions)


def test_generate_plan_blocked_without_identity(connection: sqlite3.Connection, tmp_path: Path) -> None:
    media_file_id = _seed_media_file(connection, tmp_path)
    _seed_candidate(connection, media_file_id=media_file_id)
    config = _config(tmp_path)

    plan = generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")

    assert plan.status == "BLOCKED"
    assert any("resolved ACTIVE external identity" in r for r in plan.blocking_reasons)


def test_generate_plan_blocked_when_source_missing(connection: sqlite3.Connection, tmp_path: Path) -> None:
    media_file_id = _seed_media_file(connection, tmp_path, state="MISSING")
    candidate_id = _seed_candidate(connection, media_file_id=media_file_id)
    _resolve_movie_identity(connection, media_file_id=media_file_id, candidate_id=candidate_id)
    config = _config(tmp_path)

    plan = generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")

    assert plan.status == "BLOCKED"


def test_generate_plan_blocked_when_verification_fails(connection: sqlite3.Connection, tmp_path: Path) -> None:
    media_file_id = _seed_media_file(connection, tmp_path, video_tracks=0)
    candidate_id = _seed_candidate(connection, media_file_id=media_file_id)
    _resolve_movie_identity(connection, media_file_id=media_file_id, candidate_id=candidate_id)
    config = _config(tmp_path)

    plan = generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")

    assert plan.status == "BLOCKED"
    assert plan.verification_status == "FAIL"


def test_generate_plan_review_required_without_destination_category(connection: sqlite3.Connection, tmp_path: Path) -> None:
    media_file_id = _seed_media_file(connection, tmp_path)
    candidate_id = _seed_candidate(connection, media_file_id=media_file_id)
    _resolve_movie_identity(connection, media_file_id=media_file_id, candidate_id=candidate_id)
    config = _config(tmp_path)

    plan = generate_plan(connection, config, media_file_id=media_file_id, destination_category=None)

    assert plan.status == "REVIEW_REQUIRED"
    assert any("destination category not specified" in r for r in plan.blocking_reasons)


def test_generate_plan_review_required_for_manual_assignment(connection: sqlite3.Connection, tmp_path: Path) -> None:
    media_file_id = _seed_media_file(connection, tmp_path)
    candidate_id = _seed_candidate(connection, media_file_id=media_file_id)
    _resolve_movie_identity(connection, media_file_id=media_file_id, candidate_id=candidate_id, method="MANUAL")
    config = _config(tmp_path)

    plan = generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")

    assert plan.status == "REVIEW_REQUIRED"


def test_generate_plan_review_required_on_verification_warning(connection: sqlite3.Connection, tmp_path: Path) -> None:
    media_file_id = _seed_media_file(connection, tmp_path, audio_tracks=0)
    candidate_id = _seed_candidate(connection, media_file_id=media_file_id)
    _resolve_movie_identity(connection, media_file_id=media_file_id, candidate_id=candidate_id)
    config = _config(tmp_path)

    plan = generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")

    assert plan.status == "REVIEW_REQUIRED"
    assert plan.verification_status == "WARNING"


def test_generate_plan_rejects_mismatched_destination_category(connection: sqlite3.Connection, tmp_path: Path) -> None:
    media_file_id = _seed_media_file(connection, tmp_path)
    candidate_id = _seed_candidate(connection, media_file_id=media_file_id)
    _resolve_movie_identity(connection, media_file_id=media_file_id, candidate_id=candidate_id)
    config = _config(tmp_path)

    plan = generate_plan(connection, config, media_file_id=media_file_id, destination_category="tv")

    assert plan.status == "BLOCKED"
    assert any("not valid for a MOVIE identity" in r for r in plan.blocking_reasons)


def test_generate_plan_raises_for_unknown_media_file(connection: sqlite3.Connection, tmp_path: Path) -> None:
    config = _config(tmp_path)
    with pytest.raises(IngestPlanError):
        generate_plan(connection, config, media_file_id=999999, destination_category="movie")


def test_generate_plan_raises_for_file_outside_incoming_root(connection: sqlite3.Connection, tmp_path: Path) -> None:
    library_id = _lastrowid(
        connection.execute("INSERT INTO libraries (category, root_path) VALUES ('movies', ?)", (str(tmp_path / "NAS" / "Movies"),))
    )
    scan_id = _lastrowid(connection.execute("INSERT INTO scan_runs DEFAULT VALUES"))
    media_file_id = _lastrowid(
        connection.execute(
            """
            INSERT INTO media_files (
                library_id, absolute_path, relative_path, filename, extension, parent_directory, layout,
                size_bytes, first_seen_scan_id, last_seen_scan_id
            ) VALUES (?, ?, ?, ?, ?, ?, 'movie_flat', ?, ?, ?)
            """,
            (library_id, str(tmp_path / "NAS" / "Movies" / "Alien (1979).mkv"), "Alien (1979).mkv", "Alien (1979).mkv", ".mkv", str(tmp_path / "NAS" / "Movies"), 1000, scan_id, scan_id),
        )
    )
    config = _config(tmp_path)
    with pytest.raises(IngestPlanError):
        generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")


def test_generate_plan_raises_for_invalid_destination_category(connection: sqlite3.Connection, tmp_path: Path) -> None:
    media_file_id = _seed_media_file(connection, tmp_path)
    config = _config(tmp_path)
    with pytest.raises(IngestPlanError):
        generate_plan(connection, config, media_file_id=media_file_id, destination_category="nonsense")


# --- collision analysis ---------------------------------------------------------


def test_collision_destination_already_exists_on_disk(connection: sqlite3.Connection, tmp_path: Path) -> None:
    media_file_id = _seed_media_file(connection, tmp_path)
    candidate_id = _seed_candidate(connection, media_file_id=media_file_id)
    _resolve_movie_identity(connection, media_file_id=media_file_id, candidate_id=candidate_id)
    config = _config(tmp_path)
    destination_dir = tmp_path / "NAS" / "Movies" / "Alien (1979)"
    destination_dir.mkdir(parents=True)
    (destination_dir / "Alien (1979).mkv").write_bytes(b"\0")

    plan = generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")

    assert plan.status == "BLOCKED"
    assert any("already exists on disk" in r for r in plan.blocking_reasons)


def test_collision_destination_already_in_canonical_inventory(connection: sqlite3.Connection, tmp_path: Path) -> None:
    media_file_id = _seed_media_file(connection, tmp_path)
    candidate_id = _seed_candidate(connection, media_file_id=media_file_id)
    _resolve_movie_identity(connection, media_file_id=media_file_id, candidate_id=candidate_id)
    config = _config(tmp_path)

    other_library_id = _lastrowid(
        connection.execute("INSERT INTO libraries (category, root_path) VALUES ('movies', ?)", (str(tmp_path / "NAS" / "Movies"),))
    )
    scan_id = _lastrowid(connection.execute("INSERT INTO scan_runs DEFAULT VALUES"))
    destination_path = str(tmp_path / "NAS" / "Movies" / "Alien (1979)" / "Alien (1979).mkv")
    connection.execute(
        """
        INSERT INTO media_files (
            library_id, absolute_path, relative_path, filename, extension, parent_directory, layout,
            size_bytes, first_seen_scan_id, last_seen_scan_id
        ) VALUES (?, ?, 'Alien (1979).mkv', 'Alien (1979).mkv', '.mkv', ?, 'movie_folder', 1000, ?, ?)
        """,
        (other_library_id, destination_path, str(tmp_path / "NAS" / "Movies" / "Alien (1979)"), scan_id, scan_id),
    )

    plan = generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")

    assert plan.status == "BLOCKED"
    assert any("canonical inventory" in r for r in plan.blocking_reasons)


def test_collision_duplicate_active_plan_targets_same_destination(connection: sqlite3.Connection, tmp_path: Path) -> None:
    config = _config(tmp_path)
    media_file_id_1 = _seed_media_file(connection, tmp_path, name="Alien.mkv")
    candidate_id_1 = _seed_candidate(connection, media_file_id=media_file_id_1)
    _resolve_movie_identity(connection, media_file_id=media_file_id_1, candidate_id=candidate_id_1)
    plan_1 = generate_plan(connection, config, media_file_id=media_file_id_1, destination_category="movie")
    assert plan_1.status == "READY_FOR_REVIEW"

    media_file_id_2 = _seed_media_file(connection, tmp_path, name="AlienDup.mkv")
    candidate_id_2 = _seed_candidate(connection, media_file_id=media_file_id_2)
    identity = upsert_external_identity(connection, media_type="MOVIE", provider_id=348, title="Alien", release_year=1979)
    assign_identity(
        connection, media_file_id=media_file_id_2, identification_candidate_id=candidate_id_2,
        external_identity_id=identity.id, resolution_attempt_id=None, assignment_method="AUTO", confidence="HIGH",
    )

    plan_2 = generate_plan(connection, config, media_file_id=media_file_id_2, destination_category="movie")

    assert plan_2.status == "BLOCKED"
    assert any("another active plan" in r for r in plan_2.blocking_reasons)


# --- determinism / regeneration ------------------------------------------------


def test_regeneration_against_unchanged_inputs_does_not_duplicate(connection: sqlite3.Connection, tmp_path: Path) -> None:
    media_file_id = _seed_media_file(connection, tmp_path)
    candidate_id = _seed_candidate(connection, media_file_id=media_file_id)
    _resolve_movie_identity(connection, media_file_id=media_file_id, candidate_id=candidate_id)
    config = _config(tmp_path)

    first = generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")
    second = generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")

    assert first.id == second.id
    assert first.updated_at == second.updated_at
    count = connection.execute("SELECT COUNT(*) FROM ingest_plans").fetchone()[0]
    assert count == 1


def test_regeneration_after_identity_change_updates_plan(connection: sqlite3.Connection, tmp_path: Path) -> None:
    media_file_id = _seed_media_file(connection, tmp_path)
    candidate_id = _seed_candidate(connection, media_file_id=media_file_id)
    _resolve_movie_identity(connection, media_file_id=media_file_id, candidate_id=candidate_id)
    config = _config(tmp_path)
    first = generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")

    other_identity = upsert_external_identity(connection, media_type="MOVIE", provider_id=679, title="Aliens", release_year=1986)
    assign_identity(
        connection, media_file_id=media_file_id, identification_candidate_id=candidate_id,
        external_identity_id=other_identity.id, resolution_attempt_id=None, assignment_method="MANUAL", confidence="MEDIUM",
    )
    second = generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")

    assert second.id == first.id
    assert second.plan_version == first.plan_version + 1
    assert second.destination_filename == "Aliens (1986).mkv"
    count = connection.execute("SELECT COUNT(*) FROM ingest_plans").fetchone()[0]
    assert count == 1


# --- approval ------------------------------------------------------------------


def test_approve_ready_for_review_plan(connection: sqlite3.Connection, tmp_path: Path) -> None:
    media_file_id = _seed_media_file(connection, tmp_path)
    candidate_id = _seed_candidate(connection, media_file_id=media_file_id)
    _resolve_movie_identity(connection, media_file_id=media_file_id, candidate_id=candidate_id)
    config = _config(tmp_path)
    plan = generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")

    approved = approve(connection, plan.id)

    assert approved.status == "APPROVED"
    assert approved.approved_by == "MANUAL_CLI"


def test_approve_rejects_blocked_plan(connection: sqlite3.Connection, tmp_path: Path) -> None:
    media_file_id = _seed_media_file(connection, tmp_path)
    _seed_candidate(connection, media_file_id=media_file_id)
    config = _config(tmp_path)
    plan = generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")
    assert plan.status == "BLOCKED"

    with pytest.raises(PlanNotApprovableError):
        approve(connection, plan.id)


def test_no_filesystem_writes_occur_during_plan_generation(connection: sqlite3.Connection, tmp_path: Path) -> None:
    media_file_id = _seed_media_file(connection, tmp_path)
    candidate_id = _seed_candidate(connection, media_file_id=media_file_id)
    _resolve_movie_identity(connection, media_file_id=media_file_id, candidate_id=candidate_id)
    config = _config(tmp_path)

    generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")

    assert not (tmp_path / "NAS" / "Movies").exists()
    assert (tmp_path / "Incoming" / "Alien.mkv").exists()  # source untouched, still present


def test_current_plan_lookup_reflects_generated_plan(connection: sqlite3.Connection, tmp_path: Path) -> None:
    media_file_id = _seed_media_file(connection, tmp_path)
    candidate_id = _seed_candidate(connection, media_file_id=media_file_id)
    _resolve_movie_identity(connection, media_file_id=media_file_id, candidate_id=candidate_id)
    config = _config(tmp_path)
    plan = generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")

    current = get_current_plan_for_media_file(connection, media_file_id)
    assert current is not None
    assert current.id == plan.id
