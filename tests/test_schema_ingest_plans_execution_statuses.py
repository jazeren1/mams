"""Tests for the ingest_plans execution-status widening
(database/migrations/0013_ingest_plans_execution_statuses.sql): the new
CHECK-constraint values, that the 12-step table rebuild preserves every
existing row (including id, so ingest_plan_actions.ingest_plan_id values
stay valid with zero changes to that child table), and that no foreign
key is left dangling by the rebuild.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mams.db import connect, current_schema_version, migrate

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"

_THROUGH_0012 = (
    "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010", "0011", "0012",
)
_THROUGH_0013 = _THROUGH_0012 + ("0013",)


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
    migrate(path, _migrations_dir_through(tmp_path, *_THROUGH_0013))
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
    status: str = "DRAFT",
    source_path: str = "/Users/johnzeren/Media Archive/Incoming/Alien.mkv",
) -> int:
    cursor = connection.execute(
        "INSERT INTO ingest_plans (media_file_id, status, source_path) VALUES (?, ?, ?)",
        (media_file_id, status, source_path),
    )
    return _lastrowid(cursor)


def _insert_action(
    connection: sqlite3.Connection, *, ingest_plan_id: int, action_order: int = 1, action_type: str = "VALIDATE_SOURCE"
) -> int:
    cursor = connection.execute(
        "INSERT INTO ingest_plan_actions (ingest_plan_id, action_order, action_type) VALUES (?, ?, ?)",
        (ingest_plan_id, action_order, action_type),
    )
    return _lastrowid(cursor)


def test_migrate_applies_through_0013(db_path: Path) -> None:
    with connect(db_path) as connection:
        assert current_schema_version(connection) == 13
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"ingest_plans", "ingest_plan_actions"} <= tables


@pytest.mark.parametrize(
    "status",
    [
        "DRAFT", "READY_FOR_REVIEW", "REVIEW_REQUIRED", "BLOCKED", "APPROVED", "SUPERSEDED",
        "EXECUTING", "EXECUTED", "EXECUTION_FAILED", "RECOVERY_REQUIRED",
    ],
)
def test_plan_accepts_all_valid_statuses_including_execution_statuses(db_path: Path, status: str) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        plan_id = _insert_plan(connection, media_file_id=media_file_id, status=status)
        row = connection.execute("SELECT status FROM ingest_plans WHERE id = ?", (plan_id,)).fetchone()
        assert row["status"] == status


def test_plan_rejects_invalid_status(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_plan(connection, media_file_id=media_file_id, status="NOT_A_REAL_STATUS")


@pytest.mark.parametrize(
    "index_name",
    [
        "idx_ingest_plans_current_media_file",
        "idx_ingest_plans_media_file_id",
        "idx_ingest_plans_status",
        "idx_ingest_plans_destination_library",
    ],
)
def test_indexes_survive_the_rebuild(db_path: Path, index_name: str) -> None:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name = ?", (index_name,)
        ).fetchone()
        assert row is not None, f"missing index {index_name} after rebuild"


def test_rebuild_preserves_existing_rows_ids_and_child_actions(tmp_path: Path) -> None:
    """Seed data on the pre-0013 schema, then apply 0013, and confirm the
    plan keeps its original id (so ingest_plan_actions.ingest_plan_id
    values, untouched by this migration, remain valid) with every column
    value intact, and its action rows are still reachable."""
    path = tmp_path / "mams.db"
    migrate(path, _migrations_dir_through(tmp_path, *_THROUGH_0012))

    with connect(path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        plan_id = _insert_plan(connection, media_file_id=media_file_id, status="APPROVED")
        connection.execute(
            "UPDATE ingest_plans SET source_size_bytes = ?, source_mtime = ?, approved_by = ? WHERE id = ?",
            (123456, 1700000000.5, "MANUAL_CLI", plan_id),
        )
        action_id = _insert_action(connection, ingest_plan_id=plan_id, action_order=1, action_type="MOVE")
        before = dict(connection.execute("SELECT * FROM ingest_plans WHERE id = ?", (plan_id,)).fetchone())

    migrate(path, _migrations_dir_through(tmp_path, "0013"))

    with connect(path) as connection:
        assert current_schema_version(connection) == 13
        after_row = connection.execute("SELECT * FROM ingest_plans WHERE id = ?", (plan_id,)).fetchone()
        assert after_row is not None
        after = dict(after_row)
        assert after == before

        action_row = connection.execute(
            "SELECT * FROM ingest_plan_actions WHERE id = ?", (action_id,)
        ).fetchone()
        assert action_row is not None
        assert action_row["ingest_plan_id"] == plan_id


def test_no_dangling_foreign_keys_after_rebuild(tmp_path: Path) -> None:
    """The actual proof that the rename-based rebuild leaves
    ingest_plan_actions' FK to ingest_plans intact: PRAGMA foreign_key_check
    must return zero rows."""
    path = tmp_path / "mams.db"
    migrate(path, _migrations_dir_through(tmp_path, *_THROUGH_0012))

    with connect(path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        plan_id = _insert_plan(connection, media_file_id=media_file_id, status="APPROVED")
        _insert_action(connection, ingest_plan_id=plan_id, action_order=1, action_type="MOVE")
        _insert_action(connection, ingest_plan_id=plan_id, action_order=2, action_type="REFRESH_INVENTORY")

    migrate(path, _migrations_dir_through(tmp_path, "0013"))

    with connect(path) as connection:
        dangling = connection.execute("PRAGMA foreign_key_check").fetchall()
        assert dangling == []


def test_migrate_is_idempotent_after_0013(tmp_path: Path, db_path: Path) -> None:
    migrations_dir = _migrations_dir_through(tmp_path, *_THROUGH_0013)
    applied_again = migrate(db_path, migrations_dir)
    assert applied_again == []
