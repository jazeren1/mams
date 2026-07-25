"""Tests for the resolution schema (database/migrations/0008_resolution.sql):
resolution_attempts, resolution_matches, media_identity_assignments.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mams.db import connect, current_schema_version, migrate

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"


def _migrations_dir_through(tmp_path: Path, *versions: str) -> Path:
    scoped = tmp_path / "scoped_migrations"
    scoped.mkdir(exist_ok=True)
    for version in versions:
        matches = list(REPO_MIGRATIONS_DIR.glob(f"{version}_*.sql"))
        assert len(matches) == 1, f"expected exactly one migration file for {version}"
        (scoped / matches[0].name).write_text(matches[0].read_text(encoding="utf-8"), encoding="utf-8")
    return scoped


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "mams.db"
    migrate(
        path,
        _migrations_dir_through(tmp_path, "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008"),
    )
    return path


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _insert_library(connection: sqlite3.Connection, category: str = "movies") -> int:
    cursor = connection.execute(
        "INSERT INTO libraries (category, root_path) VALUES (?, ?)", (category, f"/Volumes/{category}")
    )
    return _lastrowid(cursor)


def _insert_scan_run(connection: sqlite3.Connection) -> int:
    cursor = connection.execute("INSERT INTO scan_runs DEFAULT VALUES")
    return _lastrowid(cursor)


def _insert_media_file(
    connection: sqlite3.Connection, *, library_id: int, scan_id: int, name: str = "Alien (1979).mkv"
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO media_files (
            library_id, absolute_path, relative_path, filename, extension,
            parent_directory, layout, size_bytes, first_seen_scan_id, last_seen_scan_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (library_id, f"/Volumes/movies/{name}", name, name, ".mkv", "/Volumes/movies", "movie_flat", 1234, scan_id, scan_id),
    )
    return _lastrowid(cursor)


def _insert_candidate(connection: sqlite3.Connection, *, media_file_id: int) -> int:
    cursor = connection.execute(
        """
        INSERT INTO identification_candidates (media_file_id, candidate_type, parsed_title, confidence, parser_version)
        VALUES (?, 'MOVIE', 'Alien', 'HIGH', 1)
        """,
        (media_file_id,),
    )
    return _lastrowid(cursor)


def _insert_external_identity(connection: sqlite3.Connection, *, provider_id: int = 348) -> int:
    cursor = connection.execute(
        "INSERT INTO external_identities (provider, media_type, provider_id, title, release_year) "
        "VALUES ('TMDB', 'MOVIE', ?, 'Alien', 1979)",
        (provider_id,),
    )
    return _lastrowid(cursor)


def _insert_attempt(
    connection: sqlite3.Connection,
    *,
    identification_candidate_id: int | None,
    media_file_id: int | None,
    status: str = "PENDING",
    algorithm_version: int = 1,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO resolution_attempts (
            identification_candidate_id, media_file_id, provider, status, algorithm_version
        ) VALUES (?, ?, 'TMDB', ?, ?)
        """,
        (identification_candidate_id, media_file_id, status, algorithm_version),
    )
    return _lastrowid(cursor)


def _insert_match(
    connection: sqlite3.Connection,
    *,
    resolution_attempt_id: int,
    provider_media_type: str = "MOVIE",
    provider_id: int = 348,
    score: float = 0.95,
    rank: int = 1,
    selected: int = 0,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO resolution_matches (
            resolution_attempt_id, provider, provider_media_type, provider_id, title, score, rank,
            scoring_json, selected
        ) VALUES (?, 'TMDB', ?, ?, 'Alien', ?, ?, '{}', ?)
        """,
        (resolution_attempt_id, provider_media_type, provider_id, score, rank, selected),
    )
    return _lastrowid(cursor)


def _insert_assignment(
    connection: sqlite3.Connection,
    *,
    media_file_id: int,
    external_identity_id: int,
    assignment_method: str = "AUTO",
    confidence: str = "HIGH",
    status: str = "ACTIVE",
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO media_identity_assignments (
            media_file_id, external_identity_id, assignment_method, confidence, status
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (media_file_id, external_identity_id, assignment_method, confidence, status),
    )
    return _lastrowid(cursor)


def test_migrate_applies_resolution_schema(db_path: Path) -> None:
    with connect(db_path) as connection:
        assert current_schema_version(connection) == 8
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"resolution_attempts", "resolution_matches", "media_identity_assignments"} <= tables


# --- resolution_attempts -----------------------------------------------------


@pytest.mark.parametrize(
    "status", ["PENDING", "RESOLVED", "REVIEW_REQUIRED", "NO_MATCH", "FAILED", "SKIPPED"]
)
def test_attempt_accepts_all_valid_statuses(db_path: Path, status: str) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        candidate_id = _insert_candidate(connection, media_file_id=media_file_id)
        attempt_id = _insert_attempt(
            connection, identification_candidate_id=candidate_id, media_file_id=media_file_id, status=status
        )
        row = connection.execute("SELECT status FROM resolution_attempts WHERE id = ?", (attempt_id,)).fetchone()
        assert row["status"] == status


def test_attempt_rejects_invalid_status(db_path: Path) -> None:
    with connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_attempt(connection, identification_candidate_id=None, media_file_id=None, status="MAYBE")


def test_attempt_requires_provider_status_algorithm_version(db_path: Path) -> None:
    with connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO resolution_attempts (status, algorithm_version) VALUES ('PENDING', 1)")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO resolution_attempts (provider, algorithm_version) VALUES ('TMDB', 1)")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO resolution_attempts (provider, status) VALUES ('TMDB', 'PENDING')")


def test_multiple_attempts_allowed_for_same_candidate(db_path: Path) -> None:
    """resolution_attempts is append-only history, not reconciled in place
    like identification_candidates -- re-evaluation creates a new row."""
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        candidate_id = _insert_candidate(connection, media_file_id=media_file_id)
        _insert_attempt(connection, identification_candidate_id=candidate_id, media_file_id=media_file_id)
        _insert_attempt(connection, identification_candidate_id=candidate_id, media_file_id=media_file_id)
        count = connection.execute("SELECT COUNT(*) FROM resolution_attempts").fetchone()[0]
        assert count == 2


def test_deleting_candidate_sets_attempt_candidate_id_null_but_preserves_row(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        candidate_id = _insert_candidate(connection, media_file_id=media_file_id)
        attempt_id = _insert_attempt(
            connection, identification_candidate_id=candidate_id, media_file_id=media_file_id
        )

        connection.execute("DELETE FROM identification_candidates WHERE id = ?", (candidate_id,))

        row = connection.execute("SELECT * FROM resolution_attempts WHERE id = ?", (attempt_id,)).fetchone()
        assert row is not None
        assert row["identification_candidate_id"] is None


def test_deleting_media_file_sets_attempt_media_file_id_null_but_preserves_row(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        attempt_id = _insert_attempt(connection, identification_candidate_id=None, media_file_id=media_file_id)

        connection.execute("DELETE FROM media_files WHERE id = ?", (media_file_id,))

        row = connection.execute("SELECT * FROM resolution_attempts WHERE id = ?", (attempt_id,)).fetchone()
        assert row is not None
        assert row["media_file_id"] is None


# --- resolution_matches -------------------------------------------------------


def test_match_score_bounds_enforced(db_path: Path) -> None:
    with connect(db_path) as connection:
        attempt_id = _insert_attempt(connection, identification_candidate_id=None, media_file_id=None)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_match(connection, resolution_attempt_id=attempt_id, score=1.5)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_match(connection, resolution_attempt_id=attempt_id, score=-0.1)


def test_match_rank_must_be_positive(db_path: Path) -> None:
    with connect(db_path) as connection:
        attempt_id = _insert_attempt(connection, identification_candidate_id=None, media_file_id=None)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_match(connection, resolution_attempt_id=attempt_id, rank=0)


def test_uniqueness_rejects_duplicate_rank_within_attempt(db_path: Path) -> None:
    with connect(db_path) as connection:
        attempt_id = _insert_attempt(connection, identification_candidate_id=None, media_file_id=None)
        _insert_match(connection, resolution_attempt_id=attempt_id, provider_id=1, rank=1)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_match(connection, resolution_attempt_id=attempt_id, provider_id=2, rank=1)


def test_uniqueness_rejects_duplicate_provider_result_within_attempt(db_path: Path) -> None:
    with connect(db_path) as connection:
        attempt_id = _insert_attempt(connection, identification_candidate_id=None, media_file_id=None)
        _insert_match(connection, resolution_attempt_id=attempt_id, provider_id=348, rank=1)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_match(connection, resolution_attempt_id=attempt_id, provider_id=348, rank=2)


def test_at_most_one_selected_match_per_attempt(db_path: Path) -> None:
    with connect(db_path) as connection:
        attempt_id = _insert_attempt(connection, identification_candidate_id=None, media_file_id=None)
        _insert_match(connection, resolution_attempt_id=attempt_id, provider_id=1, rank=1, selected=1)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_match(connection, resolution_attempt_id=attempt_id, provider_id=2, rank=2, selected=1)


def test_unselected_matches_are_not_constrained_by_the_partial_index(db_path: Path) -> None:
    with connect(db_path) as connection:
        attempt_id = _insert_attempt(connection, identification_candidate_id=None, media_file_id=None)
        _insert_match(connection, resolution_attempt_id=attempt_id, provider_id=1, rank=1, selected=0)
        _insert_match(connection, resolution_attempt_id=attempt_id, provider_id=2, rank=2, selected=0)
        count = connection.execute("SELECT COUNT(*) FROM resolution_matches").fetchone()[0]
        assert count == 2


def test_deleting_attempt_cascades_to_matches(db_path: Path) -> None:
    with connect(db_path) as connection:
        attempt_id = _insert_attempt(connection, identification_candidate_id=None, media_file_id=None)
        match_id = _insert_match(connection, resolution_attempt_id=attempt_id)

        connection.execute("DELETE FROM resolution_attempts WHERE id = ?", (attempt_id,))

        row = connection.execute("SELECT * FROM resolution_matches WHERE id = ?", (match_id,)).fetchone()
        assert row is None


def test_selected_match_can_be_referenced_from_attempt(db_path: Path) -> None:
    """Exercises the attempts -> matches -> attempts circular reference:
    the match is inserted after its attempt, then the attempt is updated
    to point at its selected match."""
    with connect(db_path) as connection:
        attempt_id = _insert_attempt(connection, identification_candidate_id=None, media_file_id=None)
        match_id = _insert_match(connection, resolution_attempt_id=attempt_id, selected=1)
        connection.execute(
            "UPDATE resolution_attempts SET status = 'RESOLVED', selected_match_id = ? WHERE id = ?",
            (match_id, attempt_id),
        )
        row = connection.execute(
            "SELECT selected_match_id FROM resolution_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        assert row["selected_match_id"] == match_id


# --- media_identity_assignments ----------------------------------------------


@pytest.mark.parametrize("assignment_method", ["AUTO", "MANUAL"])
def test_assignment_accepts_all_valid_methods(db_path: Path, assignment_method: str) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        identity_id = _insert_external_identity(connection)
        assignment_id = _insert_assignment(
            connection, media_file_id=media_file_id, external_identity_id=identity_id, assignment_method=assignment_method
        )
        row = connection.execute(
            "SELECT assignment_method FROM media_identity_assignments WHERE id = ?", (assignment_id,)
        ).fetchone()
        assert row["assignment_method"] == assignment_method


def test_assignment_rejects_invalid_method(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        identity_id = _insert_external_identity(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_assignment(
                connection, media_file_id=media_file_id, external_identity_id=identity_id, assignment_method="GUESSED"
            )


@pytest.mark.parametrize("confidence", ["HIGH", "MEDIUM", "LOW"])
def test_assignment_accepts_all_valid_confidence_levels(db_path: Path, confidence: str) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        identity_id = _insert_external_identity(connection)
        assignment_id = _insert_assignment(
            connection, media_file_id=media_file_id, external_identity_id=identity_id, confidence=confidence
        )
        row = connection.execute(
            "SELECT confidence FROM media_identity_assignments WHERE id = ?", (assignment_id,)
        ).fetchone()
        assert row["confidence"] == confidence


def test_assignment_rejects_invalid_confidence(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        identity_id = _insert_external_identity(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_assignment(
                connection, media_file_id=media_file_id, external_identity_id=identity_id, confidence="CERTAIN"
            )


def test_at_most_one_active_assignment_per_media_file(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        identity_id_1 = _insert_external_identity(connection, provider_id=1)
        identity_id_2 = _insert_external_identity(connection, provider_id=2)
        _insert_assignment(connection, media_file_id=media_file_id, external_identity_id=identity_id_1, status="ACTIVE")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_assignment(
                connection, media_file_id=media_file_id, external_identity_id=identity_id_2, status="ACTIVE"
            )


def test_new_assignment_supersedes_prior_one(db_path: Path) -> None:
    """The documented supersede lifecycle: mark the old ACTIVE row
    SUPERSEDED, then insert a new ACTIVE row -- never UPDATE in place."""
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        identity_id_1 = _insert_external_identity(connection, provider_id=1)
        identity_id_2 = _insert_external_identity(connection, provider_id=2)
        first_id = _insert_assignment(connection, media_file_id=media_file_id, external_identity_id=identity_id_1)

        connection.execute(
            "UPDATE media_identity_assignments SET status = 'SUPERSEDED' WHERE id = ?", (first_id,)
        )
        second_id = _insert_assignment(connection, media_file_id=media_file_id, external_identity_id=identity_id_2)

        count = connection.execute("SELECT COUNT(*) FROM media_identity_assignments").fetchone()[0]
        assert count == 2
        active = connection.execute(
            "SELECT id FROM media_identity_assignments WHERE status = 'ACTIVE'"
        ).fetchone()
        assert active["id"] == second_id


def test_deleting_media_file_cascades_to_assignment(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        identity_id = _insert_external_identity(connection)
        assignment_id = _insert_assignment(connection, media_file_id=media_file_id, external_identity_id=identity_id)

        connection.execute("DELETE FROM media_files WHERE id = ?", (media_file_id,))

        row = connection.execute(
            "SELECT * FROM media_identity_assignments WHERE id = ?", (assignment_id,)
        ).fetchone()
        assert row is None


@pytest.mark.parametrize(
    "index_name",
    [
        "idx_resolution_attempts_candidate_id",
        "idx_resolution_attempts_media_file_id",
        "idx_resolution_attempts_status",
        "idx_resolution_attempts_started_at",
        "idx_resolution_matches_attempt_rank",
        "idx_resolution_matches_attempt_provider_id",
        "idx_resolution_matches_one_selected_per_attempt",
        "idx_resolution_matches_attempt_id",
        "idx_media_identity_assignments_active_media_file",
        "idx_media_identity_assignments_media_file_id",
        "idx_media_identity_assignments_external_identity_id",
        "idx_media_identity_assignments_status",
        "idx_media_identity_assignments_resolution_attempt_id",
    ],
)
def test_required_index_exists(db_path: Path, index_name: str) -> None:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name = ?", (index_name,)
        ).fetchone()
        assert row is not None, f"missing index {index_name}"


def test_migrate_from_scratch_applies_at_least_through_0008(tmp_path: Path) -> None:
    path = tmp_path / "mams.db"
    applied = migrate(path, REPO_MIGRATIONS_DIR)
    assert applied[:8] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_migrate_is_idempotent_after_0008(tmp_path: Path, db_path: Path) -> None:
    migrations_dir = _migrations_dir_through(
        tmp_path, "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008"
    )
    applied_again = migrate(db_path, migrations_dir)
    assert applied_again == []
