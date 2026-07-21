"""Tests for change-event generation (ADDED/UPDATED/MISSING/RESTORED) in
inventory_repository.py's write path.

Builds InventoryReport/CategoryScan/ScannedFile objects directly and
persists them via persist_scan(), then inspects the resulting scan_changes
rows -- same approach as test_inventory_repository.py.
"""

from __future__ import annotations

import json
import os
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


def _category_scan(category: str, root: Path, files: list[ScannedFile], *, exists: bool = True) -> CategoryScan:
    return CategoryScan(category=category, root_path=str(root), exists=exists, files=tuple(files))


def _sample_media_info(video_codec: str = "HEVC", duration: float = 120.0) -> MediaInfo:
    return MediaInfo(
        container="Matroska",
        duration_seconds=duration,
        overall_bitrate=5_000_000,
        video_tracks=(
            VideoTrack(
                codec=video_codec, width=1920, height=1080, aspect_ratio="16:9",
                frame_rate=23.976, hdr_format=None, bit_depth=8, scan_type="Progressive",
            ),
        ),
        audio_tracks=(AudioTrack(codec="AC3", language="eng", channels=6, bitrate=640_000, default=True),),
        subtitle_tracks=(SubtitleTrack(language="eng", default=False, forced=False),),
    )


def _persist(
    connection: sqlite3.Connection,
    tmp_path: Path,
    files: list[ScannedFile],
    *,
    exists: bool = True,
    metadata_enabled: bool = False,
) -> int:
    report = InventoryReport(categories=(_category_scan("movies", tmp_path, files, exists=exists),))
    return repo.persist_scan(
        connection, report, {"movies": str(tmp_path)}, metadata_enabled=metadata_enabled, mediainfo_version=None
    )


def _changes_for_scan(connection: sqlite3.Connection, scan_run_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM scan_changes WHERE scan_run_id = ? ORDER BY id", (scan_run_id,)
    ).fetchall()


def _details(row: sqlite3.Row) -> dict:
    assert row["details_json"] is not None
    return json.loads(row["details_json"])


# --- ADDED / no-op ------------------------------------------------------------


def test_first_scan_records_added_event(connection: sqlite3.Connection, tmp_path: Path) -> None:
    scanned = _scanned_file(tmp_path)
    scan_run_id = _persist(connection, tmp_path, [scanned])

    changes = _changes_for_scan(connection, scan_run_id)

    assert len(changes) == 1
    assert changes[0]["change_type"] == "ADDED"
    assert changes[0]["absolute_path"] == scanned.absolute_path
    assert changes[0]["details_json"] is None


def test_unchanged_repeat_scan_records_no_events(connection: sqlite3.Connection, tmp_path: Path) -> None:
    scanned = _scanned_file(tmp_path)
    _persist(connection, tmp_path, [scanned])

    second_scan_id = _persist(connection, tmp_path, [scanned])

    assert _changes_for_scan(connection, second_scan_id) == []


# --- UPDATED --------------------------------------------------------------


def test_size_change_records_updated_event(connection: sqlite3.Connection, tmp_path: Path) -> None:
    scanned = _scanned_file(tmp_path, size=10)
    _persist(connection, tmp_path, [scanned])

    _touch(Path(scanned.absolute_path), size=20)
    resized = ScannedFile(
        category=scanned.category, absolute_path=scanned.absolute_path, relative_path=scanned.relative_path,
        filename=scanned.filename, extension=scanned.extension, parent_directory=scanned.parent_directory,
        size_bytes=20, layout=scanned.layout,
    )
    scan_run_id = _persist(connection, tmp_path, [resized])

    changes = _changes_for_scan(connection, scan_run_id)
    assert len(changes) == 1
    assert changes[0]["change_type"] == "UPDATED"
    details = _details(changes[0])
    field_names = {c["field"] for c in details["changes"]}
    assert "size_bytes" in field_names
    size_change = next(c for c in details["changes"] if c["field"] == "size_bytes")
    assert size_change == {"field": "size_bytes", "old": 10, "new": 20}


def test_mtime_change_records_updated_event(connection: sqlite3.Connection, tmp_path: Path) -> None:
    scanned = _scanned_file(tmp_path)
    _persist(connection, tmp_path, [scanned])

    # Force a distinct mtime -- same size/content, just touched.
    new_mtime = Path(scanned.absolute_path).stat().st_mtime + 100
    os.utime(scanned.absolute_path, (new_mtime, new_mtime))

    scan_run_id = _persist(connection, tmp_path, [scanned])

    changes = _changes_for_scan(connection, scan_run_id)
    assert len(changes) == 1
    assert changes[0]["change_type"] == "UPDATED"
    field_names = {c["field"] for c in _details(changes[0])["changes"]}
    assert field_names == {"mtime"}


def test_layout_change_records_updated_event(connection: sqlite3.Connection, tmp_path: Path) -> None:
    scanned = _scanned_file(tmp_path, layout=Layout.MOVIE_FLAT)
    _persist(connection, tmp_path, [scanned])

    relayout = ScannedFile(
        category=scanned.category, absolute_path=scanned.absolute_path, relative_path=scanned.relative_path,
        filename=scanned.filename, extension=scanned.extension, parent_directory=scanned.parent_directory,
        size_bytes=scanned.size_bytes, layout=Layout.MOVIE_FOLDER,
    )
    scan_run_id = _persist(connection, tmp_path, [relayout])

    changes = _changes_for_scan(connection, scan_run_id)
    assert len(changes) == 1
    assert changes[0]["change_type"] == "UPDATED"
    layout_change = next(c for c in _details(changes[0])["changes"] if c["field"] == "layout")
    assert layout_change == {"field": "layout", "old": "movie_flat", "new": "movie_folder"}


def test_metadata_change_records_updated_event(connection: sqlite3.Connection, tmp_path: Path) -> None:
    scanned = _scanned_file(tmp_path, media_info=_sample_media_info(duration=100.0))
    _persist(connection, tmp_path, [scanned], metadata_enabled=True)

    changed = ScannedFile(
        category=scanned.category, absolute_path=scanned.absolute_path, relative_path=scanned.relative_path,
        filename=scanned.filename, extension=scanned.extension, parent_directory=scanned.parent_directory,
        size_bytes=scanned.size_bytes, layout=scanned.layout, media_info=_sample_media_info(duration=200.0),
    )
    scan_run_id = _persist(connection, tmp_path, [changed], metadata_enabled=True)

    changes = _changes_for_scan(connection, scan_run_id)
    assert len(changes) == 1
    assert changes[0]["change_type"] == "UPDATED"
    duration_change = next(c for c in _details(changes[0])["changes"] if c["field"] == "duration_seconds")
    assert duration_change == {"field": "duration_seconds", "old": 100.0, "new": 200.0}


def test_track_content_change_records_updated_event_with_counts_not_payload(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    single_track_info = _sample_media_info()
    scanned = _scanned_file(tmp_path, media_info=single_track_info)
    _persist(connection, tmp_path, [scanned], metadata_enabled=True)

    two_track_info = MediaInfo(
        container=single_track_info.container,
        duration_seconds=single_track_info.duration_seconds,
        overall_bitrate=single_track_info.overall_bitrate,
        video_tracks=single_track_info.video_tracks,
        audio_tracks=single_track_info.audio_tracks
        + (AudioTrack(codec="AAC", language="jpn", channels=2, bitrate=128_000, default=False),),
        subtitle_tracks=single_track_info.subtitle_tracks,
    )
    changed = ScannedFile(
        category=scanned.category, absolute_path=scanned.absolute_path, relative_path=scanned.relative_path,
        filename=scanned.filename, extension=scanned.extension, parent_directory=scanned.parent_directory,
        size_bytes=scanned.size_bytes, layout=scanned.layout, media_info=two_track_info,
    )
    scan_run_id = _persist(connection, tmp_path, [changed], metadata_enabled=True)

    changes = _changes_for_scan(connection, scan_run_id)
    assert len(changes) == 1
    audio_change = next(c for c in _details(changes[0])["changes"] if c["field"] == "audio_tracks")
    assert audio_change == {"field": "audio_tracks", "old_count": 1, "new_count": 2}
    # must not leak full track payloads into details_json
    assert "codec" not in changes[0]["details_json"]
    assert "language" not in changes[0]["details_json"]


def test_no_metadata_scan_does_not_create_false_metadata_changes(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    scanned = _scanned_file(tmp_path, media_info=_sample_media_info())
    _persist(connection, tmp_path, [scanned], metadata_enabled=True)

    no_metadata_rescan = ScannedFile(
        category=scanned.category, absolute_path=scanned.absolute_path, relative_path=scanned.relative_path,
        filename=scanned.filename, extension=scanned.extension, parent_directory=scanned.parent_directory,
        size_bytes=scanned.size_bytes, layout=scanned.layout,
    )
    scan_run_id = _persist(connection, tmp_path, [no_metadata_rescan], metadata_enabled=False)

    assert _changes_for_scan(connection, scan_run_id) == []


def test_successful_metadata_probe_identical_to_existing_creates_no_false_update(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    media_info = _sample_media_info()
    scanned = _scanned_file(tmp_path, media_info=media_info)
    _persist(connection, tmp_path, [scanned], metadata_enabled=True)

    # A fresh MediaInfo object with identical content -- proves the diff is
    # by value, not by whether the track rows got touched (they always are:
    # delete+reinsert on every successful probe).
    identical_reprobe = ScannedFile(
        category=scanned.category, absolute_path=scanned.absolute_path, relative_path=scanned.relative_path,
        filename=scanned.filename, extension=scanned.extension, parent_directory=scanned.parent_directory,
        size_bytes=scanned.size_bytes, layout=scanned.layout, media_info=_sample_media_info(),
    )
    scan_run_id = _persist(connection, tmp_path, [identical_reprobe], metadata_enabled=True)

    assert _changes_for_scan(connection, scan_run_id) == []


def test_metadata_error_transition_records_updated_event(connection: sqlite3.Connection, tmp_path: Path) -> None:
    scanned = _scanned_file(tmp_path, media_info=_sample_media_info())
    _persist(connection, tmp_path, [scanned], metadata_enabled=True)

    now_failing = ScannedFile(
        category=scanned.category, absolute_path=scanned.absolute_path, relative_path=scanned.relative_path,
        filename=scanned.filename, extension=scanned.extension, parent_directory=scanned.parent_directory,
        size_bytes=scanned.size_bytes, layout=scanned.layout,
        media_info=None, media_info_error="mediainfo timed out after 60s",
    )
    scan_run_id = _persist(connection, tmp_path, [now_failing], metadata_enabled=True)

    changes = _changes_for_scan(connection, scan_run_id)
    assert len(changes) == 1
    assert changes[0]["change_type"] == "UPDATED"
    error_change = next(c for c in _details(changes[0])["changes"] if c["field"] == "media_info_error")
    assert error_change == {"field": "media_info_error", "old": None, "new": "mediainfo timed out after 60s"}


# --- MISSING / RESTORED --------------------------------------------------


def test_removal_records_missing_event(connection: sqlite3.Connection, tmp_path: Path) -> None:
    scanned = _scanned_file(tmp_path)
    _persist(connection, tmp_path, [scanned])

    scan_run_id = _persist(connection, tmp_path, [])

    changes = _changes_for_scan(connection, scan_run_id)
    assert len(changes) == 1
    assert changes[0]["change_type"] == "MISSING"
    assert changes[0]["absolute_path"] == scanned.absolute_path
    assert changes[0]["details_json"] is None


def test_rediscovery_records_restored_event(connection: sqlite3.Connection, tmp_path: Path) -> None:
    scanned = _scanned_file(tmp_path)
    _persist(connection, tmp_path, [scanned])
    _persist(connection, tmp_path, [])  # goes MISSING

    scan_run_id = _persist(connection, tmp_path, [scanned])

    changes = _changes_for_scan(connection, scan_run_id)
    assert len(changes) == 1
    assert changes[0]["change_type"] == "RESTORED"
    assert changes[0]["absolute_path"] == scanned.absolute_path


def test_rediscovery_with_other_changes_is_a_single_restored_event(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    scanned = _scanned_file(tmp_path, size=10)
    _persist(connection, tmp_path, [scanned])
    _persist(connection, tmp_path, [])  # goes MISSING

    _touch(Path(scanned.absolute_path), size=999)
    resized = ScannedFile(
        category=scanned.category, absolute_path=scanned.absolute_path, relative_path=scanned.relative_path,
        filename=scanned.filename, extension=scanned.extension, parent_directory=scanned.parent_directory,
        size_bytes=999, layout=scanned.layout,
    )
    scan_run_id = _persist(connection, tmp_path, [resized])

    changes = _changes_for_scan(connection, scan_run_id)
    assert len(changes) == 1
    assert changes[0]["change_type"] == "RESTORED"
    size_change = next(c for c in _details(changes[0])["changes"] if c["field"] == "size_bytes")
    assert size_change == {"field": "size_bytes", "old": 10, "new": 999}


def test_unavailable_root_records_no_missing_event(connection: sqlite3.Connection, tmp_path: Path) -> None:
    scanned = _scanned_file(tmp_path)
    _persist(connection, tmp_path, [scanned])

    scan_run_id = _persist(connection, tmp_path, [], exists=False)

    assert _changes_for_scan(connection, scan_run_id) == []
    row = connection.execute(
        "SELECT state FROM media_files WHERE absolute_path = ?", (scanned.absolute_path,)
    ).fetchone()
    assert row["state"] == "ACTIVE"


# --- atomicity --------------------------------------------------------------


def test_failed_persistence_records_no_events(connection: sqlite3.Connection, tmp_path: Path) -> None:
    vanished = ScannedFile(
        category="movies", absolute_path=str(tmp_path / "Vanished.mkv"), relative_path="Vanished.mkv",
        filename="Vanished.mkv", extension=".mkv", parent_directory=str(tmp_path), size_bytes=10,
        layout=Layout.MOVIE_FLAT,
    )
    with pytest.raises(FileNotFoundError):
        _persist(connection, tmp_path, [vanished])

    total = connection.execute("SELECT COUNT(*) FROM scan_changes").fetchone()[0]
    assert total == 0


def test_multiple_changes_roll_back_atomically_when_reconciliation_fails(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    good_a = _scanned_file(tmp_path, name="Good A.mkv")
    good_b = _scanned_file(tmp_path, name="Good B.mkv")
    vanished = ScannedFile(
        category="movies", absolute_path=str(tmp_path / "Vanished.mkv"), relative_path="Vanished.mkv",
        filename="Vanished.mkv", extension=".mkv", parent_directory=str(tmp_path), size_bytes=10,
        layout=Layout.MOVIE_FLAT,
    )

    with pytest.raises(FileNotFoundError):
        _persist(connection, tmp_path, [good_a, good_b, vanished])

    assert connection.execute("SELECT COUNT(*) FROM scan_changes").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM media_files").fetchone()[0] == 0

    scan_run = connection.execute("SELECT status FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert scan_run["status"] == "FAILED"


# --- determinism --------------------------------------------------------------


_FIXED_MTIME = 1_700_000_000.0


def _pin_mtime(path: Path, mtime: float = _FIXED_MTIME) -> None:
    os.utime(path, (mtime, mtime))


def test_event_details_are_deterministic(connection: sqlite3.Connection, tmp_path: Path) -> None:
    scanned = _scanned_file(tmp_path, size=10)
    _pin_mtime(Path(scanned.absolute_path))
    _persist(connection, tmp_path, [scanned])

    # Resize only -- mtime pinned to the same value both times, so
    # size_bytes is the only field that can legitimately differ.
    _touch(Path(scanned.absolute_path), size=20)
    _pin_mtime(Path(scanned.absolute_path))
    resized = ScannedFile(
        category=scanned.category, absolute_path=scanned.absolute_path, relative_path=scanned.relative_path,
        filename=scanned.filename, extension=scanned.extension, parent_directory=scanned.parent_directory,
        size_bytes=20, layout=scanned.layout,
    )
    scan_run_id = _persist(connection, tmp_path, [resized])

    row = _changes_for_scan(connection, scan_run_id)[0]
    expected = json.dumps(
        {"changes": [{"field": "size_bytes", "new": 20, "old": 10}]}, sort_keys=True, separators=(",", ":")
    )
    assert row["details_json"] == expected


def test_event_details_field_order_is_stable_across_runs(tmp_path: Path) -> None:
    """Two independent databases reconciling the same change produce
    byte-identical details_json -- not just equal-when-parsed."""
    results = []
    for i in range(2):
        db_path = tmp_path / f"run{i}.db"
        migrate(db_path, REPO_MIGRATIONS_DIR)
        conn = connect(db_path)
        run_dir = tmp_path / f"files{i}"
        run_dir.mkdir()
        scanned = _scanned_file(run_dir, size=10)
        _pin_mtime(Path(scanned.absolute_path))
        _persist(conn, run_dir, [scanned])
        _touch(Path(scanned.absolute_path), size=20)
        _pin_mtime(Path(scanned.absolute_path))
        resized = ScannedFile(
            category=scanned.category, absolute_path=scanned.absolute_path, relative_path=scanned.relative_path,
            filename=scanned.filename, extension=scanned.extension, parent_directory=scanned.parent_directory,
            size_bytes=20, layout=scanned.layout,
        )
        scan_run_id = _persist(conn, run_dir, [resized])
        row = _changes_for_scan(conn, scan_run_id)[0]
        results.append(row["details_json"])
        conn.close()

    assert results[0] == results[1]


# --- scan_runs summary counts -------------------------------------------------


def test_scan_runs_records_change_counts(connection: sqlite3.Connection, tmp_path: Path) -> None:
    a = _scanned_file(tmp_path, name="A.mkv")
    b = _scanned_file(tmp_path, name="B.mkv")
    _persist(connection, tmp_path, [a, b])  # both ADDED

    _touch(Path(a.absolute_path), size=999)
    a_resized = ScannedFile(
        category=a.category, absolute_path=a.absolute_path, relative_path=a.relative_path,
        filename=a.filename, extension=a.extension, parent_directory=a.parent_directory,
        size_bytes=999, layout=a.layout,
    )
    scan_run_id = _persist(connection, tmp_path, [a_resized])  # a UPDATED, b MISSING

    row = connection.execute(
        "SELECT added_count, updated_count, missing_count, restored_count FROM scan_runs WHERE id = ?",
        (scan_run_id,),
    ).fetchone()
    assert (row["added_count"], row["updated_count"], row["missing_count"], row["restored_count"]) == (0, 1, 1, 0)
