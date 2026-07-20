"""Tests for the inventory schema (database/migrations/0002_inventory.sql).

These exercise the migration file directly against a temp database rather
than mocking SQL, so a constraint typo in the .sql file itself gets caught.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mams.db import connect, current_schema_version, migrate

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"

INVENTORY_TABLES = {
    "libraries",
    "scan_runs",
    "media_files",
    "video_tracks",
    "audio_tracks",
    "subtitle_tracks",
}


def _migrations_dir_through(tmp_path: Path, *versions: str) -> Path:
    """A temp migrations dir containing only the named repo migration files.

    Scopes this file's tests to exactly the migrations it's testing (0001,
    0002), so a later migration (0003+) added elsewhere never changes the
    schema_version this file expects.
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
    migrate(path, _migrations_dir_through(tmp_path, "0001", "0002"))
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
    connection: sqlite3.Connection,
    *,
    library_id: int,
    scan_id: int,
    absolute_path: str = "/Volumes/movies/Movie (2001).mkv",
    layout: str = "movie_flat",
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO media_files (
            library_id, absolute_path, relative_path, filename, extension,
            parent_directory, layout, size_bytes, first_seen_scan_id, last_seen_scan_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            library_id,
            absolute_path,
            "Movie (2001).mkv",
            "Movie (2001).mkv",
            ".mkv",
            "/Volumes/movies",
            layout,
            1234,
            scan_id,
            scan_id,
        ),
    )
    return _lastrowid(cursor)


def test_migrate_applies_inventory_schema(db_path: Path) -> None:
    with connect(db_path) as connection:
        assert current_schema_version(connection) == 2
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert INVENTORY_TABLES <= tables


def test_libraries_category_is_unique(db_path: Path) -> None:
    with connect(db_path) as connection:
        _insert_library(connection, "movies")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_library(connection, "movies")


def test_media_files_absolute_path_is_unique(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_media_file(connection, library_id=library_id, scan_id=scan_id)


def test_media_files_rejects_invalid_layout(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_media_file(connection, library_id=library_id, scan_id=scan_id, layout="not_a_real_layout")


def test_media_files_state_defaults_to_active(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)

        row = connection.execute("SELECT state FROM media_files WHERE id = ?", (media_file_id,)).fetchone()
        assert row["state"] == "ACTIVE"


def test_media_files_rejects_invalid_state(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO media_files (
                    library_id, absolute_path, relative_path, filename, extension,
                    parent_directory, layout, size_bytes, state,
                    first_seen_scan_id, last_seen_scan_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    "DELETED",
                    scan_id,
                    scan_id,
                ),
            )


def test_media_files_rejects_unknown_library_id(db_path: Path) -> None:
    with connect(db_path) as connection:
        scan_id = _insert_scan_run(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_media_file(connection, library_id=9999, scan_id=scan_id)


def test_scan_runs_status_defaults_to_running_and_rejects_invalid_values(db_path: Path) -> None:
    with connect(db_path) as connection:
        scan_id = _insert_scan_run(connection)
        row = connection.execute("SELECT status FROM scan_runs WHERE id = ?", (scan_id,)).fetchone()
        assert row["status"] == "RUNNING"

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO scan_runs (status) VALUES ('BOGUS')")


def test_scan_runs_records_mediainfo_version(db_path: Path) -> None:
    with connect(db_path) as connection:
        connection.execute(
            "INSERT INTO scan_runs (metadata_enabled, mediainfo_version) VALUES (1, ?)", ("v23.11",)
        )
        row = connection.execute("SELECT mediainfo_version FROM scan_runs").fetchone()
        assert row["mediainfo_version"] == "v23.11"


def test_video_audio_subtitle_tracks_cascade_delete_with_media_file(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)

        connection.execute(
            "INSERT INTO video_tracks (media_file_id, track_index, codec) VALUES (?, 0, 'HEVC')",
            (media_file_id,),
        )
        connection.execute(
            "INSERT INTO audio_tracks (media_file_id, track_index, codec) VALUES (?, 0, 'AC3')",
            (media_file_id,),
        )
        connection.execute(
            "INSERT INTO subtitle_tracks (media_file_id, track_index, language) VALUES (?, 0, 'eng')",
            (media_file_id,),
        )

        connection.execute("DELETE FROM media_files WHERE id = ?", (media_file_id,))

        assert connection.execute("SELECT COUNT(*) FROM video_tracks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM audio_tracks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM subtitle_tracks").fetchone()[0] == 0


def test_video_tracks_rejects_unknown_media_file_id(db_path: Path) -> None:
    with connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO video_tracks (media_file_id, track_index, codec) VALUES (9999, 0, 'HEVC')"
            )


def test_audio_and_subtitle_track_flag_columns_default_to_false(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)

        connection.execute(
            "INSERT INTO audio_tracks (media_file_id, track_index, codec) VALUES (?, 0, 'AC3')",
            (media_file_id,),
        )
        connection.execute(
            "INSERT INTO subtitle_tracks (media_file_id, track_index, language) VALUES (?, 0, 'eng')",
            (media_file_id,),
        )

        audio_row = connection.execute("SELECT is_default FROM audio_tracks").fetchone()
        subtitle_row = connection.execute("SELECT is_default, is_forced FROM subtitle_tracks").fetchone()
        assert audio_row["is_default"] == 0
        assert subtitle_row["is_default"] == 0
        assert subtitle_row["is_forced"] == 0


@pytest.mark.parametrize(
    "index_name",
    [
        "idx_libraries_category",
        "idx_media_files_absolute_path",
        "idx_media_files_library_id",
        "idx_media_files_state",
        "idx_media_files_library_layout",
        "idx_media_files_last_seen",
        "idx_video_tracks_media_file_id",
        "idx_video_tracks_hdr",
        "idx_audio_tracks_media_file_id",
        "idx_audio_tracks_language",
        "idx_subtitle_tracks_media_file_id",
        "idx_subtitle_tracks_language",
    ],
)
def test_required_index_exists(db_path: Path, index_name: str) -> None:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name = ?", (index_name,)
        ).fetchone()
        assert row is not None, f"missing index {index_name}"


def test_migrate_from_version_1_to_2_is_idempotent_on_rerun(tmp_path: Path) -> None:
    path = tmp_path / "mams.db"
    migrations_dir = _migrations_dir_through(tmp_path, "0001", "0002")
    first = migrate(path, migrations_dir)
    second = migrate(path, migrations_dir)

    assert first == [1, 2]
    assert second == []
