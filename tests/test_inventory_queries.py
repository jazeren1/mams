"""Tests for the query layer (list_media_files, search_media_files,
get_inventory_stats) in inventory_repository.py.

Builds real inventory data via persist_scan() (same fixtures pattern as
test_inventory_repository.py) rather than hand-crafting rows, so these
tests exercise the actual write+query round trip.
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
    name: str,
    *,
    category: str = "movies",
    size: int = 10,
    layout: Layout = Layout.MOVIE_FLAT,
    media_info: MediaInfo | None = None,
    media_info_error: str | None = None,
) -> ScannedFile:
    path = tmp_path / category / name
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


def _sample_media_info() -> MediaInfo:
    return MediaInfo(
        container="Matroska",
        duration_seconds=120.0,
        overall_bitrate=5_000_000,
        video_tracks=(
            VideoTrack(
                codec="HEVC", width=1920, height=1080, aspect_ratio="16:9",
                frame_rate=23.976, hdr_format="HDR10", bit_depth=10, scan_type="Progressive",
            ),
        ),
        audio_tracks=(
            AudioTrack(codec="AC3", language="eng", channels=6, bitrate=640_000, default=True),
            AudioTrack(codec="AAC", language="jpn", channels=2, bitrate=128_000, default=False),
        ),
        subtitle_tracks=(SubtitleTrack(language="eng", default=False, forced=False),),
    )


def _seed_library(
    connection: sqlite3.Connection,
    tmp_path: Path,
    category: str,
    files: list[ScannedFile],
    *,
    metadata_enabled: bool = False,
) -> None:
    (tmp_path / category).mkdir(parents=True, exist_ok=True)
    report = InventoryReport(
        categories=(CategoryScan(category=category, root_path=str(tmp_path / category), exists=True, files=tuple(files)),)
    )
    repo.persist_scan(
        connection, report, {category: str(tmp_path / category)},
        metadata_enabled=metadata_enabled, mediainfo_version="v1" if metadata_enabled else None,
    )


# --- list_media_files -------------------------------------------------------


def test_list_media_files_filters_by_category(connection: sqlite3.Connection, tmp_path: Path) -> None:
    _seed_library(connection, tmp_path, "movies", [_scanned_file(tmp_path, "M (2001).mkv", category="movies")])
    _seed_library(connection, tmp_path, "tv", [_scanned_file(tmp_path, "T (2002).mkv", category="tv")])

    result = repo.list_media_files(connection, category="movies")

    assert [r.filename for r in result] == ["M (2001).mkv"]


def test_list_media_files_filters_by_state(connection: sqlite3.Connection, tmp_path: Path) -> None:
    f = _scanned_file(tmp_path, "M (2001).mkv")
    _seed_library(connection, tmp_path, "movies", [f])
    _seed_library(connection, tmp_path, "movies", [])  # rescan without the file -> MISSING

    active = repo.list_media_files(connection, state="ACTIVE")
    missing = repo.list_media_files(connection, state="MISSING")

    assert active == []
    assert [r.filename for r in missing] == ["M (2001).mkv"]
    assert missing[0].state == "MISSING"


def test_list_media_files_filters_by_layout(connection: sqlite3.Connection, tmp_path: Path) -> None:
    flat = _scanned_file(tmp_path, "Flat (2001).mkv", layout=Layout.MOVIE_FLAT)
    folder = ScannedFile(
        category="movies", absolute_path=str(tmp_path / "movies" / "Folder (2002)" / "Folder (2002).mkv"),
        relative_path="Folder (2002)/Folder (2002).mkv", filename="Folder (2002).mkv", extension=".mkv",
        parent_directory=str(tmp_path / "movies" / "Folder (2002)"), size_bytes=10, layout=Layout.MOVIE_FOLDER,
    )
    _touch(Path(folder.absolute_path))
    _seed_library(connection, tmp_path, "movies", [flat, folder])

    result = repo.list_media_files(connection, layout="movie_folder")

    assert [r.filename for r in result] == ["Folder (2002).mkv"]


def test_list_media_files_metadata_error_filter(connection: sqlite3.Connection, tmp_path: Path) -> None:
    good = _scanned_file(tmp_path, "Good (2001).mkv", media_info=_sample_media_info())
    bad = _scanned_file(tmp_path, "Bad (2002).mkv", media_info_error="mediainfo timed out")
    _seed_library(connection, tmp_path, "movies", [good, bad], metadata_enabled=True)

    errors_only = repo.list_media_files(connection, metadata_error=True)
    no_errors = repo.list_media_files(connection, metadata_error=False)

    assert [r.filename for r in errors_only] == ["Bad (2002).mkv"]
    assert [r.filename for r in no_errors] == ["Good (2001).mkv"]


def test_list_media_files_deterministic_ordering(connection: sqlite3.Connection, tmp_path: Path) -> None:
    files = [_scanned_file(tmp_path, n) for n in ("Zebra (2001).mkv", "Apple (2002).mkv", "Mango (2003).mkv")]
    _seed_library(connection, tmp_path, "movies", files)

    result = repo.list_media_files(connection)

    assert [r.filename for r in result] == ["Apple (2002).mkv", "Mango (2003).mkv", "Zebra (2001).mkv"]


def test_list_media_files_limit(connection: sqlite3.Connection, tmp_path: Path) -> None:
    files = [_scanned_file(tmp_path, f"Movie {i:02d}.mkv") for i in range(5)]
    _seed_library(connection, tmp_path, "movies", files)

    result = repo.list_media_files(connection, limit=2)

    assert len(result) == 2


def test_list_media_files_includes_track_counts(connection: sqlite3.Connection, tmp_path: Path) -> None:
    f = _scanned_file(tmp_path, "Movie (2001).mkv", media_info=_sample_media_info())
    _seed_library(connection, tmp_path, "movies", [f], metadata_enabled=True)

    result = repo.list_media_files(connection)

    assert result[0].video_track_count == 1
    assert result[0].audio_track_count == 2
    assert result[0].subtitle_track_count == 1


def test_list_media_files_empty_database_returns_empty_list(connection: sqlite3.Connection) -> None:
    assert repo.list_media_files(connection) == []


# --- search_media_files ------------------------------------------------------


def test_search_media_files_is_case_insensitive(connection: sqlite3.Connection, tmp_path: Path) -> None:
    f = _scanned_file(tmp_path, "District 9 (2009).mkv")
    _seed_library(connection, tmp_path, "movies", [f])

    lower = repo.search_media_files(connection, "district")
    upper = repo.search_media_files(connection, "DISTRICT")
    mixed = repo.search_media_files(connection, "DiStRiCt")

    assert [r.filename for r in lower] == ["District 9 (2009).mkv"]
    assert [r.filename for r in upper] == ["District 9 (2009).mkv"]
    assert [r.filename for r in mixed] == ["District 9 (2009).mkv"]


def test_search_media_files_matches_relative_and_absolute_path(connection: sqlite3.Connection, tmp_path: Path) -> None:
    f = ScannedFile(
        category="tv", absolute_path=str(tmp_path / "tv" / "Carnivale" / "Season 01" / "S01E01.mkv"),
        relative_path="Carnivale/Season 01/S01E01.mkv", filename="S01E01.mkv", extension=".mkv",
        parent_directory=str(tmp_path / "tv" / "Carnivale" / "Season 01"), size_bytes=10,
        layout=Layout.TV_SEASON_FOLDER,
    )
    _touch(Path(f.absolute_path))
    _seed_library(connection, tmp_path, "tv", [f])

    result = repo.search_media_files(connection, "carnivale")

    assert [r.filename for r in result] == ["S01E01.mkv"]


def test_search_media_files_no_match_returns_empty(connection: sqlite3.Connection, tmp_path: Path) -> None:
    _seed_library(connection, tmp_path, "movies", [_scanned_file(tmp_path, "Movie (2001).mkv")])

    assert repo.search_media_files(connection, "nonexistent-title") == []


def test_search_media_files_category_filter(connection: sqlite3.Connection, tmp_path: Path) -> None:
    _seed_library(connection, tmp_path, "movies", [_scanned_file(tmp_path, "Shared Name.mkv", category="movies")])
    _seed_library(connection, tmp_path, "tv", [_scanned_file(tmp_path, "Shared Name.mkv", category="tv")])

    result = repo.search_media_files(connection, "shared", category="movies")

    assert len(result) == 1
    assert result[0].category == "movies"


def test_search_media_files_state_filter(connection: sqlite3.Connection, tmp_path: Path) -> None:
    f = _scanned_file(tmp_path, "Vanishing (2001).mkv")
    _seed_library(connection, tmp_path, "movies", [f])
    _seed_library(connection, tmp_path, "movies", [])  # goes MISSING

    active = repo.search_media_files(connection, "vanishing", state="ACTIVE")
    missing = repo.search_media_files(connection, "vanishing", state="MISSING")

    assert active == []
    assert len(missing) == 1


def test_search_media_files_deterministic_ordering(connection: sqlite3.Connection, tmp_path: Path) -> None:
    files = [_scanned_file(tmp_path, n) for n in ("Zebra Movie.mkv", "Apple Movie.mkv")]
    _seed_library(connection, tmp_path, "movies", files)

    result = repo.search_media_files(connection, "movie")

    assert [r.filename for r in result] == ["Apple Movie.mkv", "Zebra Movie.mkv"]


def test_search_media_files_does_not_string_interpolate_query(connection: sqlite3.Connection, tmp_path: Path) -> None:
    """A query containing SQL-meaningful characters must not break or leak
    behavior -- proves parameterization, not string formatting."""
    _seed_library(connection, tmp_path, "movies", [_scanned_file(tmp_path, "Movie (2001).mkv")])

    result = repo.search_media_files(connection, "'; DROP TABLE media_files; --")

    assert result == []
    # table must still exist and be queryable
    assert repo.list_media_files(connection) != []


# --- get_inventory_stats -----------------------------------------------------


def test_get_inventory_stats_totals(connection: sqlite3.Connection, tmp_path: Path) -> None:
    active = _scanned_file(tmp_path, "Active (2001).mkv", size=100)
    to_go_missing = _scanned_file(tmp_path, "Gone (2002).mkv", size=200)
    _seed_library(connection, tmp_path, "movies", [active, to_go_missing])
    _seed_library(connection, tmp_path, "movies", [active])  # "Gone" becomes MISSING

    stats = repo.get_inventory_stats(connection)

    assert stats.active_file_count == 1
    assert stats.missing_file_count == 1
    assert stats.active_total_size_bytes == 100


def test_get_inventory_stats_per_library_breakdown(connection: sqlite3.Connection, tmp_path: Path) -> None:
    _seed_library(connection, tmp_path, "movies", [_scanned_file(tmp_path, "M.mkv", category="movies", size=50)])
    _seed_library(connection, tmp_path, "tv", [_scanned_file(tmp_path, "T.mkv", category="tv", size=75)])

    stats = repo.get_inventory_stats(connection)

    by_category = {lib.category: lib for lib in stats.libraries}
    assert by_category["movies"].active_count == 1
    assert by_category["movies"].active_total_size_bytes == 50
    assert by_category["tv"].active_count == 1
    assert by_category["tv"].active_total_size_bytes == 75


def test_get_inventory_stats_layout_and_extension_counts(connection: sqlite3.Connection, tmp_path: Path) -> None:
    a = _scanned_file(tmp_path, "A.mkv", layout=Layout.MOVIE_FLAT)
    b = _scanned_file(tmp_path, "B.mp4", layout=Layout.MOVIE_FLAT)
    _seed_library(connection, tmp_path, "movies", [a, b])

    stats = repo.get_inventory_stats(connection)

    assert stats.layout_counts == {"movie_flat": 2}
    assert stats.extension_counts == {".mkv": 1, ".mp4": 1}


def test_get_inventory_stats_metadata_counts(connection: sqlite3.Connection, tmp_path: Path) -> None:
    success = _scanned_file(tmp_path, "Success.mkv", media_info=_sample_media_info())
    error = _scanned_file(tmp_path, "Error.mkv", media_info_error="boom")
    not_probed = _scanned_file(tmp_path, "NotProbed.mkv")
    _seed_library(connection, tmp_path, "movies", [success, error, not_probed], metadata_enabled=True)

    stats = repo.get_inventory_stats(connection)

    assert stats.metadata_success_count == 1
    assert stats.metadata_error_count == 1
    assert stats.metadata_not_probed_count == 1


def test_get_inventory_stats_track_counts(connection: sqlite3.Connection, tmp_path: Path) -> None:
    f = _scanned_file(tmp_path, "Movie.mkv", media_info=_sample_media_info())
    _seed_library(connection, tmp_path, "movies", [f], metadata_enabled=True)

    stats = repo.get_inventory_stats(connection)

    assert stats.video_track_count == 1
    assert stats.audio_track_count == 2
    assert stats.subtitle_track_count == 1


def test_get_inventory_stats_most_recent_scan(connection: sqlite3.Connection, tmp_path: Path) -> None:
    _seed_library(connection, tmp_path, "movies", [_scanned_file(tmp_path, "M.mkv")])
    _seed_library(connection, tmp_path, "movies", [_scanned_file(tmp_path, "M.mkv"), _scanned_file(tmp_path, "N.mkv")])

    stats = repo.get_inventory_stats(connection)

    assert stats.most_recent_scan is not None
    assert stats.most_recent_scan.status == "COMPLETE"
    assert stats.most_recent_scan.file_count == 2


def test_get_inventory_stats_on_empty_database(connection: sqlite3.Connection) -> None:
    stats = repo.get_inventory_stats(connection)

    assert stats.active_file_count == 0
    assert stats.missing_file_count == 0
    assert stats.libraries == ()
    assert stats.most_recent_scan is None


# --- bounded query counts -----------------------------------------------------


def test_list_media_files_uses_a_bounded_number_of_queries(connection: sqlite3.Connection, tmp_path: Path) -> None:
    files = [_scanned_file(tmp_path, f"Movie {i:03d}.mkv", media_info=_sample_media_info()) for i in range(15)]
    _seed_library(connection, tmp_path, "movies", files, metadata_enabled=True)

    executed: list[str] = []
    connection.set_trace_callback(executed.append)
    try:
        result = repo.list_media_files(connection)
    finally:
        connection.set_trace_callback(None)

    assert len(result) == 15
    select_statements = [sql for sql in executed if sql.strip().upper().startswith("SELECT")]
    assert len(select_statements) == 1, select_statements


def test_get_inventory_stats_uses_a_bounded_number_of_queries(connection: sqlite3.Connection, tmp_path: Path) -> None:
    files = [_scanned_file(tmp_path, f"Movie {i:03d}.mkv", media_info=_sample_media_info()) for i in range(15)]
    _seed_library(connection, tmp_path, "movies", files, metadata_enabled=True)

    executed: list[str] = []
    connection.set_trace_callback(executed.append)
    try:
        repo.get_inventory_stats(connection)
    finally:
        connection.set_trace_callback(None)

    select_statements = [sql for sql in executed if sql.strip().upper().startswith("SELECT")]
    assert len(select_statements) <= 6, select_statements
