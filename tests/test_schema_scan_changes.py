"""Tests for the scan_changes schema (database/migrations/0003_scan_changes.sql).

Exercises the migration file directly against a temp database, same
approach as test_schema_inventory.py, so a constraint typo in the .sql
file itself gets caught.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mams.db import connect, current_schema_version, migrate

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"


def _migrations_dir_through(tmp_path: Path, *versions: str) -> Path:
    """A temp migrations dir containing only the named repo migration files.

    Scopes this file's tests to exactly the migrations it's testing (up
    through 0003), so a later migration (0004+) added elsewhere never
    changes the schema_version this file expects.
    """
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
    migrate(path, _migrations_dir_through(tmp_path, "0001", "0002", "0003"))
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


def _insert_media_file(connection: sqlite3.Connection, *, library_id: int, scan_id: int) -> int:
    cursor = connection.execute(
        """
        INSERT INTO media_files (
            library_id, absolute_path, relative_path, filename, extension,
            parent_directory, layout, size_bytes, first_seen_scan_id, last_seen_scan_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            library_id,
            "/Volumes/movies/Movie (2001).mkv",
            "Movie (2001).mkv",
            "Movie (2001).mkv",
            ".mkv",
            "/Volumes/movies",
            "movie_flat",
            1234,
            scan_id,
            scan_id,
        ),
    )
    return _lastrowid(cursor)


def _insert_scan_change(
    connection: sqlite3.Connection,
    *,
    scan_run_id: int,
    media_file_id: int | None,
    library_id: int | None,
    change_type: str = "ADDED",
    absolute_path: str = "/Volumes/movies/Movie (2001).mkv",
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO scan_changes (scan_run_id, media_file_id, library_id, change_type, absolute_path)
        VALUES (?, ?, ?, ?, ?)
        """,
        (scan_run_id, media_file_id, library_id, change_type, absolute_path),
    )
    return _lastrowid(cursor)


def test_migrate_applies_scan_changes_schema(db_path: Path) -> None:
    with connect(db_path) as connection:
        assert current_schema_version(connection) == 3
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "scan_changes" in tables


def test_scan_runs_gains_change_count_columns(db_path: Path) -> None:
    with connect(db_path) as connection:
        scan_id = _insert_scan_run(connection)
        row = connection.execute(
            "SELECT added_count, updated_count, missing_count, restored_count FROM scan_runs WHERE id = ?",
            (scan_id,),
        ).fetchone()
        assert tuple(row) == (None, None, None, None)


def test_scan_changes_rejects_invalid_change_type(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_scan_change(
                connection,
                scan_run_id=scan_id,
                media_file_id=media_file_id,
                library_id=library_id,
                change_type="RENAMED",
            )


@pytest.mark.parametrize("change_type", ["ADDED", "UPDATED", "MISSING", "RESTORED"])
def test_scan_changes_accepts_all_valid_change_types(db_path: Path, change_type: str) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        change_id = _insert_scan_change(
            connection,
            scan_run_id=scan_id,
            media_file_id=media_file_id,
            library_id=library_id,
            change_type=change_type,
        )
        row = connection.execute("SELECT change_type FROM scan_changes WHERE id = ?", (change_id,)).fetchone()
        assert row["change_type"] == change_type


def test_scan_changes_requires_scan_run_id(db_path: Path) -> None:
    with connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO scan_changes (media_file_id, library_id, change_type, absolute_path) "
                "VALUES (NULL, NULL, 'ADDED', '/x.mkv')"
            )


def test_scan_changes_rejects_unknown_scan_run_id(db_path: Path) -> None:
    with connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_scan_change(connection, scan_run_id=9999, media_file_id=None, library_id=None)


def test_deleting_scan_run_referenced_by_scan_changes_is_blocked(db_path: Path) -> None:
    """scan_runs is an append-only audit log; nothing deletes it, and the
    schema itself refuses to let a referenced row disappear."""
    with connect(db_path) as connection:
        scan_id = _insert_scan_run(connection)
        _insert_scan_change(connection, scan_run_id=scan_id, media_file_id=None, library_id=None)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM scan_runs WHERE id = ?", (scan_id,))

        remaining = connection.execute("SELECT COUNT(*) FROM scan_changes").fetchone()[0]
        assert remaining == 1


def test_deleting_media_file_sets_scan_changes_media_file_id_null_but_preserves_the_row(
    db_path: Path,
) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        change_id = _insert_scan_change(
            connection, scan_run_id=scan_id, media_file_id=media_file_id, library_id=library_id
        )

        connection.execute("DELETE FROM media_files WHERE id = ?", (media_file_id,))

        row = connection.execute("SELECT * FROM scan_changes WHERE id = ?", (change_id,)).fetchone()
        assert row is not None
        assert row["media_file_id"] is None
        assert row["absolute_path"] == "/Volumes/movies/Movie (2001).mkv"


def test_deleting_library_sets_scan_changes_library_id_null_but_preserves_the_row(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        change_id = _insert_scan_change(
            connection, scan_run_id=scan_id, media_file_id=None, library_id=library_id
        )

        connection.execute("DELETE FROM libraries WHERE id = ?", (library_id,))

        row = connection.execute("SELECT * FROM scan_changes WHERE id = ?", (change_id,)).fetchone()
        assert row is not None
        assert row["library_id"] is None


@pytest.mark.parametrize(
    "index_name",
    [
        "idx_scan_changes_scan_run_id",
        "idx_scan_changes_media_file_id",
        "idx_scan_changes_library_id",
        "idx_scan_changes_change_type",
        "idx_scan_changes_absolute_path",
    ],
)
def test_required_index_exists(db_path: Path, index_name: str) -> None:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name = ?", (index_name,)
        ).fetchone()
        assert row is not None, f"missing index {index_name}"


def test_migrate_from_scratch_applies_at_least_through_0003(tmp_path: Path) -> None:
    """Sanity-checks the real repo migrations dir specifically (unlike the
    other tests in this file, which use a scoped copy so they don't break
    when a later migration is added). Only asserts a prefix, so this stays
    true after a future 0004+ lands."""
    path = tmp_path / "mams.db"
    applied = migrate(path, REPO_MIGRATIONS_DIR)
    assert applied[:3] == [1, 2, 3]


def test_migrate_is_idempotent_after_0003(tmp_path: Path, db_path: Path) -> None:
    migrations_dir = _migrations_dir_through(tmp_path, "0001", "0002", "0003")
    applied_again = migrate(db_path, migrations_dir)
    assert applied_again == []
