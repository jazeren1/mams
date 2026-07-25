"""Tests for the identification_overrides schema
(database/migrations/0011_identification_overrides.sql): the overrides
table itself, its partial-unique-active-row index, cascade-on-media-file-
delete, and the new resolution_attempts.identification_override_id FK.
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
            tmp_path, "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010", "0011"
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
    connection: sqlite3.Connection, *, library_id: int, scan_id: int, name: str = "Alien.mkv"
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


def _insert_override(
    connection: sqlite3.Connection,
    *,
    media_file_id: int,
    candidate_type: str = "MOVIE",
    title: str | None = "Alien",
    cleared_at: str | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO identification_overrides (media_file_id, candidate_type, title, cleared_at)
        VALUES (?, ?, ?, ?)
        """,
        (media_file_id, candidate_type, title, cleared_at),
    )
    return _lastrowid(cursor)


def _insert_attempt(connection: sqlite3.Connection, *, media_file_id: int, identification_override_id: int | None = None) -> int:
    cursor = connection.execute(
        """
        INSERT INTO resolution_attempts (media_file_id, provider, status, algorithm_version, identification_override_id)
        VALUES (?, 'TMDB', 'RESOLVED', 1, ?)
        """,
        (media_file_id, identification_override_id),
    )
    return _lastrowid(cursor)


def test_migrate_applies_overrides_schema(db_path: Path) -> None:
    with connect(db_path) as connection:
        assert current_schema_version(connection) == 11
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "identification_overrides" in tables
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(resolution_attempts)")}
        assert "identification_override_id" in columns


@pytest.mark.parametrize("candidate_type", ["MOVIE", "EPISODE"])
def test_accepts_valid_candidate_types(db_path: Path, candidate_type: str) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        override_id = _insert_override(connection, media_file_id=media_file_id, candidate_type=candidate_type)
        row = connection.execute("SELECT candidate_type FROM identification_overrides WHERE id = ?", (override_id,)).fetchone()
        assert row["candidate_type"] == candidate_type


def test_rejects_invalid_candidate_type(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_override(connection, media_file_id=media_file_id, candidate_type="SPECIAL")


def test_at_most_one_active_override_per_media_file(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        _insert_override(connection, media_file_id=media_file_id)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_override(connection, media_file_id=media_file_id)


def test_a_second_active_override_is_allowed_once_the_first_is_cleared(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        _insert_override(connection, media_file_id=media_file_id, cleared_at="2026-07-24 00:00:00")
        second_id = _insert_override(connection, media_file_id=media_file_id)
        assert second_id is not None


def test_cleared_overrides_for_different_files_do_not_conflict(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        file_a = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
        file_b = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="B.mkv")
        _insert_override(connection, media_file_id=file_a)
        second_id = _insert_override(connection, media_file_id=file_b)
        assert second_id is not None


def test_override_cascades_on_media_file_delete(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        override_id = _insert_override(connection, media_file_id=media_file_id)
        connection.execute("DELETE FROM media_files WHERE id = ?", (media_file_id,))
        row = connection.execute("SELECT * FROM identification_overrides WHERE id = ?", (override_id,)).fetchone()
        assert row is None


def test_unknown_media_file_id_rejected_by_fk(db_path: Path) -> None:
    with connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_override(connection, media_file_id=999999)


# --- resolution_attempts.identification_override_id --------------------------


def test_resolution_attempt_records_override_id(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        override_id = _insert_override(connection, media_file_id=media_file_id)
        attempt_id = _insert_attempt(connection, media_file_id=media_file_id, identification_override_id=override_id)
        row = connection.execute(
            "SELECT identification_override_id FROM resolution_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        assert row["identification_override_id"] == override_id


def test_resolution_attempt_override_id_defaults_to_null(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        attempt_id = _insert_attempt(connection, media_file_id=media_file_id)
        row = connection.execute(
            "SELECT identification_override_id FROM resolution_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        assert row["identification_override_id"] is None


def test_resolution_attempt_override_id_survives_override_delete_as_null(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        override_id = _insert_override(connection, media_file_id=media_file_id)
        attempt_id = _insert_attempt(connection, media_file_id=media_file_id, identification_override_id=override_id)
        connection.execute("DELETE FROM identification_overrides WHERE id = ?", (override_id,))
        row = connection.execute(
            "SELECT identification_override_id FROM resolution_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        assert row["identification_override_id"] is None
