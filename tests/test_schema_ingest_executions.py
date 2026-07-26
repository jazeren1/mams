"""Tests for the execution-history schema
(database/migrations/0014_ingest_executions.sql): ingest_executions,
ingest_execution_steps, and scan_runs.triggered_by.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mams.db import connect, current_schema_version, migrate

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"

_THROUGH_0014 = (
    "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008",
    "0009", "0010", "0011", "0012", "0013", "0014",
)


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
    migrate(path, _migrations_dir_through(tmp_path, *_THROUGH_0014))
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


def _insert_plan(
    connection: sqlite3.Connection,
    *,
    media_file_id: int,
    status: str = "APPROVED",
    source_path: str = "/Users/johnzeren/Media Archive/Incoming/Alien.mkv",
) -> int:
    cursor = connection.execute(
        "INSERT INTO ingest_plans (media_file_id, status, source_path) VALUES (?, ?, ?)",
        (media_file_id, status, source_path),
    )
    return _lastrowid(cursor)


def _insert_execution(
    connection: sqlite3.Connection,
    *,
    ingest_plan_id: int,
    plan_version: int = 1,
    status: str = "EXECUTING",
    source_path: str = "/Users/johnzeren/Media Archive/Incoming/Alien.mkv",
    destination_path: str = "/Volumes/NASMedia/Movies/Alien (1979)/Alien (1979).mkv",
    source_size_bytes: int = 123456,
    lock_token: str = "token-1",
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO ingest_executions (
            ingest_plan_id, plan_version, status, source_path, destination_path,
            source_size_bytes, lock_token
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (ingest_plan_id, plan_version, status, source_path, destination_path, source_size_bytes, lock_token),
    )
    return _lastrowid(cursor)


def _insert_step(
    connection: sqlite3.Connection,
    *,
    ingest_execution_id: int,
    step_order: int = 1,
    step_type: str = "VALIDATE_SOURCE",
    status: str = "PENDING",
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO ingest_execution_steps (ingest_execution_id, step_order, step_type, status)
        VALUES (?, ?, ?, ?)
        """,
        (ingest_execution_id, step_order, step_type, status),
    )
    return _lastrowid(cursor)


def test_migrate_applies_through_0014(db_path: Path) -> None:
    with connect(db_path) as connection:
        assert current_schema_version(connection) == 14
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"ingest_executions", "ingest_execution_steps"} <= tables


def _seed_plan(connection: sqlite3.Connection) -> int:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
    return _insert_plan(connection, media_file_id=media_file_id)


@pytest.mark.parametrize("status", ["EXECUTING", "SUCCEEDED", "FAILED", "RECOVERY_REQUIRED"])
def test_execution_accepts_all_valid_statuses(db_path: Path, status: str) -> None:
    with connect(db_path) as connection:
        plan_id = _seed_plan(connection)
        execution_id = _insert_execution(connection, ingest_plan_id=plan_id, status=status)
        row = connection.execute("SELECT status FROM ingest_executions WHERE id = ?", (execution_id,)).fetchone()
        assert row["status"] == status


def test_execution_rejects_invalid_status(db_path: Path) -> None:
    with connect(db_path) as connection:
        plan_id = _seed_plan(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_execution(connection, ingest_plan_id=plan_id, status="BOGUS")


@pytest.mark.parametrize(
    "strategy", ["SAME_FILESYSTEM_ATOMIC_RENAME", "CROSS_FILESYSTEM_COPY_VERIFY_REMOVE"]
)
def test_execution_accepts_valid_transfer_strategies(db_path: Path, strategy: str) -> None:
    with connect(db_path) as connection:
        plan_id = _seed_plan(connection)
        execution_id = _insert_execution(connection, ingest_plan_id=plan_id)
        connection.execute(
            "UPDATE ingest_executions SET transfer_strategy = ? WHERE id = ?", (strategy, execution_id)
        )
        row = connection.execute(
            "SELECT transfer_strategy FROM ingest_executions WHERE id = ?", (execution_id,)
        ).fetchone()
        assert row["transfer_strategy"] == strategy


def test_execution_rejects_invalid_transfer_strategy(db_path: Path) -> None:
    with connect(db_path) as connection:
        plan_id = _seed_plan(connection)
        execution_id = _insert_execution(connection, ingest_plan_id=plan_id)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE ingest_executions SET transfer_strategy = 'TELEPORT' WHERE id = ?", (execution_id,)
            )


@pytest.mark.parametrize(
    "recovery_status",
    [
        "NONE", "PARTIAL_DESTINATION_SOURCE_INTACT", "DESTINATION_VERIFIED_SOURCE_NOT_REMOVED",
        "DESTINATION_UNVERIFIED_SOURCE_REMOVED", "INVENTORY_REFRESH_INCOMPLETE",
        "INTERRUPTED_STATE_UNKNOWN", "OTHER_REQUIRES_MANUAL_INSPECTION",
    ],
)
def test_execution_accepts_all_valid_recovery_statuses(db_path: Path, recovery_status: str) -> None:
    with connect(db_path) as connection:
        plan_id = _seed_plan(connection)
        execution_id = _insert_execution(connection, ingest_plan_id=plan_id)
        connection.execute(
            "UPDATE ingest_executions SET recovery_status = ? WHERE id = ?", (recovery_status, execution_id)
        )
        row = connection.execute(
            "SELECT recovery_status FROM ingest_executions WHERE id = ?", (execution_id,)
        ).fetchone()
        assert row["recovery_status"] == recovery_status


@pytest.mark.parametrize("plex_status", ["SKIPPED", "SUCCEEDED", "FAILED"])
def test_execution_accepts_all_valid_plex_refresh_statuses(db_path: Path, plex_status: str) -> None:
    with connect(db_path) as connection:
        plan_id = _seed_plan(connection)
        execution_id = _insert_execution(connection, ingest_plan_id=plan_id)
        connection.execute(
            "UPDATE ingest_executions SET plex_refresh_status = ? WHERE id = ?", (plex_status, execution_id)
        )
        row = connection.execute(
            "SELECT plex_refresh_status FROM ingest_executions WHERE id = ?", (execution_id,)
        ).fetchone()
        assert row["plex_refresh_status"] == plex_status


def test_execution_requires_plan_id_source_and_destination_paths(db_path: Path) -> None:
    with connect(db_path) as connection:
        plan_id = _seed_plan(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO ingest_executions (status, source_path, destination_path, "
                "source_size_bytes, lock_token) VALUES ('EXECUTING', '/a', '/b', 1, 'tok')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO ingest_executions (ingest_plan_id, plan_version, status, "
                "destination_path, source_size_bytes, lock_token) "
                "VALUES (?, 1, 'EXECUTING', '/b', 1, 'tok')",
                (plan_id,),
            )


def test_execution_defaults(db_path: Path) -> None:
    with connect(db_path) as connection:
        plan_id = _seed_plan(connection)
        execution_id = _insert_execution(connection, ingest_plan_id=plan_id)
        row = connection.execute("SELECT * FROM ingest_executions WHERE id = ?", (execution_id,)).fetchone()
        assert row["checksum_algorithm"] == "sha256"
        assert row["started_at"] is not None
        assert row["completed_at"] is None
        assert row["recovery_status"] is None
        assert row["plex_refresh_status"] is None


def test_deleting_plan_cascades_to_executions(db_path: Path) -> None:
    with connect(db_path) as connection:
        plan_id = _seed_plan(connection)
        execution_id = _insert_execution(connection, ingest_plan_id=plan_id)

        connection.execute("DELETE FROM ingest_plans WHERE id = ?", (plan_id,))

        row = connection.execute("SELECT * FROM ingest_executions WHERE id = ?", (execution_id,)).fetchone()
        assert row is None


# --- ingest_execution_steps ----------------------------------------------------


@pytest.mark.parametrize(
    "step_type",
    [
        "VALIDATE_SOURCE", "CREATE_DESTINATION_DIRECTORY", "STREAM_COPY_WITH_CHECKSUM",
        "COMPUTE_DESTINATION_CHECKSUM", "VERIFY_CHECKSUM_MATCH", "ATOMIC_RENAME", "FINAL_RENAME",
        "VERIFY_DESTINATION_MEDIA", "REMOVE_SOURCE", "REFRESH_INVENTORY", "PLEX_REFRESH", "FINALIZE",
    ],
)
def test_step_accepts_all_valid_types(db_path: Path, step_type: str) -> None:
    with connect(db_path) as connection:
        plan_id = _seed_plan(connection)
        execution_id = _insert_execution(connection, ingest_plan_id=plan_id)
        step_id = _insert_step(connection, ingest_execution_id=execution_id, step_type=step_type)
        row = connection.execute(
            "SELECT step_type FROM ingest_execution_steps WHERE id = ?", (step_id,)
        ).fetchone()
        assert row["step_type"] == step_type


def test_step_rejects_invalid_type(db_path: Path) -> None:
    with connect(db_path) as connection:
        plan_id = _seed_plan(connection)
        execution_id = _insert_execution(connection, ingest_plan_id=plan_id)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_step(connection, ingest_execution_id=execution_id, step_type="TELEPORT")


@pytest.mark.parametrize("status", ["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "SKIPPED"])
def test_step_accepts_all_valid_statuses(db_path: Path, status: str) -> None:
    with connect(db_path) as connection:
        plan_id = _seed_plan(connection)
        execution_id = _insert_execution(connection, ingest_plan_id=plan_id)
        step_id = _insert_step(connection, ingest_execution_id=execution_id, status=status)
        row = connection.execute("SELECT status FROM ingest_execution_steps WHERE id = ?", (step_id,)).fetchone()
        assert row["status"] == status


def test_step_rejects_invalid_status(db_path: Path) -> None:
    with connect(db_path) as connection:
        plan_id = _seed_plan(connection)
        execution_id = _insert_execution(connection, ingest_plan_id=plan_id)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_step(connection, ingest_execution_id=execution_id, status="BOGUS")


def test_step_order_must_be_positive(db_path: Path) -> None:
    with connect(db_path) as connection:
        plan_id = _seed_plan(connection)
        execution_id = _insert_execution(connection, ingest_plan_id=plan_id)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_step(connection, ingest_execution_id=execution_id, step_order=0)


def test_uniqueness_rejects_duplicate_step_order_within_execution(db_path: Path) -> None:
    with connect(db_path) as connection:
        plan_id = _seed_plan(connection)
        execution_id = _insert_execution(connection, ingest_plan_id=plan_id)
        _insert_step(connection, ingest_execution_id=execution_id, step_order=1, step_type="VALIDATE_SOURCE")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_step(connection, ingest_execution_id=execution_id, step_order=1, step_type="FINALIZE")


def test_uniqueness_allows_same_order_across_different_executions(db_path: Path) -> None:
    with connect(db_path) as connection:
        plan_id_1 = _seed_plan(connection)
        library_id = _insert_library(connection, category="tv")
        scan_id = _insert_scan_run(connection)
        media_file_id_2 = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="Two.mkv")
        plan_id_2 = _insert_plan(connection, media_file_id=media_file_id_2)
        execution_id_1 = _insert_execution(connection, ingest_plan_id=plan_id_1)
        execution_id_2 = _insert_execution(connection, ingest_plan_id=plan_id_2)
        _insert_step(connection, ingest_execution_id=execution_id_1, step_order=1)
        _insert_step(connection, ingest_execution_id=execution_id_2, step_order=1)
        count = connection.execute("SELECT COUNT(*) FROM ingest_execution_steps").fetchone()[0]
        assert count == 2


def test_deleting_execution_cascades_to_steps(db_path: Path) -> None:
    with connect(db_path) as connection:
        plan_id = _seed_plan(connection)
        execution_id = _insert_execution(connection, ingest_plan_id=plan_id)
        step_id = _insert_step(connection, ingest_execution_id=execution_id)

        connection.execute("DELETE FROM ingest_executions WHERE id = ?", (execution_id,))

        row = connection.execute("SELECT * FROM ingest_execution_steps WHERE id = ?", (step_id,)).fetchone()
        assert row is None


# --- scan_runs.triggered_by -----------------------------------------------------


def test_scan_run_defaults_triggered_by_to_scan(db_path: Path) -> None:
    with connect(db_path) as connection:
        scan_id = _insert_scan_run(connection)
        row = connection.execute("SELECT triggered_by FROM scan_runs WHERE id = ?", (scan_id,)).fetchone()
        assert row["triggered_by"] == "SCAN"


def test_scan_run_accepts_execution_triggered_by(db_path: Path) -> None:
    with connect(db_path) as connection:
        cursor = connection.execute("INSERT INTO scan_runs (triggered_by) VALUES ('EXECUTION')")
        scan_id = _lastrowid(cursor)
        row = connection.execute("SELECT triggered_by FROM scan_runs WHERE id = ?", (scan_id,)).fetchone()
        assert row["triggered_by"] == "EXECUTION"


def test_scan_run_rejects_invalid_triggered_by(db_path: Path) -> None:
    with connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO scan_runs (triggered_by) VALUES ('BOGUS')")


@pytest.mark.parametrize(
    "index_name",
    [
        "idx_ingest_executions_plan_id",
        "idx_ingest_executions_status",
        "idx_ingest_executions_recovery_status",
        "idx_ingest_execution_steps_execution_order",
        "idx_ingest_execution_steps_execution_id",
    ],
)
def test_required_index_exists(db_path: Path, index_name: str) -> None:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name = ?", (index_name,)
        ).fetchone()
        assert row is not None, f"missing index {index_name}"


def test_migrate_is_idempotent_after_0014(tmp_path: Path, db_path: Path) -> None:
    migrations_dir = _migrations_dir_through(tmp_path, *_THROUGH_0014)
    applied_again = migrate(db_path, migrations_dir)
    assert applied_again == []
