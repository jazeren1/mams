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


# --- read_inventory_report ------------------------------------------------


def test_read_inventory_report_on_empty_database(connection: sqlite3.Connection, tmp_path: Path) -> None:
    empty_root = tmp_path / "Movies"
    empty_root.mkdir()

    result = repo.read_inventory_report(connection, {"movies": str(empty_root)})

    assert len(result.categories) == 1
    category = result.categories[0]
    assert category.category == "movies"
    assert category.exists is True
    assert category.files == ()
    assert result.file_count == 0


def test_read_inventory_report_missing_root_reports_not_exists(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    missing_root = tmp_path / "does-not-exist"

    result = repo.read_inventory_report(connection, {"movies": str(missing_root)})

    assert result.categories[0].exists is False
    assert result.categories[0].files == ()


def test_read_inventory_report_one_category_one_file(connection: sqlite3.Connection, tmp_path: Path) -> None:
    scanned = _scanned_file(tmp_path)
    report = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned]),))
    repo.persist_scan(connection, report, {"movies": str(tmp_path)}, metadata_enabled=False, mediainfo_version=None)

    result = repo.read_inventory_report(connection, {"movies": str(tmp_path)})

    assert result.file_count == 1
    file = result.categories[0].files[0]
    assert file.filename == scanned.filename
    assert file.category == "movies"
    assert file.size_bytes == scanned.size_bytes
    assert file.layout == Layout.MOVIE_FLAT


def test_read_inventory_report_preserves_configured_category_order(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    movies_root = tmp_path / "Movies"
    tv_root = tmp_path / "TV"
    fitness_root = tmp_path / "Fitness"
    for root in (movies_root, tv_root, fitness_root):
        root.mkdir()

    # Sync libraries in a different order than the categories dict below, to
    # prove output order comes from the categories dict, not library
    # insertion/id order.
    repo.sync_libraries(connection, {"tv": str(tv_root), "movies": str(movies_root), "fitness": str(fitness_root)})
    categories = {"fitness": str(fitness_root), "movies": str(movies_root), "tv": str(tv_root)}

    result = repo.read_inventory_report(connection, categories)

    assert [c.category for c in result.categories] == ["fitness", "movies", "tv"]


def test_read_inventory_report_file_ordering_matches_scanner(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    files = [
        _scanned_file(tmp_path, name="Zebra (2001).mkv"),
        _scanned_file(tmp_path, name="Apple (2002).mkv"),
        _scanned_file(tmp_path, name="Mango (2003).mkv"),
    ]
    report = InventoryReport(categories=(_category_scan("movies", tmp_path, files),))
    repo.persist_scan(connection, report, {"movies": str(tmp_path)}, metadata_enabled=False, mediainfo_version=None)

    result = repo.read_inventory_report(connection, {"movies": str(tmp_path)})

    filenames = [f.filename for f in result.categories[0].files]
    assert filenames == ["Apple (2002).mkv", "Mango (2003).mkv", "Zebra (2001).mkv"]
    assert filenames == sorted(filenames)


def test_read_inventory_report_excludes_missing_files(connection: sqlite3.Connection, tmp_path: Path) -> None:
    scanned = _scanned_file(tmp_path)
    report1 = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned]),))
    repo.persist_scan(connection, report1, {"movies": str(tmp_path)}, metadata_enabled=False, mediainfo_version=None)

    report2 = InventoryReport(categories=(_category_scan("movies", tmp_path, []),))
    repo.persist_scan(connection, report2, {"movies": str(tmp_path)}, metadata_enabled=False, mediainfo_version=None)

    result = repo.read_inventory_report(connection, {"movies": str(tmp_path)})

    assert result.file_count == 0
    row = connection.execute(
        "SELECT state FROM media_files WHERE absolute_path = ?", (scanned.absolute_path,)
    ).fetchone()
    assert row["state"] == "MISSING"


def test_read_inventory_report_reconstructs_general_media_info_fields(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    media_info = _sample_media_info()
    scanned = _scanned_file(tmp_path, media_info=media_info)
    report = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned]),))
    repo.persist_scan(connection, report, {"movies": str(tmp_path)}, metadata_enabled=True, mediainfo_version="v1")

    result = repo.read_inventory_report(connection, {"movies": str(tmp_path)})

    file = result.categories[0].files[0]
    assert file.media_info is not None
    assert file.media_info.container == "Matroska"
    assert file.media_info.duration_seconds == 120.0
    assert file.media_info.overall_bitrate == 5_000_000
    assert file.media_info_error is None


def test_read_inventory_report_reconstructs_tracks_in_track_index_order(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    media_info = MediaInfo(
        container="Matroska",
        duration_seconds=100.0,
        overall_bitrate=1000,
        video_tracks=(
            VideoTrack(
                codec="H.264", width=1280, height=720, aspect_ratio="16:9",
                frame_rate=25.0, hdr_format=None, bit_depth=8, scan_type="Progressive",
            ),
            VideoTrack(
                codec="HEVC", width=1920, height=1080, aspect_ratio="16:9",
                frame_rate=23.976, hdr_format="HDR10", bit_depth=10, scan_type="Progressive",
            ),
        ),
        audio_tracks=(
            AudioTrack(codec="AAC", language="eng", channels=2, bitrate=128_000, default=True),
            AudioTrack(codec="AC3", language="jpn", channels=6, bitrate=640_000, default=False),
        ),
        subtitle_tracks=(
            SubtitleTrack(language="eng", default=True, forced=False),
            SubtitleTrack(language="spa", default=False, forced=True),
        ),
    )
    scanned = _scanned_file(tmp_path, media_info=media_info)
    report = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned]),))
    repo.persist_scan(connection, report, {"movies": str(tmp_path)}, metadata_enabled=True, mediainfo_version="v1")

    result = repo.read_inventory_report(connection, {"movies": str(tmp_path)})

    reconstructed = result.categories[0].files[0].media_info
    assert reconstructed is not None
    assert [t.codec for t in reconstructed.video_tracks] == ["H.264", "HEVC"]
    assert [t.language for t in reconstructed.audio_tracks] == ["eng", "jpn"]
    assert [t.language for t in reconstructed.subtitle_tracks] == ["eng", "spa"]
    assert reconstructed == media_info


def test_read_inventory_report_reconstructs_metadata_error(connection: sqlite3.Connection, tmp_path: Path) -> None:
    scanned = _scanned_file(tmp_path, media_info=None, media_info_error="mediainfo timed out after 60s")
    report = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned]),))
    repo.persist_scan(connection, report, {"movies": str(tmp_path)}, metadata_enabled=True, mediainfo_version="v1")

    result = repo.read_inventory_report(connection, {"movies": str(tmp_path)})

    file = result.categories[0].files[0]
    assert file.media_info is None
    assert file.media_info_error == "mediainfo timed out after 60s"


def test_read_inventory_report_metadata_absent_remains_absent(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    scanned = _scanned_file(tmp_path)
    report = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned]),))
    repo.persist_scan(connection, report, {"movies": str(tmp_path)}, metadata_enabled=False, mediainfo_version=None)

    result = repo.read_inventory_report(connection, {"movies": str(tmp_path)})

    file = result.categories[0].files[0]
    assert file.media_info is None
    assert file.media_info_error is None


def test_read_inventory_report_is_semantically_equal_to_original_after_persistence(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    media_info = _sample_media_info()
    scanned_a = _scanned_file(tmp_path, name="A (2001).mkv", media_info=media_info)
    scanned_b = _scanned_file(tmp_path, name="B (2002).mkv")
    original = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned_a, scanned_b]),))

    repo.persist_scan(
        connection, original, {"movies": str(tmp_path)}, metadata_enabled=True, mediainfo_version="v1"
    )
    reconstructed = repo.read_inventory_report(connection, {"movies": str(tmp_path)})

    assert reconstructed == original


def test_read_inventory_report_render_summary_matches_in_memory(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from mams.inventory import render_summary

    media_info = _sample_media_info()
    scanned_a = _scanned_file(tmp_path, name="A (2001).mkv", media_info=media_info)
    scanned_b = _scanned_file(tmp_path, name="B (2002).mkv")
    original = InventoryReport(categories=(_category_scan("movies", tmp_path, [scanned_a, scanned_b]),))

    repo.persist_scan(
        connection, original, {"movies": str(tmp_path)}, metadata_enabled=True, mediainfo_version="v1"
    )
    reconstructed = repo.read_inventory_report(connection, {"movies": str(tmp_path)})

    assert render_summary(reconstructed) == render_summary(original)


def test_read_inventory_report_uses_a_bounded_number_of_queries(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    media_info = _sample_media_info()
    files = [_scanned_file(tmp_path, name=f"Movie {i:03d}.mkv", media_info=media_info) for i in range(20)]
    report = InventoryReport(categories=(_category_scan("movies", tmp_path, files),))
    repo.persist_scan(connection, report, {"movies": str(tmp_path)}, metadata_enabled=True, mediainfo_version="v1")

    executed: list[str] = []
    connection.set_trace_callback(executed.append)
    try:
        result = repo.read_inventory_report(connection, {"movies": str(tmp_path)})
    finally:
        connection.set_trace_callback(None)

    assert result.file_count == 20
    select_statements = [sql for sql in executed if sql.strip().upper().startswith("SELECT")]
    assert len(select_statements) < 10, (
        f"expected a bounded query count independent of file count (20 files), "
        f"got {len(select_statements)}: {select_statements}"
    )
