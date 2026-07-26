"""Tests for inventory_repository.relocate_media_file(): the Milestone 8
targeted, single-file canonical-inventory update used after a successful
transfer -- deliberately never a category walk.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from mams import inventory_repository as repo
from mams.db import connect, migrate
from mams.mediainfo import AudioTrack, MediaInfo, VideoTrack

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"


@pytest.fixture()
def connection(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "mams.db"
    migrate(db_path, REPO_MIGRATIONS_DIR)
    conn = connect(db_path)
    yield conn
    conn.close()


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _insert_library(connection: sqlite3.Connection, category: str) -> int:
    row = connection.execute("SELECT id FROM libraries WHERE category = ?", (category,)).fetchone()
    if row is not None:
        return int(row["id"])
    return _lastrowid(
        connection.execute("INSERT INTO libraries (category, root_path) VALUES (?, ?)", (category, f"/{category}"))
    )


def _insert_scan_run(connection: sqlite3.Connection) -> int:
    return _lastrowid(connection.execute("INSERT INTO scan_runs DEFAULT VALUES"))


def _insert_media_file(
    connection: sqlite3.Connection,
    *,
    library_id: int,
    scan_id: int,
    absolute_path: str = "/Incoming/Alien.mkv",
    state: str = "ACTIVE",
) -> int:
    return _lastrowid(
        connection.execute(
            """
            INSERT INTO media_files (
                library_id, absolute_path, relative_path, filename, extension,
                parent_directory, layout, size_bytes, state, first_seen_scan_id, last_seen_scan_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                library_id,
                absolute_path,
                Path(absolute_path).name,
                Path(absolute_path).name,
                Path(absolute_path).suffix,
                str(Path(absolute_path).parent),
                "unknown",
                1234,
                state,
                scan_id,
                scan_id,
            ),
        )
    )


def _sample_media_info() -> MediaInfo:
    return MediaInfo(
        container="Matroska",
        duration_seconds=6000.0,
        overall_bitrate=5_000_000,
        video_tracks=(
            VideoTrack(
                codec="HEVC", width=1920, height=1080, aspect_ratio="16:9",
                frame_rate=23.976, hdr_format=None, bit_depth=8, scan_type="Progressive",
            ),
        ),
        audio_tracks=(AudioTrack(codec="AC3", language="eng", channels=6, bitrate=640_000, default=True),),
        subtitle_tracks=(),
    )


def test_relocate_preserves_media_file_id_and_first_seen_scan_id(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection, "incoming")
    first_scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=first_scan_id)

    destination_library_id = _insert_library(connection, "movies")
    relocate_scan_id = _insert_scan_run(connection)
    with connection:
        repo.relocate_media_file(
            connection,
            media_file_id=media_file_id,
            new_library_id=destination_library_id,
            new_absolute_path="/NAS/Movies/Alien (1979)/Alien (1979).mkv",
            new_relative_path="Alien (1979)/Alien (1979).mkv",
            new_filename="Alien (1979).mkv",
            new_extension=".mkv",
            new_parent_directory="/NAS/Movies/Alien (1979)",
            new_layout="movie_folder",
            new_size_bytes=999_999,
            new_mtime=1700000000.0,
            scan_run_id=relocate_scan_id,
            media_info=_sample_media_info(),
            media_info_error=None,
        )

    row = connection.execute("SELECT * FROM media_files WHERE id = ?", (media_file_id,)).fetchone()
    assert row["id"] == media_file_id
    assert row["first_seen_scan_id"] == first_scan_id
    assert row["last_seen_scan_id"] == relocate_scan_id
    assert row["absolute_path"] == "/NAS/Movies/Alien (1979)/Alien (1979).mkv"
    assert row["library_id"] == destination_library_id
    assert row["state"] == "ACTIVE"
    assert row["size_bytes"] == 999_999
    assert row["container"] == "Matroska"
    assert row["duration_seconds"] == 6000.0


def test_relocate_records_exactly_one_updated_change_event_with_previous_path(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection, "incoming")
    first_scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=first_scan_id)

    relocate_scan_id = _insert_scan_run(connection)
    with connection:
        repo.relocate_media_file(
            connection,
            media_file_id=media_file_id,
            new_library_id=library_id,
            new_absolute_path="/NAS/Movies/Alien (1979)/Alien (1979).mkv",
            new_relative_path="Alien (1979)/Alien (1979).mkv",
            new_filename="Alien (1979).mkv",
            new_extension=".mkv",
            new_parent_directory="/NAS/Movies/Alien (1979)",
            new_layout="movie_folder",
            new_size_bytes=999_999,
            new_mtime=1700000000.0,
            scan_run_id=relocate_scan_id,
            media_info=None,
            media_info_error=None,
        )

    changes = connection.execute(
        "SELECT * FROM scan_changes WHERE media_file_id = ? AND scan_run_id = ?", (media_file_id, relocate_scan_id)
    ).fetchall()
    assert len(changes) == 1
    assert changes[0]["change_type"] == "UPDATED"
    assert changes[0]["previous_absolute_path"] == "/Incoming/Alien.mkv"
    assert changes[0]["absolute_path"] == "/NAS/Movies/Alien (1979)/Alien (1979).mkv"


def test_relocate_with_media_info_error_preserves_last_successful_metadata(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection, "incoming")
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
    connection.execute(
        "UPDATE media_files SET container = 'Matroska', duration_seconds = 6000.0 WHERE id = ?", (media_file_id,)
    )

    relocate_scan_id = _insert_scan_run(connection)
    with connection:
        repo.relocate_media_file(
            connection,
            media_file_id=media_file_id,
            new_library_id=library_id,
            new_absolute_path="/NAS/Movies/Alien (1979)/Alien (1979).mkv",
            new_relative_path="Alien (1979)/Alien (1979).mkv",
            new_filename="Alien (1979).mkv",
            new_extension=".mkv",
            new_parent_directory="/NAS/Movies/Alien (1979)",
            new_layout="movie_folder",
            new_size_bytes=999_999,
            new_mtime=1700000000.0,
            scan_run_id=relocate_scan_id,
            media_info=None,
            media_info_error="probe timed out",
        )

    row = connection.execute("SELECT * FROM media_files WHERE id = ?", (media_file_id,)).fetchone()
    assert row["media_info_error"] == "probe timed out"
    assert row["container"] == "Matroska"
    assert row["duration_seconds"] == 6000.0


def test_relocate_reactivates_a_missing_source_row(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection, "incoming")
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, state="MISSING")

    relocate_scan_id = _insert_scan_run(connection)
    with connection:
        repo.relocate_media_file(
            connection,
            media_file_id=media_file_id,
            new_library_id=library_id,
            new_absolute_path="/NAS/Movies/Alien (1979)/Alien (1979).mkv",
            new_relative_path="Alien (1979)/Alien (1979).mkv",
            new_filename="Alien (1979).mkv",
            new_extension=".mkv",
            new_parent_directory="/NAS/Movies/Alien (1979)",
            new_layout="movie_folder",
            new_size_bytes=999_999,
            new_mtime=1700000000.0,
            scan_run_id=relocate_scan_id,
            media_info=None,
            media_info_error=None,
        )

    row = connection.execute("SELECT * FROM media_files WHERE id = ?", (media_file_id,)).fetchone()
    assert row["state"] == "ACTIVE"
    assert row["missing_since_scan_id"] is None


def test_relocate_unknown_media_file_id_raises(connection: sqlite3.Connection) -> None:
    scan_id = _insert_scan_run(connection)
    with pytest.raises(ValueError):
        repo.relocate_media_file(
            connection,
            media_file_id=999_999,
            new_library_id=1,
            new_absolute_path="/x",
            new_relative_path="x",
            new_filename="x",
            new_extension=".mkv",
            new_parent_directory="/",
            new_layout="unknown",
            new_size_bytes=1,
            new_mtime=1.0,
            scan_run_id=scan_id,
            media_info=None,
            media_info_error=None,
        )


def test_relocate_never_walks_a_directory(connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """Proxy proof that relocate_media_file is a single-row update, not a
    category walk: any attempt to list a directory or os.walk would
    raise, and relocate_media_file must still succeed."""

    def _forbidden_iterdir(self: Path) -> None:
        raise AssertionError("relocate_media_file must never walk a directory")

    def _forbidden_walk(*args: object, **kwargs: object) -> None:
        raise AssertionError("relocate_media_file must never walk a directory")

    monkeypatch.setattr(Path, "iterdir", _forbidden_iterdir)
    monkeypatch.setattr(os, "walk", _forbidden_walk)

    library_id = _insert_library(connection, "incoming")
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)

    relocate_scan_id = _insert_scan_run(connection)
    with connection:
        repo.relocate_media_file(
            connection,
            media_file_id=media_file_id,
            new_library_id=library_id,
            new_absolute_path="/NAS/Movies/Alien (1979)/Alien (1979).mkv",
            new_relative_path="Alien (1979)/Alien (1979).mkv",
            new_filename="Alien (1979).mkv",
            new_extension=".mkv",
            new_parent_directory="/NAS/Movies/Alien (1979)",
            new_layout="movie_folder",
            new_size_bytes=999_999,
            new_mtime=1700000000.0,
            scan_run_id=relocate_scan_id,
            media_info=None,
            media_info_error=None,
        )

    row = connection.execute("SELECT * FROM media_files WHERE id = ?", (media_file_id,)).fetchone()
    assert row["absolute_path"] == "/NAS/Movies/Alien (1979)/Alien (1979).mkv"


def test_existing_reconcile_change_callers_are_unaffected_by_previous_absolute_path_column(
    connection: sqlite3.Connection,
) -> None:
    """A regular scan-driven change event (not a relocation) must still
    leave previous_absolute_path NULL -- the new kwarg is opt-in."""
    library_id = _insert_library(connection, "incoming")
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
    with connection:
        repo._record_change(
            connection,
            scan_run_id=scan_id,
            media_file_id=media_file_id,
            library_id=library_id,
            change_type="ADDED",
            absolute_path="/Incoming/Alien.mkv",
        )
    row = connection.execute(
        "SELECT * FROM scan_changes WHERE media_file_id = ? ORDER BY id DESC LIMIT 1", (media_file_id,)
    ).fetchone()
    assert row["previous_absolute_path"] is None
