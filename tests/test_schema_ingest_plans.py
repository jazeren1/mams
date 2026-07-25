"""Tests for the ingest plan schema (database/migrations/0009_ingest_plans.sql):
ingest_plans, ingest_plan_actions.
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
        _migrations_dir_through(
            tmp_path, "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009"
        ),
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


def test_migrate_applies_ingest_plan_schema(db_path: Path) -> None:
    with connect(db_path) as connection:
        assert current_schema_version(connection) == 9
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"ingest_plans", "ingest_plan_actions"} <= tables


@pytest.mark.parametrize(
    "status", ["DRAFT", "READY_FOR_REVIEW", "REVIEW_REQUIRED", "BLOCKED", "APPROVED", "SUPERSEDED"]
)
def test_plan_accepts_all_valid_statuses(db_path: Path, status: str) -> None:
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
            _insert_plan(connection, media_file_id=media_file_id, status="EXECUTED")


def test_plan_requires_media_file_id_and_source_path(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO ingest_plans (status, source_path) VALUES ('DRAFT', '/x')")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO ingest_plans (media_file_id, status) VALUES (?, 'DRAFT')", (media_file_id,)
            )


def test_plan_defaults(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        plan_id = _insert_plan(connection, media_file_id=media_file_id)
        row = connection.execute("SELECT * FROM ingest_plans WHERE id = ?", (plan_id,)).fetchone()
        assert row["plan_version"] == 1
        assert row["approved_at"] is None
        assert row["executed_at"] is None
        assert row["created_at"] is not None
        assert row["updated_at"] is not None


def test_at_most_one_current_plan_per_media_file(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        _insert_plan(connection, media_file_id=media_file_id, status="DRAFT")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_plan(connection, media_file_id=media_file_id, status="BLOCKED")


def test_superseded_plan_does_not_block_a_new_current_plan(db_path: Path) -> None:
    """An APPROVED plan whose content changes is marked SUPERSEDED, then a
    new current plan is inserted -- the partial unique index only
    constrains non-SUPERSEDED rows."""
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        first_id = _insert_plan(connection, media_file_id=media_file_id, status="APPROVED")

        connection.execute("UPDATE ingest_plans SET status = 'SUPERSEDED' WHERE id = ?", (first_id,))
        second_id = _insert_plan(connection, media_file_id=media_file_id, status="DRAFT")

        count = connection.execute("SELECT COUNT(*) FROM ingest_plans").fetchone()[0]
        assert count == 2
        current = connection.execute(
            "SELECT id FROM ingest_plans WHERE status != 'SUPERSEDED'"
        ).fetchone()
        assert current["id"] == second_id


def test_deleting_media_file_cascades_to_plan(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        plan_id = _insert_plan(connection, media_file_id=media_file_id)

        connection.execute("DELETE FROM media_files WHERE id = ?", (media_file_id,))

        row = connection.execute("SELECT * FROM ingest_plans WHERE id = ?", (plan_id,)).fetchone()
        assert row is None


# --- ingest_plan_actions -------------------------------------------------------


@pytest.mark.parametrize(
    "action_type",
    [
        "VALIDATE_SOURCE",
        "VERIFY_MEDIA",
        "CREATE_DIRECTORY",
        "RENAME",
        "MOVE",
        "REFRESH_INVENTORY",
        "REQUEST_PLEX_REFRESH",
    ],
)
def test_action_accepts_all_valid_types(db_path: Path, action_type: str) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        plan_id = _insert_plan(connection, media_file_id=media_file_id)
        action_id = _insert_action(connection, ingest_plan_id=plan_id, action_type=action_type)
        row = connection.execute(
            "SELECT action_type FROM ingest_plan_actions WHERE id = ?", (action_id,)
        ).fetchone()
        assert row["action_type"] == action_type


def test_action_rejects_invalid_type(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        plan_id = _insert_plan(connection, media_file_id=media_file_id)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_action(connection, ingest_plan_id=plan_id, action_type="DELETE")


def test_action_order_must_be_positive(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        plan_id = _insert_plan(connection, media_file_id=media_file_id)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_action(connection, ingest_plan_id=plan_id, action_order=0)


def test_uniqueness_rejects_duplicate_action_order_within_plan(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        plan_id = _insert_plan(connection, media_file_id=media_file_id)
        _insert_action(connection, ingest_plan_id=plan_id, action_order=1, action_type="VALIDATE_SOURCE")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_action(connection, ingest_plan_id=plan_id, action_order=1, action_type="VERIFY_MEDIA")


def test_uniqueness_allows_same_order_across_different_plans(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id_1 = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="One.mkv")
        media_file_id_2 = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="Two.mkv")
        plan_id_1 = _insert_plan(connection, media_file_id=media_file_id_1)
        plan_id_2 = _insert_plan(connection, media_file_id=media_file_id_2)
        _insert_action(connection, ingest_plan_id=plan_id_1, action_order=1)
        _insert_action(connection, ingest_plan_id=plan_id_2, action_order=1)
        count = connection.execute("SELECT COUNT(*) FROM ingest_plan_actions").fetchone()[0]
        assert count == 2


def test_deleting_plan_cascades_to_actions(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        plan_id = _insert_plan(connection, media_file_id=media_file_id)
        action_id = _insert_action(connection, ingest_plan_id=plan_id)

        connection.execute("DELETE FROM ingest_plans WHERE id = ?", (plan_id,))

        row = connection.execute("SELECT * FROM ingest_plan_actions WHERE id = ?", (action_id,)).fetchone()
        assert row is None


@pytest.mark.parametrize(
    "index_name",
    [
        "idx_ingest_plans_current_media_file",
        "idx_ingest_plans_media_file_id",
        "idx_ingest_plans_status",
        "idx_ingest_plans_destination_library",
        "idx_ingest_plan_actions_plan_order",
        "idx_ingest_plan_actions_plan_id",
    ],
)
def test_required_index_exists(db_path: Path, index_name: str) -> None:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name = ?", (index_name,)
        ).fetchone()
        assert row is not None, f"missing index {index_name}"


def test_migrate_from_scratch_applies_at_least_through_0009(tmp_path: Path) -> None:
    path = tmp_path / "mams.db"
    applied = migrate(path, REPO_MIGRATIONS_DIR)
    assert applied[:9] == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_migrate_is_idempotent_after_0009(tmp_path: Path, db_path: Path) -> None:
    migrations_dir = _migrations_dir_through(
        tmp_path, "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009"
    )
    applied_again = migrate(db_path, migrations_dir)
    assert applied_again == []
