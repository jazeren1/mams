from __future__ import annotations

import json
from pathlib import Path

from mams.inventory import (
    Layout,
    detect_layout,
    render_summary,
    scan_categories,
    scan_category,
)


def _touch(path: Path, size: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)


def test_discovers_supported_files(tmp_path: Path) -> None:
    _touch(tmp_path / "Movie One (2001).mkv", size=10)
    _touch(tmp_path / "Movie Two (2002).mp4", size=20)

    scan = scan_category("movies", tmp_path)

    assert scan.exists is True
    assert scan.file_count == 2
    filenames = {f.filename for f in scan.files}
    assert filenames == {"Movie One (2001).mkv", "Movie Two (2002).mp4"}


def test_recursion_into_subdirectories(tmp_path: Path) -> None:
    _touch(tmp_path / "Series/Season 01/Series - S01E01.mkv")
    _touch(tmp_path / "Series/Season 01/Series - S01E02.mkv")
    _touch(tmp_path / "Series/Season 02/Series - S02E01.mkv")

    scan = scan_category("tv", tmp_path)

    assert scan.file_count == 3
    relative_paths = {f.relative_path for f in scan.files}
    assert relative_paths == {
        "Series/Season 01/Series - S01E01.mkv",
        "Series/Season 01/Series - S01E02.mkv",
        "Series/Season 02/Series - S02E01.mkv",
    }


def test_extension_filtering(tmp_path: Path) -> None:
    _touch(tmp_path / "Movie (2001).mkv")
    _touch(tmp_path / "poster.jpg")
    _touch(tmp_path / "notes.txt")
    _touch(tmp_path / "sample.srt")

    scan = scan_category("movies", tmp_path)

    assert scan.file_count == 1
    assert scan.files[0].filename == "Movie (2001).mkv"


def test_unsupported_extensions_are_never_recorded(tmp_path: Path) -> None:
    _touch(tmp_path / "readme.nfo")

    scan = scan_category("movies", tmp_path)

    assert scan.file_count == 0


def test_hidden_files_ignored(tmp_path: Path) -> None:
    _touch(tmp_path / "Movie (2001).mkv")
    _touch(tmp_path / ".DS_Store")
    _touch(tmp_path / "._Movie (2001).mkv")
    _touch(tmp_path / ".hidden.mkv")

    scan = scan_category("movies", tmp_path)

    assert scan.file_count == 1
    assert scan.files[0].filename == "Movie (2001).mkv"


def test_hidden_directories_are_not_descended_into(tmp_path: Path) -> None:
    _touch(tmp_path / ".AppleDouble/Movie (2001).mkv")
    _touch(tmp_path / "Visible (2002).mkv")

    scan = scan_category("movies", tmp_path)

    assert scan.file_count == 1
    assert scan.files[0].filename == "Visible (2002).mkv"


def test_macos_and_nas_metadata_directories_ignored(tmp_path: Path) -> None:
    _touch(tmp_path / "__MACOSX/Movie (2001).mkv")
    _touch(tmp_path / "@eaDir/thumb.mkv")
    _touch(tmp_path / "Real Movie (2003).mkv")

    scan = scan_category("movies", tmp_path)

    assert scan.file_count == 1
    assert scan.files[0].filename == "Real Movie (2003).mkv"


def test_temporary_and_lock_files_ignored(tmp_path: Path) -> None:
    _touch(tmp_path / "Movie (2001).mkv")
    _touch(tmp_path / "Movie (2001).mkv.tmp")
    _touch(tmp_path / "Movie (2001).mkv.part")
    _touch(tmp_path / "~$Movie (2001).mkv")

    scan = scan_category("movies", tmp_path)

    assert scan.file_count == 1
    assert scan.files[0].filename == "Movie (2001).mkv"


def test_layout_detection_movie_flat(tmp_path: Path) -> None:
    relative = Path("Movie (2001).mkv")
    assert detect_layout("movies", relative) == Layout.MOVIE_FLAT


def test_layout_detection_movie_folder(tmp_path: Path) -> None:
    relative = Path("Movie (2001)/Movie (2001).mkv")
    assert detect_layout("movies", relative) == Layout.MOVIE_FOLDER


def test_layout_detection_movie_collection_folder(tmp_path: Path) -> None:
    relative = Path("Star Wars/A New Hope/A New Hope_001.mp4")
    assert detect_layout("movies", relative) == Layout.MOVIE_COLLECTION_FOLDER


def test_collection_folder_with_multiple_files_is_untouched_by_scan(tmp_path: Path) -> None:
    movie_dir = tmp_path / "Star Wars" / "The Phantom Menace"
    disc_one = movie_dir / "The Phantom Menace_001.mkv"
    disc_two = movie_dir / "The Phantom Menace_002.mkv"
    _touch(disc_one, size=11)
    _touch(disc_two, size=22)
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in (disc_one, disc_two)}

    scan = scan_category("movies", tmp_path)

    assert scan.file_count == 2
    layouts = {f.filename: f.layout for f in scan.files}
    assert layouts["The Phantom Menace_001.mkv"] == Layout.MOVIE_COLLECTION_FOLDER
    assert layouts["The Phantom Menace_002.mkv"] == Layout.MOVIE_COLLECTION_FOLDER
    after = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in (disc_one, disc_two)}
    assert before == after


def test_layout_detection_movie_unknown_when_deeply_nested(tmp_path: Path) -> None:
    relative = Path("Star Wars/A New Hope/Extras/A New Hope_001.mp4")
    assert detect_layout("movies", relative) == Layout.UNKNOWN


def test_layout_detection_tv_series_folder(tmp_path: Path) -> None:
    relative = Path("Series Name/Series Name - S01E01.mkv")
    assert detect_layout("tv", relative) == Layout.TV_SERIES_FOLDER


def test_layout_detection_tv_season_folder(tmp_path: Path) -> None:
    relative = Path("Series Name/Season 01/Series Name - S01E01.mkv")
    assert detect_layout("tv", relative) == Layout.TV_SEASON_FOLDER


def test_layout_detection_tv_unknown_when_deeply_nested(tmp_path: Path) -> None:
    relative = Path("Series Name/Season 01/Disc 1/Series Name - S01E01.mkv")
    assert detect_layout("tv", relative) == Layout.UNKNOWN


def test_layout_detection_end_to_end_scan(tmp_path: Path) -> None:
    _touch(tmp_path / "Flat Movie (2001).mkv")
    _touch(tmp_path / "Folder Movie (2002)/Folder Movie (2002).mkv")
    _touch(tmp_path / "Star Wars/A New Hope/A New Hope_001.mp4")
    _touch(tmp_path / "The Lord of the Rings/Fellowship of the Ring/TheFellowshipOfTheRing.mp4")

    scan = scan_category("movies", tmp_path)
    layouts = {f.filename: f.layout for f in scan.files}

    assert layouts["Flat Movie (2001).mkv"] == Layout.MOVIE_FLAT
    assert layouts["Folder Movie (2002).mkv"] == Layout.MOVIE_FOLDER
    assert layouts["A New Hope_001.mp4"] == Layout.MOVIE_COLLECTION_FOLDER
    assert layouts["TheFellowshipOfTheRing.mp4"] == Layout.MOVIE_COLLECTION_FOLDER


def test_missing_directory_does_not_raise(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    scan = scan_category("movies", missing)

    assert scan.exists is False
    assert scan.file_count == 0
    assert scan.files == ()


def test_scan_categories_handles_mixed_existing_and_missing(tmp_path: Path) -> None:
    movies_root = tmp_path / "Movies"
    _touch(movies_root / "Movie (2001).mkv")
    missing_root = tmp_path / "Missing"

    report = scan_categories({"movies": str(movies_root), "tv": str(missing_root)})

    by_category = {c.category: c for c in report.categories}
    assert by_category["movies"].exists is True
    assert by_category["movies"].file_count == 1
    assert by_category["tv"].exists is False
    assert report.file_count == 1


def test_deterministic_ordering(tmp_path: Path) -> None:
    # Create files in an order that does not match sorted order.
    _touch(tmp_path / "Zebra (2001).mkv")
    _touch(tmp_path / "Apple (2002).mkv")
    _touch(tmp_path / "Mango (2003).mkv")

    first = scan_category("movies", tmp_path)
    second = scan_category("movies", tmp_path)

    first_order = [f.relative_path for f in first.files]
    second_order = [f.relative_path for f in second.files]

    assert first_order == second_order
    assert first_order == sorted(first_order)


def test_file_size_recorded(tmp_path: Path) -> None:
    _touch(tmp_path / "Movie (2001).mkv", size=1234)

    scan = scan_category("movies", tmp_path)

    assert scan.files[0].size_bytes == 1234
    assert scan.total_size_bytes == 1234


def test_scanned_file_metadata_fields(tmp_path: Path) -> None:
    _touch(tmp_path / "Series/Season 01/Series - S01E01.mkv", size=5)

    scan = scan_category("tv", tmp_path)
    scanned = scan.files[0]

    assert scanned.category == "tv"
    assert scanned.filename == "Series - S01E01.mkv"
    assert scanned.extension == ".mkv"
    assert scanned.relative_path == "Series/Season 01/Series - S01E01.mkv"
    assert Path(scanned.parent_directory).name == "Season 01"
    assert Path(scanned.absolute_path).is_absolute()
    assert Path(scanned.absolute_path).exists()


def test_extension_matching_is_case_insensitive(tmp_path: Path) -> None:
    _touch(tmp_path / "Movie (2001).MKV")

    scan = scan_category("movies", tmp_path)

    assert scan.file_count == 1
    assert scan.files[0].extension == ".mkv"


def test_json_report_is_valid_and_round_trips(tmp_path: Path) -> None:
    _touch(tmp_path / "Movie (2001).mkv", size=42)

    report = scan_categories({"movies": str(tmp_path)})
    payload = json.loads(report.to_json())

    assert payload["file_count"] == 1
    assert payload["total_size_bytes"] == 42
    assert payload["categories"][0]["category"] == "movies"
    assert payload["categories"][0]["files"][0]["layout"] == "movie_flat"


def test_render_summary_reports_missing_category(tmp_path: Path) -> None:
    report = scan_categories({"movies": str(tmp_path / "missing")})

    summary = render_summary(report)

    assert "MISSING" in summary
    assert "not found" in summary


def test_render_summary_reports_counts(tmp_path: Path) -> None:
    _touch(tmp_path / "Movie (2001).mkv")

    report = scan_categories({"movies": str(tmp_path)})
    summary = render_summary(report)

    assert "files: 1" in summary
    assert "Total files: 1" in summary
