"""Tests for execution_service.py: the Milestone 8 approved-plan
executor. Builds a genuinely READY_FOR_EXECUTOR plan the same way
test_ingest_service.py's audit tests do (seed media file -> candidate
-> resolve identity -> generate_plan -> approve), then exercises
execute_plan() against real files under tmp_path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mams.config import AppConfig
from mams.db import connect, migrate
from mams.execution import FaultPoint
from mams.execution_lock import acquire_lock, read_lock, release_lock
from mams.execution_repository import get_execution
from mams.execution_service import (
    ActiveExecutionExistsError,
    ExecutionLockHeldError,
    PlanNotApprovedError,
    PlanNotExecutableError,
    PlanNotFoundError,
    execute_plan,
    inspect_recovery,
)
from mams.ingest_repository import get_plan
from mams.ingest_service import approve, generate_plan
from mams.mediainfo import AudioTrack, MediaInfo, MediaInfoOutcome, VideoTrack
from mams.resolution_repository import assign_identity, upsert_external_identity

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"


@pytest.fixture()
def connection(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "mams.db"
    migrate(db_path, REPO_MIGRATIONS_DIR)
    conn = connect(db_path)
    yield conn
    conn.close()


def _config(tmp_path: Path, **execution_overrides: object) -> AppConfig:
    execution: dict[str, object] = {"state_directory": str(tmp_path / ".mams" / "locks")}
    execution.update(execution_overrides)
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
            "execution": execution,
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
    size_bytes: int = 4096,
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
    payload = b"\x01\x02\x03\x04" * (size_bytes // 4)
    incoming_file.write_bytes(payload)
    absolute_path = str(incoming_file)
    real_mtime = incoming_file.stat().st_mtime
    media_file_id = _lastrowid(
        connection.execute(
            """
            INSERT INTO media_files (
                library_id, absolute_path, relative_path, filename, extension, parent_directory, layout,
                size_bytes, state, duration_seconds, media_info_error, media_info_probed_at, container,
                first_seen_scan_id, last_seen_scan_id
            ) VALUES (?, ?, ?, ?, ?, ?, 'unknown', ?, 'ACTIVE', ?, NULL, '2024-01-01T00:00:00', 'Matroska', ?, ?)
            """,
            (
                library_id, absolute_path, name, name, ".mkv", str(tmp_path / "Incoming"),
                len(payload), 7020.0, scan_id, scan_id,
            ),
        )
    )
    connection.execute(
        "UPDATE media_files SET mtime = ? WHERE id = ?", (real_mtime, media_file_id)
    )
    connection.execute("INSERT INTO video_tracks (media_file_id, track_index) VALUES (?, 0)", (media_file_id,))
    connection.execute("INSERT INTO audio_tracks (media_file_id, track_index) VALUES (?, 0)", (media_file_id,))
    return media_file_id


def _seed_candidate(connection: sqlite3.Connection, *, media_file_id: int) -> int:
    return _lastrowid(
        connection.execute(
            "INSERT INTO identification_candidates (media_file_id, candidate_type, parsed_title, confidence, parser_version) "
            "VALUES (?, 'MOVIE', 'Alien', 'HIGH', 1)",
            (media_file_id,),
        )
    )


def _resolve_movie_identity(connection: sqlite3.Connection, *, media_file_id: int, candidate_id: int) -> int:
    identity = upsert_external_identity(connection, media_type="MOVIE", provider_id=348, title="Alien", release_year=1979)
    assignment = assign_identity(
        connection, media_file_id=media_file_id, identification_candidate_id=candidate_id,
        external_identity_id=identity.id, resolution_attempt_id=None, assignment_method="AUTO", confidence="HIGH",
    )
    return assignment.id


def _ensure_nas_roots_exist(tmp_path: Path) -> None:
    """A real NAS mount would already exist as a directory before any
    execution is attempted -- generate_plan()/approve() never require
    this (dry-run only), but execute_plan()'s preflight does (it must
    stat the destination root to resolve a device id and free space)."""
    for category in ("Movies", "KidsMovies", "TV", "KidsShows"):
        (tmp_path / "NAS" / category).mkdir(parents=True, exist_ok=True)


def _build_ready_approved_plan(connection: sqlite3.Connection, tmp_path: Path, config: AppConfig, *, name: str = "Alien.mkv") -> int:
    _ensure_nas_roots_exist(tmp_path)
    media_file_id = _seed_media_file(connection, tmp_path, name=name)
    candidate_id = _seed_candidate(connection, media_file_id=media_file_id)
    _resolve_movie_identity(connection, media_file_id=media_file_id, candidate_id=candidate_id)
    plan = generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")
    assert plan.status == "READY_FOR_REVIEW"
    approved = approve(connection, plan.id)
    assert approved.status == "APPROVED"
    return approved.id


class _FakeMetadataProvider:
    """Always reports a plausible probe result -- avoids depending on
    the real `mediainfo` executable being installed for these tests."""

    def probe(self, path: Path) -> MediaInfoOutcome:
        return MediaInfoOutcome(
            media_info=MediaInfo(
                container="Matroska",
                duration_seconds=7020.0,
                overall_bitrate=5_000_000,
                video_tracks=(
                    VideoTrack(
                        codec="HEVC", width=1920, height=1080, aspect_ratio="16:9",
                        frame_rate=23.976, hdr_format=None, bit_depth=8, scan_type="Progressive",
                    ),
                ),
                audio_tracks=(AudioTrack(codec="AC3", language="eng", channels=6, bitrate=640_000, default=True),),
                subtitle_tracks=(),
            ),
            error=None,
        )


def test_execute_plan_same_filesystem_happy_path(connection: sqlite3.Connection, tmp_path: Path) -> None:
    config = _config(tmp_path)
    plan_id = _build_ready_approved_plan(connection, tmp_path, config)
    source_path = tmp_path / "Incoming" / "Alien.mkv"
    original_bytes = source_path.read_bytes()

    result = execute_plan(connection, config, plan_id=plan_id, metadata_provider=_FakeMetadataProvider())

    assert result.status == "SUCCEEDED"
    assert result.transfer_strategy == "SAME_FILESYSTEM_ATOMIC_RENAME"
    assert result.recovery_status == "NONE"

    destination_path = Path(result.destination_path)
    assert destination_path.exists()
    assert destination_path.read_bytes() == original_bytes
    assert not source_path.exists()

    plan = get_plan(connection, plan_id)
    assert plan is not None
    assert plan.status == "EXECUTED"
    assert plan.executed_at is not None

    media_file = connection.execute(
        "SELECT * FROM media_files WHERE absolute_path = ?", (str(destination_path),)
    ).fetchone()
    assert media_file is not None
    assert media_file["state"] == "ACTIVE"

    change = connection.execute(
        "SELECT * FROM scan_changes WHERE media_file_id = ? ORDER BY id DESC LIMIT 1", (media_file["id"],)
    ).fetchone()
    assert change["change_type"] == "UPDATED"
    assert change["previous_absolute_path"] == str(source_path)

    assert result.steps[0].step_type == "VALIDATE_SOURCE"
    assert result.steps[-1].step_type == "FINALIZE"
    assert all(step.status == "SUCCEEDED" or step.status == "SKIPPED" for step in result.steps)
    plex_step = next(step for step in result.steps if step.step_type == "PLEX_REFRESH")
    assert plex_step.status == "SKIPPED"
    assert result.plex_refresh_status == "SKIPPED"


def test_execute_plan_cross_filesystem_happy_path(
    connection: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forces the cross-filesystem strategy by monkeypatching
    stat_device_id -- tmp_path is a single real filesystem, so this is
    the only way to exercise the copy/checksum/commit/remove path in a
    fast, hermetic unit test without a second real mounted volume."""
    import mams.execution_filesystem as fs_module

    config = _config(tmp_path)
    plan_id = _build_ready_approved_plan(connection, tmp_path, config, name="Alien2.mkv")
    plan = get_plan(connection, plan_id)
    assert plan is not None
    source_path = Path(plan.source_path)
    original_bytes = source_path.read_bytes()

    def _fake_stat_device_id(path: str) -> int:
        return 1 if path == str(source_path) else 2

    monkeypatch.setattr(fs_module, "stat_device_id", _fake_stat_device_id)

    result = execute_plan(connection, config, plan_id=plan_id, metadata_provider=_FakeMetadataProvider())

    assert result.status == "SUCCEEDED"
    assert result.transfer_strategy == "CROSS_FILESYSTEM_COPY_VERIFY_REMOVE"
    assert result.recovery_status == "NONE"
    assert result.source_checksum is not None
    assert result.destination_checksum == result.source_checksum

    destination_path = Path(result.destination_path)
    assert destination_path.exists()
    assert destination_path.read_bytes() == original_bytes
    assert not source_path.exists()

    leftover_temp_files = list(destination_path.parent.glob(f".{destination_path.name}.mams-partial-*"))
    assert leftover_temp_files == []

    plan_after = get_plan(connection, plan_id)
    assert plan_after is not None
    assert plan_after.status == "EXECUTED"

    step_types = [step.step_type for step in result.steps]
    assert step_types == [
        "VALIDATE_SOURCE",
        "CREATE_DESTINATION_DIRECTORY",
        "STREAM_COPY_WITH_CHECKSUM",
        "COMPUTE_DESTINATION_CHECKSUM",
        "VERIFY_CHECKSUM_MATCH",
        "FINAL_RENAME",
        "VERIFY_DESTINATION_MEDIA",
        "REMOVE_SOURCE",
        "REFRESH_INVENTORY",
        "PLEX_REFRESH",
        "FINALIZE",
    ]
    assert all(step.status in ("SUCCEEDED", "SKIPPED") for step in result.steps)


def test_execute_plan_cross_filesystem_succeeds_when_hard_links_are_unsupported_like_smb(
    connection: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test reproducing the real Milestone 8 NAS acceptance
    failure end to end: a local Incoming source, simulated different
    source/destination device IDs (forcing the cross-filesystem
    strategy), and os.link() raising errno 45 ENOTSUP exactly as the
    real SMB-mounted NAS destination did against execution #1. Copy and
    checksum must still succeed, finalization must succeed via the new
    SMB-safe primitive (which never calls os.link()), the source must be
    removed only after destination verification passes, and the plan
    and execution must both reach their success states."""
    import mams.execution_filesystem as fs_module

    config = _config(tmp_path)
    plan_id = _build_ready_approved_plan(connection, tmp_path, config, name="District9.mkv")
    plan = get_plan(connection, plan_id)
    assert plan is not None
    source_path = Path(plan.source_path)
    original_bytes = source_path.read_bytes()

    def _fake_stat_device_id(path: str) -> int:
        return 1 if path == str(source_path) else 2

    monkeypatch.setattr(fs_module, "stat_device_id", _fake_stat_device_id)

    def _raise_enotsup(*args: object, **kwargs: object) -> None:
        raise OSError(45, "Operation not supported")

    monkeypatch.setattr("os.link", _raise_enotsup)

    result = execute_plan(connection, config, plan_id=plan_id, metadata_provider=_FakeMetadataProvider())

    assert result.status == "SUCCEEDED"
    assert result.transfer_strategy == "CROSS_FILESYSTEM_COPY_VERIFY_REMOVE"
    assert result.recovery_status == "NONE"
    assert result.source_checksum is not None
    assert result.destination_checksum == result.source_checksum

    destination_path = Path(result.destination_path)
    assert destination_path.exists()
    assert destination_path.read_bytes() == original_bytes
    assert not source_path.exists()
    assert result.source_removed_at is not None

    leftover_temp_files = list(destination_path.parent.glob(f".{destination_path.name}.mams-partial-*"))
    assert leftover_temp_files == []

    plan_after = get_plan(connection, plan_id)
    assert plan_after is not None
    assert plan_after.status == "EXECUTED"

    step_types_and_status = [(step.step_type, step.status) for step in result.steps]
    assert step_types_and_status == [
        ("VALIDATE_SOURCE", "SUCCEEDED"),
        ("CREATE_DESTINATION_DIRECTORY", "SUCCEEDED"),
        ("STREAM_COPY_WITH_CHECKSUM", "SUCCEEDED"),
        ("COMPUTE_DESTINATION_CHECKSUM", "SUCCEEDED"),
        ("VERIFY_CHECKSUM_MATCH", "SUCCEEDED"),
        ("FINAL_RENAME", "SUCCEEDED"),
        ("VERIFY_DESTINATION_MEDIA", "SUCCEEDED"),
        ("REMOVE_SOURCE", "SUCCEEDED"),
        ("REFRESH_INVENTORY", "SUCCEEDED"),
        ("PLEX_REFRESH", "SKIPPED"),
        ("FINALIZE", "SUCCEEDED"),
    ]


def test_execute_plan_rejects_unknown_plan(connection: sqlite3.Connection, tmp_path: Path) -> None:
    config = _config(tmp_path)
    with pytest.raises(PlanNotFoundError):
        execute_plan(connection, config, plan_id=999_999)


def test_execute_plan_rejects_non_approved_plan(connection: sqlite3.Connection, tmp_path: Path) -> None:
    config = _config(tmp_path)
    media_file_id = _seed_media_file(connection, tmp_path)
    candidate_id = _seed_candidate(connection, media_file_id=media_file_id)
    _resolve_movie_identity(connection, media_file_id=media_file_id, candidate_id=candidate_id)
    plan = generate_plan(connection, config, media_file_id=media_file_id, destination_category="movie")
    assert plan.status == "READY_FOR_REVIEW"

    with pytest.raises(PlanNotApprovedError):
        execute_plan(connection, config, plan_id=plan.id)


def test_execute_plan_rejects_stale_plan(connection: sqlite3.Connection, tmp_path: Path) -> None:
    """Simulates a rescan that recorded a changed size in canonical
    inventory without the plan being regenerated -- exactly what
    readiness.py's source_size_matches_plan_snapshot check exists to
    catch, independent of and before any live filesystem preflight."""
    config = _config(tmp_path)
    plan_id = _build_ready_approved_plan(connection, tmp_path, config)
    plan = get_plan(connection, plan_id)
    assert plan is not None
    connection.execute("UPDATE media_files SET size_bytes = size_bytes + 1 WHERE id = ?", (plan.media_file_id,))

    with pytest.raises(PlanNotExecutableError):
        execute_plan(connection, config, plan_id=plan_id, metadata_provider=_FakeMetadataProvider())

    plan = get_plan(connection, plan_id)
    assert plan is not None
    assert plan.status == "APPROVED"


def test_execute_plan_rejects_second_call_while_active(connection: sqlite3.Connection, tmp_path: Path) -> None:
    """A currently-EXECUTING execution (simulated by directly inserting
    one) must block a second execute_plan() call for the same plan."""
    config = _config(tmp_path)
    plan_id = _build_ready_approved_plan(connection, tmp_path, config)
    with connection:
        connection.execute("UPDATE ingest_plans SET status = 'EXECUTING' WHERE id = ?", (plan_id,))
        connection.execute(
            """
            INSERT INTO ingest_executions (
                ingest_plan_id, plan_version, status, source_path, destination_path, source_size_bytes, lock_token
            ) VALUES (?, 1, 'EXECUTING', '/x', '/y', 1, 'tok')
            """,
            (plan_id,),
        )

    with pytest.raises((PlanNotApprovedError, ActiveExecutionExistsError)):
        execute_plan(connection, config, plan_id=plan_id, metadata_provider=_FakeMetadataProvider())


# --- fault injection: every mutation boundary ------------------------------------

_FAULT_POINT_EXPECTATIONS: dict[FaultPoint, tuple[str, str, str]] = {
    FaultPoint.BEFORE_DIRECTORY_CREATE: ("EXECUTION_FAILED", "FAILED", "NONE"),
    FaultPoint.AFTER_DIRECTORY_CREATE: ("EXECUTION_FAILED", "FAILED", "NONE"),
    FaultPoint.BEFORE_COPY_START: ("RECOVERY_REQUIRED", "RECOVERY_REQUIRED", "PARTIAL_DESTINATION_SOURCE_INTACT"),
    FaultPoint.DURING_COPY: ("RECOVERY_REQUIRED", "RECOVERY_REQUIRED", "PARTIAL_DESTINATION_SOURCE_INTACT"),
    FaultPoint.AFTER_COPY_COMPLETE: ("RECOVERY_REQUIRED", "RECOVERY_REQUIRED", "PARTIAL_DESTINATION_SOURCE_INTACT"),
    FaultPoint.BEFORE_CHECKSUM_COMPUTE: ("RECOVERY_REQUIRED", "RECOVERY_REQUIRED", "PARTIAL_DESTINATION_SOURCE_INTACT"),
    FaultPoint.AFTER_CHECKSUM_COMPUTE_BEFORE_RENAME: (
        "RECOVERY_REQUIRED", "RECOVERY_REQUIRED", "PARTIAL_DESTINATION_SOURCE_INTACT",
    ),
    FaultPoint.BEFORE_FINAL_RENAME: ("RECOVERY_REQUIRED", "RECOVERY_REQUIRED", "PARTIAL_DESTINATION_SOURCE_INTACT"),
    FaultPoint.AFTER_FINAL_RENAME: ("RECOVERY_REQUIRED", "RECOVERY_REQUIRED", "PARTIAL_DESTINATION_SOURCE_INTACT"),
    FaultPoint.BEFORE_DESTINATION_VERIFY: (
        "RECOVERY_REQUIRED", "RECOVERY_REQUIRED", "DESTINATION_VERIFIED_SOURCE_NOT_REMOVED",
    ),
    FaultPoint.AFTER_DESTINATION_VERIFY_BEFORE_SOURCE_REMOVE: (
        "RECOVERY_REQUIRED", "RECOVERY_REQUIRED", "DESTINATION_VERIFIED_SOURCE_NOT_REMOVED",
    ),
    FaultPoint.AFTER_SOURCE_REMOVE_BEFORE_INVENTORY_REFRESH: (
        "RECOVERY_REQUIRED", "RECOVERY_REQUIRED", "INVENTORY_REFRESH_INCOMPLETE",
    ),
    FaultPoint.AFTER_INVENTORY_REFRESH_BEFORE_FINALIZE: (
        "RECOVERY_REQUIRED", "RECOVERY_REQUIRED", "INVENTORY_REFRESH_INCOMPLETE",
    ),
}


class _FailAtPoint:
    def __init__(self, point: FaultPoint) -> None:
        self._point = point

    def maybe_fail(self, point: FaultPoint) -> None:
        if point is self._point:
            raise RuntimeError(f"injected failure at {point.value}")


def test_fault_point_expectations_cover_every_fault_point() -> None:
    assert set(_FAULT_POINT_EXPECTATIONS) == set(FaultPoint)


@pytest.mark.parametrize("point", list(FaultPoint))
def test_fault_injection_at_every_boundary_produces_the_documented_outcome(
    connection: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: FaultPoint
) -> None:
    """Proves the failure-mapping table from docs/EXECUTION-SAFETY.md at
    all 13 named mutation boundaries -- exercised on the cross-filesystem
    path, the only one where every boundary is reachable."""
    import mams.execution_filesystem as fs_module

    config = _config(tmp_path)
    plan_id = _build_ready_approved_plan(connection, tmp_path, config, name=f"Alien-{point.value}.mkv")
    plan = get_plan(connection, plan_id)
    assert plan is not None
    source_path = Path(plan.source_path)
    original_bytes = source_path.read_bytes()

    def _fake_stat_device_id(path: str) -> int:
        return 1 if path == str(source_path) else 2

    monkeypatch.setattr(fs_module, "stat_device_id", _fake_stat_device_id)

    result = execute_plan(
        connection,
        config,
        plan_id=plan_id,
        metadata_provider=_FakeMetadataProvider(),
        fault_injector=_FailAtPoint(point),
    )

    expected_plan_status, expected_execution_status, expected_recovery_status = _FAULT_POINT_EXPECTATIONS[point]
    assert result.status == expected_execution_status
    assert result.recovery_status == expected_recovery_status

    plan_after = get_plan(connection, plan_id)
    assert plan_after is not None
    assert plan_after.status == expected_plan_status
    assert plan_after.status != "EXECUTED"

    destination_path = Path(result.destination_path)
    if expected_recovery_status in ("NONE", "PARTIAL_DESTINATION_SOURCE_INTACT"):
        # finalize_same_filesystem_source_move/finalize_verified_temp_file
        # only ever remove the source/temp as their unguarded last step,
        # after every prior check passed -- so the source is guaranteed
        # intact for every fault point up to and including a failure
        # inside the rename/commit step itself.
        assert source_path.exists()
        assert source_path.read_bytes() == original_bytes
    elif expected_recovery_status == "DESTINATION_VERIFIED_SOURCE_NOT_REMOVED":
        assert source_path.exists()
        assert destination_path.exists()
        assert destination_path.read_bytes() == original_bytes
    elif expected_recovery_status == "INVENTORY_REFRESH_INCOMPLETE":
        assert not source_path.exists()
        assert destination_path.exists()
        assert destination_path.read_bytes() == original_bytes


def test_fault_injection_never_produces_a_partial_temp_file_that_silently_disappears(
    connection: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy interrupted mid-stream must leave its partial temp file on
    disk as recovery evidence -- never auto-deleted."""
    import mams.execution_filesystem as fs_module

    config = _config(tmp_path)
    plan_id = _build_ready_approved_plan(connection, tmp_path, config, name="AlienPartial.mkv")
    plan = get_plan(connection, plan_id)
    assert plan is not None
    source_path = Path(plan.source_path)

    def _fake_stat_device_id(path: str) -> int:
        return 1 if path == str(source_path) else 2

    monkeypatch.setattr(fs_module, "stat_device_id", _fake_stat_device_id)

    result = execute_plan(
        connection,
        config,
        plan_id=plan_id,
        metadata_provider=_FakeMetadataProvider(),
        fault_injector=_FailAtPoint(FaultPoint.DURING_COPY),
    )

    assert result.recovery_status == "PARTIAL_DESTINATION_SOURCE_INTACT"
    destination_directory = Path(plan.destination_directory)  # type: ignore[arg-type]
    temp_files = list(destination_directory.glob(".*.mams-partial-*"))
    assert len(temp_files) == 1
    assert temp_files[0].exists()


# --- lock contention ---------------------------------------------------------------


def test_execute_plan_rejects_when_lock_already_held(connection: sqlite3.Connection, tmp_path: Path) -> None:
    config = _config(tmp_path)
    plan_id = _build_ready_approved_plan(connection, tmp_path, config, name="AlienLocked.mkv")
    plan = get_plan(connection, plan_id)
    assert plan is not None
    source_path = Path(plan.source_path)
    original_bytes = source_path.read_bytes()

    state_directory = config.execution_state_directory
    assert state_directory is not None
    acquire_lock(state_directory, plan_id, token="someone-elses-token")

    with pytest.raises(ExecutionLockHeldError) as excinfo:
        execute_plan(connection, config, plan_id=plan_id, metadata_provider=_FakeMetadataProvider())

    assert excinfo.value.existing is not None
    assert excinfo.value.existing.token == "someone-elses-token"

    # Nothing was mutated: no DB writes, no filesystem writes.
    plan_after = get_plan(connection, plan_id)
    assert plan_after is not None
    assert plan_after.status == "APPROVED"
    assert source_path.exists()
    assert source_path.read_bytes() == original_bytes
    destination_directory = Path(plan.destination_directory)  # type: ignore[arg-type]
    assert not destination_directory.exists()

    release_lock(state_directory, plan_id, token="someone-elses-token")


def test_execute_plan_releases_lock_after_success(connection: sqlite3.Connection, tmp_path: Path) -> None:
    config = _config(tmp_path)
    plan_id = _build_ready_approved_plan(connection, tmp_path, config, name="AlienUnlock.mkv")
    execute_plan(connection, config, plan_id=plan_id, metadata_provider=_FakeMetadataProvider())

    state_directory = config.execution_state_directory
    assert state_directory is not None
    assert read_lock(state_directory, plan_id) is None


def test_execute_plan_releases_lock_after_preflight_failure(connection: sqlite3.Connection, tmp_path: Path) -> None:
    config = _config(tmp_path)
    plan_id = _build_ready_approved_plan(connection, tmp_path, config, name="AlienPreflightFail.mkv")
    plan = get_plan(connection, plan_id)
    assert plan is not None
    # Remove the source file after approval so preflight's source_exists
    # check fails -- the audit's DB-only check still passes since
    # nothing rescanned, so this reaches preflight and is rejected there.
    Path(plan.source_path).unlink()

    with pytest.raises(Exception):  # noqa: B017 -- PreflightFailedError, imported lazily below
        execute_plan(connection, config, plan_id=plan_id, metadata_provider=_FakeMetadataProvider())

    state_directory = config.execution_state_directory
    assert state_directory is not None
    assert read_lock(state_directory, plan_id) is None
    plan_after = get_plan(connection, plan_id)
    assert plan_after is not None
    assert plan_after.status == "APPROVED"


# --- inspect_recovery ----------------------------------------------------------------


def test_inspect_recovery_returns_none_for_unknown_execution(connection: sqlite3.Connection, tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert inspect_recovery(connection, config, execution_id=999_999) is None


def test_inspect_recovery_reports_partial_destination_source_intact(
    connection: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mams.execution_filesystem as fs_module

    config = _config(tmp_path)
    plan_id = _build_ready_approved_plan(connection, tmp_path, config, name="AlienRecoveryA.mkv")
    plan = get_plan(connection, plan_id)
    assert plan is not None
    source_path = Path(plan.source_path)

    def _fake_stat_device_id(path: str) -> int:
        return 1 if path == str(source_path) else 2

    monkeypatch.setattr(fs_module, "stat_device_id", _fake_stat_device_id)
    result = execute_plan(
        connection, config, plan_id=plan_id, metadata_provider=_FakeMetadataProvider(),
        fault_injector=_FailAtPoint(FaultPoint.AFTER_COPY_COMPLETE),
    )
    assert result.recovery_status == "PARTIAL_DESTINATION_SOURCE_INTACT"

    guidance = inspect_recovery(connection, config, execution_id=result.id)
    assert guidance is not None
    assert guidance.recovery_status == "PARTIAL_DESTINATION_SOURCE_INTACT"
    assert guidance.source_exists is True
    assert guidance.temp_file_exists is True
    assert "temporary file" in guidance.recommendation

    # Recovery output must report the exact discovered temp path, not
    # just a boolean -- matches the real production naming pattern
    # `.<final-filename>.mams-partial-<token>` exactly.
    destination_path = Path(result.destination_path)
    assert len(guidance.temp_file_paths) == 1
    discovered_temp = Path(guidance.temp_file_paths[0])
    assert discovered_temp.parent == destination_path.parent
    assert discovered_temp.name.startswith(f".{destination_path.name}.mams-partial-")
    assert discovered_temp.exists()
    assert guidance.temp_file_paths[0] in guidance.recommendation


def test_inspect_recovery_reports_destination_verified_source_not_removed(
    connection: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mams.execution_filesystem as fs_module

    config = _config(tmp_path)
    plan_id = _build_ready_approved_plan(connection, tmp_path, config, name="AlienRecoveryB.mkv")
    plan = get_plan(connection, plan_id)
    assert plan is not None
    source_path = Path(plan.source_path)

    def _fake_stat_device_id(path: str) -> int:
        return 1 if path == str(source_path) else 2

    monkeypatch.setattr(fs_module, "stat_device_id", _fake_stat_device_id)
    result = execute_plan(
        connection, config, plan_id=plan_id, metadata_provider=_FakeMetadataProvider(),
        fault_injector=_FailAtPoint(FaultPoint.BEFORE_DESTINATION_VERIFY),
    )
    assert result.recovery_status == "DESTINATION_VERIFIED_SOURCE_NOT_REMOVED"

    guidance = inspect_recovery(connection, config, execution_id=result.id)
    assert guidance is not None
    assert guidance.source_exists is True
    assert guidance.destination_exists is True
    assert "not removed" in guidance.recommendation.lower()


def test_inspect_recovery_reports_inventory_refresh_incomplete(
    connection: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mams.execution_filesystem as fs_module

    config = _config(tmp_path)
    plan_id = _build_ready_approved_plan(connection, tmp_path, config, name="AlienRecoveryC.mkv")
    plan = get_plan(connection, plan_id)
    assert plan is not None
    source_path = Path(plan.source_path)

    def _fake_stat_device_id(path: str) -> int:
        return 1 if path == str(source_path) else 2

    monkeypatch.setattr(fs_module, "stat_device_id", _fake_stat_device_id)
    result = execute_plan(
        connection, config, plan_id=plan_id, metadata_provider=_FakeMetadataProvider(),
        fault_injector=_FailAtPoint(FaultPoint.AFTER_INVENTORY_REFRESH_BEFORE_FINALIZE),
    )
    assert result.recovery_status == "INVENTORY_REFRESH_INCOMPLETE"

    guidance = inspect_recovery(connection, config, execution_id=result.id)
    assert guidance is not None
    assert guidance.source_exists is False
    assert guidance.destination_exists is True
    assert "inventory" in guidance.recommendation.lower()


def test_inspect_recovery_never_mutates_database_or_filesystem(
    connection: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mams.execution_filesystem as fs_module

    config = _config(tmp_path)
    plan_id = _build_ready_approved_plan(connection, tmp_path, config, name="AlienRecoveryD.mkv")
    plan = get_plan(connection, plan_id)
    assert plan is not None
    source_path = Path(plan.source_path)

    def _fake_stat_device_id(path: str) -> int:
        return 1 if path == str(source_path) else 2

    monkeypatch.setattr(fs_module, "stat_device_id", _fake_stat_device_id)
    result = execute_plan(
        connection, config, plan_id=plan_id, metadata_provider=_FakeMetadataProvider(),
        fault_injector=_FailAtPoint(FaultPoint.BEFORE_CHECKSUM_COMPUTE),
    )

    before = get_execution(connection, result.id)
    assert before is not None

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("inspect_recovery must never mutate the filesystem")

    monkeypatch.setattr(Path, "unlink", _forbidden)
    monkeypatch.setattr(Path, "mkdir", _forbidden)

    guidance = inspect_recovery(connection, config, execution_id=result.id)
    assert guidance is not None

    after = get_execution(connection, result.id)
    assert after == before


def test_inspect_recovery_classifies_orphaned_executing_as_interrupted(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """An EXECUTING row with no matching (or no) lock file simulates a
    crashed process -- inspect_recovery must not assume 'still running'
    just because the DB status says EXECUTING."""
    config = _config(tmp_path)
    plan_id = _build_ready_approved_plan(connection, tmp_path, config, name="AlienOrphaned.mkv")
    with connection:
        connection.execute("UPDATE ingest_plans SET status = 'EXECUTING' WHERE id = ?", (plan_id,))
        cursor = connection.execute(
            """
            INSERT INTO ingest_executions (
                ingest_plan_id, plan_version, status, source_path, destination_path, source_size_bytes, lock_token
            ) VALUES (?, 1, 'EXECUTING', '/x', '/y', 1, 'orphaned-token')
            """,
            (plan_id,),
        )
        execution_id = cursor.lastrowid
    assert execution_id is not None

    guidance = inspect_recovery(connection, config, execution_id=execution_id)
    assert guidance is not None
    assert guidance.recovery_status == "INTERRUPTED_STATE_UNKNOWN"
    assert guidance.lock_held is False
