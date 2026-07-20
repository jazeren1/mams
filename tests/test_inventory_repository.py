"""Tests for the inventory database write path (inventory_repository.py).

These build `CategoryScan`/`ScannedFile`/`InventoryReport` objects directly
rather than running a real filesystem walk, to unit-test persistence in
isolation from `inventory.py`'s discovery logic. Because the repository
independently `stat()`s each file to capture `mtime` at persist time (see
`_stat_mtime`), every `ScannedFile.absolute_path` used here must point at a
real file under `tmp_path`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mams import inventory_repository as repo
from mams.db import connect, migrate
from mams.inventory import CategoryScan, InventoryReport, Layout, ScannedFile
from mams.mediainfo import AudioTrack, MediaInfo, SubtitleTrack, VideoTrack

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"


@pytest.fixture()
def connection(tmp_path: Path):
    db_path = tmp_path / "mams.db"
    migrate(db_path, REPO_MIGRATIONS_DIR)
    conn = connect(db_path)
    yield conn
    conn.close()


def _touch(path: Path, size: int = 10) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)


def _scanned_file(
    tmp_path: Path,
    name: str = "Movie (2001).mkv",
    *,
    category: str = "movies",
    size: int = 10,
    layout: Layout = Layout.MOVIE_FLAT,
    media_info: MediaInfo | None = None,
    media_info_error: str | None = None,
) -> ScannedFile:
    path = tmp_path / name
    _touch(path, size=size)
    return ScannedFile(
        category=category,
        absolute_path=str(path),
        relative_path=name,
        filename=name,
        extension=path.suffix.lower(),
        parent_directory=str(path.parent),
        size_bytes=size,
        layout=layout,
        media_info=media_info,
        media_info_error=media_info_error,
    )


def _category_scan(category: str, root: Path, files: list[ScannedFile]) -> CategoryScan:
    return CategoryScan(category=category, root_path=str(root), exists=True, files=tuple(files))


def _sample_media_info(video_codec: str = "HEVC") -> MediaInfo:
    return MediaInfo(
        container="Matroska",
        duration_seconds=120.0,
        overall_bitrate=5_000_000,
        video_tracks=(
            VideoTrack(
                codec=video_codec,
                width=1920,
                height=1080,
                aspect_ratio="16:9",
                frame_rate=23.976,
                hdr_format=None,
                bit_depth=8,
                scan_type="Progressive",
            ),
        ),
        audio_tracks=(
            AudioTrack(codec="AC3", language="eng", channels=6, bitrate=640_000, default=True),
        ),
        subtitle_tracks=(
            SubtitleTrack(language="eng", default=False, forced=False),
        ),
    )


# --- sync_libraries -----------------------------------------------------


def test_sync_libraries_inserts_new_categories(connection: sqlite3.Connection) -> None:
    library_ids = repo.sync_libraries(connection, {"movies": "/Volumes/Movies", "tv": "/Volumes/TV"})

    assert set(library_ids) == {"movies", "tv"}
    rows = {
        row["category"]: row["root_path"]
        for row in connection.execute("SELECT category, root_path FROM libraries")
    }
    assert rows == {"movies": "/Volumes/Movies", "tv": "/Volumes/TV"}


def test_sync_libraries_updates_root_path_when_changed(connection: sqlite3.Connection) -> None:
    repo.sync_libraries(connection, {"movies": "/Volumes/Movies"})
    before = connection.execute("SELECT updated_at FROM libraries WHERE category = 'movies'").fetchone()

    repo.sync_libraries(connection, {"movies": "/Volumes/Movies-Renamed"})

    row = connection.execute("SELECT root_path, updated_at FROM libraries WHERE category = 'movies'").fetchone()
    assert row["root_path"] == "/Volumes/Movies-Renamed"
    assert row["updated_at"] >= before["updated_at"]


def test_sync_libraries_is_a_noop_when_root_path_unchanged(connection: sqlite3.Connection) -> None:
    repo.sync_libraries(connection, {"movies": "/Volumes/Movies"})
    first_ids = repo.sync_libraries(connection, {"movies": "/Volumes/Movies"})
    second_ids = repo.sync_libraries(connection, {"movies": "/Volumes/Movies"})

    assert first_ids == second_ids
    count = connection.execute("SELECT COUNT(*) FROM libraries").fetchone()[0]
    assert count == 1


# --- new file insert / existing file update / first_seen preserved ------


def test_persist_scan_inserts_new_file(connection: sqlite3.Connection, tmp_path: Path) -> None:
    scanned = _scanned_file(tmp_path)
    report = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned]),))

    scan_run_id = repo.persist_scan(
        connection, report, {"movies": str(tmp_path)}, metadata_enabled=False, mediainfo_version=None
    )

    row = connection.execute("SELECT * FROM media_files WHERE absolute_path = ?", (scanned.absolute_path,)).fetchone()
    assert row is not None
    assert row["state"] == "ACTIVE"
    assert row["size_bytes"] == 10
    assert row["first_seen_scan_id"] == scan_run_id
    assert row["last_seen_scan_id"] == scan_run_id
    assert row["mtime"] is not None


def test_persist_scan_updates_existing_file_and_preserves_first_seen(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    scanned = _scanned_file(tmp_path, size=10)
    report1 = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned]),))
    first_scan_run_id = repo.persist_scan(
        connection, report1, {"movies": str(tmp_path)}, metadata_enabled=False, mediainfo_version=None
    )

    _touch(Path(scanned.absolute_path), size=20)
    updated = ScannedFile(
        category=scanned.category,
        absolute_path=scanned.absolute_path,
        relative_path=scanned.relative_path,
        filename=scanned.filename,
        extension=scanned.extension,
        parent_directory=scanned.parent_directory,
        size_bytes=20,
        layout=scanned.layout,
    )
    report2 = InventoryReport(categories=(_category_scan("movies", tmp_path, [updated]),))
    second_scan_run_id = repo.persist_scan(
        connection, report2, {"movies": str(tmp_path)}, metadata_enabled=False, mediainfo_version=None
    )

    row = connection.execute("SELECT * FROM media_files WHERE absolute_path = ?", (scanned.absolute_path,)).fetchone()
    assert row["size_bytes"] == 20
    assert row["first_seen_scan_id"] == first_scan_run_id
    assert row["last_seen_scan_id"] == second_scan_run_id
    count = connection.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]
    assert count == 1


# --- missing / rediscovered ----------------------------------------------


def test_persist_scan_marks_file_missing_when_no_longer_discovered(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    scanned = _scanned_file(tmp_path)
    report1 = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned]),))
    repo.persist_scan(connection, report1, {"movies": str(tmp_path)}, metadata_enabled=False, mediainfo_version=None)

    report2 = InventoryReport(categories=(_category_scan("movies", tmp_path, []),))
    second_scan_run_id = repo.persist_scan(
        connection, report2, {"movies": str(tmp_path)}, metadata_enabled=False, mediainfo_version=None
    )

    row = connection.execute("SELECT * FROM media_files WHERE absolute_path = ?", (scanned.absolute_path,)).fetchone()
    assert row["state"] == "MISSING"
    assert row["missing_since_scan_id"] == second_scan_run_id


def test_persist_scan_rediscovered_file_becomes_active_and_clears_missing_since(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    scanned = _scanned_file(tmp_path)
    report1 = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned]),))
    repo.persist_scan(connection, report1, {"movies": str(tmp_path)}, metadata_enabled=False, mediainfo_version=None)

    report2 = InventoryReport(categories=(_category_scan("movies", tmp_path, []),))
    repo.persist_scan(connection, report2, {"movies": str(tmp_path)}, metadata_enabled=False, mediainfo_version=None)

    report3 = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned]),))
    third_scan_run_id = repo.persist_scan(
        connection, report3, {"movies": str(tmp_path)}, metadata_enabled=False, mediainfo_version=None
    )

    row = connection.execute("SELECT * FROM media_files WHERE absolute_path = ?", (scanned.absolute_path,)).fetchone()
    assert row["state"] == "ACTIVE"
    assert row["missing_since_scan_id"] is None
    assert row["last_seen_scan_id"] == third_scan_run_id


def test_persist_scan_does_not_mark_files_missing_for_a_category_that_does_not_exist(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    movies_root = tmp_path / "Movies"
    scanned = _scanned_file(movies_root)
    report1 = InventoryReport(categories=(_category_scan("movies", movies_root, [scanned]),))
    repo.persist_scan(
        connection, report1, {"movies": str(movies_root)}, metadata_enabled=False, mediainfo_version=None
    )

    missing_category_scan = CategoryScan(
        category="movies", root_path=str(movies_root), exists=False, files=()
    )
    report2 = InventoryReport(categories=(missing_category_scan,))
    repo.persist_scan(
        connection, report2, {"movies": str(movies_root)}, metadata_enabled=False, mediainfo_version=None
    )

    row = connection.execute("SELECT * FROM media_files WHERE absolute_path = ?", (scanned.absolute_path,)).fetchone()
    assert row["state"] == "ACTIVE"


# --- metadata preservation / replacement ---------------------------------


def test_no_metadata_scan_preserves_existing_metadata_and_tracks(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    media_info = _sample_media_info()
    scanned = _scanned_file(tmp_path, media_info=media_info)
    report1 = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned]),))
    repo.persist_scan(connection, report1, {"movies": str(tmp_path)}, metadata_enabled=True, mediainfo_version="v1")

    no_metadata_rescan = ScannedFile(
        category=scanned.category,
        absolute_path=scanned.absolute_path,
        relative_path=scanned.relative_path,
        filename=scanned.filename,
        extension=scanned.extension,
        parent_directory=scanned.parent_directory,
        size_bytes=scanned.size_bytes,
        layout=scanned.layout,
    )
    report2 = InventoryReport(categories=(_category_scan("movies", tmp_path, [no_metadata_rescan]),))
    repo.persist_scan(connection, report2, {"movies": str(tmp_path)}, metadata_enabled=False, mediainfo_version=None)

    row = connection.execute("SELECT * FROM media_files WHERE absolute_path = ?", (scanned.absolute_path,)).fetchone()
    assert row["container"] == "Matroska"
    assert row["duration_seconds"] == 120.0
    video_tracks = connection.execute("SELECT * FROM video_tracks WHERE media_file_id = ?", (row["id"],)).fetchall()
    assert len(video_tracks) == 1
    assert video_tracks[0]["codec"] == "HEVC"


def test_metadata_scan_replaces_tracks(connection: sqlite3.Connection, tmp_path: Path) -> None:
    first = _sample_media_info(video_codec="H.264")
    scanned = _scanned_file(tmp_path, media_info=first)
    report1 = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned]),))
    repo.persist_scan(connection, report1, {"movies": str(tmp_path)}, metadata_enabled=True, mediainfo_version="v1")

    second = _sample_media_info(video_codec="HEVC")
    rescanned = ScannedFile(
        category=scanned.category,
        absolute_path=scanned.absolute_path,
        relative_path=scanned.relative_path,
        filename=scanned.filename,
        extension=scanned.extension,
        parent_directory=scanned.parent_directory,
        size_bytes=scanned.size_bytes,
        layout=scanned.layout,
        media_info=second,
    )
    report2 = InventoryReport(categories=(_category_scan("movies", tmp_path, [rescanned]),))
    repo.persist_scan(connection, report2, {"movies": str(tmp_path)}, metadata_enabled=True, mediainfo_version="v2")

    media_file_id = connection.execute(
        "SELECT id FROM media_files WHERE absolute_path = ?", (scanned.absolute_path,)
    ).fetchone()["id"]
    video_tracks = connection.execute(
        "SELECT codec FROM video_tracks WHERE media_file_id = ?", (media_file_id,)
    ).fetchall()
    assert [row["codec"] for row in video_tracks] == ["HEVC"]


def test_failed_metadata_probe_preserves_last_successful_metadata_and_records_error(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    media_info = _sample_media_info()
    scanned = _scanned_file(tmp_path, media_info=media_info)
    report1 = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned]),))
    repo.persist_scan(connection, report1, {"movies": str(tmp_path)}, metadata_enabled=True, mediainfo_version="v1")

    failed_probe = ScannedFile(
        category=scanned.category,
        absolute_path=scanned.absolute_path,
        relative_path=scanned.relative_path,
        filename=scanned.filename,
        extension=scanned.extension,
        parent_directory=scanned.parent_directory,
        size_bytes=scanned.size_bytes,
        layout=scanned.layout,
        media_info=None,
        media_info_error="mediainfo timed out after 60s",
    )
    report2 = InventoryReport(categories=(_category_scan("movies", tmp_path, [failed_probe]),))
    repo.persist_scan(connection, report2, {"movies": str(tmp_path)}, metadata_enabled=True, mediainfo_version="v1")

    row = connection.execute("SELECT * FROM media_files WHERE absolute_path = ?", (scanned.absolute_path,)).fetchone()
    assert row["container"] == "Matroska"
    assert row["media_info_error"] == "mediainfo timed out after 60s"
    video_tracks = connection.execute("SELECT * FROM video_tracks WHERE media_file_id = ?", (row["id"],)).fetchall()
    assert len(video_tracks) == 1
    assert video_tracks[0]["codec"] == "HEVC"


# --- scan_runs bookkeeping -------------------------------------------------


def test_persist_scan_records_completed_scan_run_totals_and_status(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    scanned_a = _scanned_file(tmp_path, name="A (2001).mkv", size=10)
    scanned_b = _scanned_file(tmp_path, name="B (2002).mkv", size=20)
    report = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned_a, scanned_b]),))

    scan_run_id = repo.persist_scan(
        connection, report, {"movies": str(tmp_path)}, metadata_enabled=False, mediainfo_version=None
    )

    row = connection.execute("SELECT * FROM scan_runs WHERE id = ?", (scan_run_id,)).fetchone()
    assert row["status"] == "COMPLETE"
    assert row["file_count"] == 2
    assert row["total_size_bytes"] == 30
    assert row["completed_at"] is not None


def test_persist_scan_records_mediainfo_version(connection: sqlite3.Connection, tmp_path: Path) -> None:
    scanned = _scanned_file(tmp_path)
    report = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned]),))

    scan_run_id = repo.persist_scan(
        connection, report, {"movies": str(tmp_path)}, metadata_enabled=True, mediainfo_version="MediaInfoLib - v23.11"
    )

    row = connection.execute("SELECT mediainfo_version, metadata_enabled FROM scan_runs WHERE id = ?", (scan_run_id,)).fetchone()
    assert row["mediainfo_version"] == "MediaInfoLib - v23.11"
    assert row["metadata_enabled"] == 1


# --- failure / rollback ----------------------------------------------------


def test_persist_scan_rolls_back_partial_writes_and_marks_scan_run_failed(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    good = _scanned_file(tmp_path, name="Good (2001).mkv")
    # This file's absolute_path does not exist on disk, so the repository's
    # independent stat() for mtime raises, forcing a real (not mocked)
    # failure partway through reconciliation.
    vanished = ScannedFile(
        category="movies",
        absolute_path=str(tmp_path / "Vanished (2002).mkv"),
        relative_path="Vanished (2002).mkv",
        filename="Vanished (2002).mkv",
        extension=".mkv",
        parent_directory=str(tmp_path),
        size_bytes=10,
        layout=Layout.MOVIE_FLAT,
    )
    report = InventoryReport(categories=(_category_scan("movies", tmp_path, [good, vanished]),))

    with pytest.raises(FileNotFoundError):
        repo.persist_scan(
            connection, report, {"movies": str(tmp_path)}, metadata_enabled=False, mediainfo_version=None
        )

    count = connection.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]
    assert count == 0

    scan_run = connection.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert scan_run["status"] == "FAILED"
    assert scan_run["error_message"] is not None
    assert "Vanished" in scan_run["error_message"] or "No such file" in scan_run["error_message"]


def test_failed_scan_run_row_remains_recorded(connection: sqlite3.Connection, tmp_path: Path) -> None:
    vanished = ScannedFile(
        category="movies",
        absolute_path=str(tmp_path / "Vanished (2002).mkv"),
        relative_path="Vanished (2002).mkv",
        filename="Vanished (2002).mkv",
        extension=".mkv",
        parent_directory=str(tmp_path),
        size_bytes=10,
        layout=Layout.MOVIE_FLAT,
    )
    report = InventoryReport(categories=(_category_scan("movies", tmp_path, [vanished]),))

    with pytest.raises(FileNotFoundError):
        repo.persist_scan(
            connection, report, {"movies": str(tmp_path)}, metadata_enabled=False, mediainfo_version=None
        )

    rows = connection.execute("SELECT * FROM scan_runs").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "FAILED"


def test_nas_fixture_files_are_never_modified_by_persist_scan(connection: sqlite3.Connection, tmp_path: Path) -> None:
    scanned = _scanned_file(tmp_path)
    path = Path(scanned.absolute_path)
    before = (path.stat().st_size, path.stat().st_mtime_ns)

    report = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned]),))
    repo.persist_scan(connection, report, {"movies": str(tmp_path)}, metadata_enabled=False, mediainfo_version=None)

    after = (path.stat().st_size, path.stat().st_mtime_ns)
    assert before == after
