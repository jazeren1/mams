"""Persistence for inventory scans: writes `InventoryReport` results into the
SQLite inventory schema (`libraries`, `scan_runs`, `media_files`,
`video_tracks`, `audio_tracks`, `subtitle_tracks`).

This module owns all inventory-related SQL. `inventory.py` stays a pure,
DB-unaware filesystem scanner; `cli.py` only calls `persist_scan()` and
handles the result. It never renames, moves, or deletes anything on the
NAS — the only filesystem interaction here is a read-only `stat()` per file
to capture `mtime` (see `_stat_mtime`).

Reconciliation runs in two phases against one connection:

1. Sync `libraries` from the configured categories and insert a `scan_runs`
   row, committed immediately. A scan attempt is always recorded, even if
   phase 2 then fails.
2. Reconcile `media_files`/track tables for every category whose root
   existed, in a single transaction. Any exception rolls the whole
   transaction back — no partial `media_files` or track writes survive —
   and the `scan_runs` row from phase 1 is updated to FAILED with the
   error message, in its own follow-up transaction.

A category whose root did not exist (`CategoryScan.exists is False`) is
never reconciled and never contributes a missing-file flip, so a NAS
mount being temporarily unavailable can never mark real files MISSING.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .inventory import CategoryScan, InventoryReport, ScannedFile
from .mediainfo import MediaInfo


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _stat_mtime(absolute_path: str) -> float:
    return Path(absolute_path).stat().st_mtime


def sync_libraries(connection: sqlite3.Connection, categories: dict[str, str]) -> dict[str, int]:
    """Upsert `libraries` by category from configured NAS categories.

    `config.yaml` (the `categories` dict) is authoritative for `root_path`;
    an existing row's `root_path` is overwritten and `updated_at` bumped
    only when it actually changed. Returns a category -> library_id map.
    """
    library_ids: dict[str, int] = {}
    for category, root_path in categories.items():
        existing = connection.execute(
            "SELECT id, root_path FROM libraries WHERE category = ?", (category,)
        ).fetchone()
        if existing is None:
            cursor = connection.execute(
                "INSERT INTO libraries (category, root_path) VALUES (?, ?)", (category, root_path)
            )
            library_ids[category] = _lastrowid(cursor)
        else:
            library_ids[category] = existing["id"]
            if existing["root_path"] != root_path:
                connection.execute(
                    "UPDATE libraries SET root_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (root_path, existing["id"]),
                )
    return library_ids


def start_scan_run(
    connection: sqlite3.Connection, *, metadata_enabled: bool, mediainfo_version: str | None
) -> int:
    cursor = connection.execute(
        "INSERT INTO scan_runs (metadata_enabled, mediainfo_version) VALUES (?, ?)",
        (int(metadata_enabled), mediainfo_version),
    )
    return _lastrowid(cursor)


def complete_scan_run(
    connection: sqlite3.Connection, scan_run_id: int, *, file_count: int, total_size_bytes: int
) -> None:
    connection.execute(
        """
        UPDATE scan_runs
        SET status = 'COMPLETE', completed_at = CURRENT_TIMESTAMP,
            file_count = ?, total_size_bytes = ?
        WHERE id = ?
        """,
        (file_count, total_size_bytes, scan_run_id),
    )


def fail_scan_run(connection: sqlite3.Connection, scan_run_id: int, *, error_message: str) -> None:
    connection.execute(
        """
        UPDATE scan_runs
        SET status = 'FAILED', completed_at = CURRENT_TIMESTAMP, error_message = ?
        WHERE id = ?
        """,
        (error_message, scan_run_id),
    )


def _upsert_media_file(
    connection: sqlite3.Connection, scanned_file: ScannedFile, *, library_id: int, scan_run_id: int
) -> int:
    mtime = _stat_mtime(scanned_file.absolute_path)
    existing = connection.execute(
        "SELECT id FROM media_files WHERE absolute_path = ?", (scanned_file.absolute_path,)
    ).fetchone()

    if existing is None:
        cursor = connection.execute(
            """
            INSERT INTO media_files (
                library_id, absolute_path, relative_path, filename, extension,
                parent_directory, layout, size_bytes, mtime, state,
                first_seen_scan_id, last_seen_scan_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
            """,
            (
                library_id,
                scanned_file.absolute_path,
                scanned_file.relative_path,
                scanned_file.filename,
                scanned_file.extension,
                scanned_file.parent_directory,
                scanned_file.layout.value,
                scanned_file.size_bytes,
                mtime,
                scan_run_id,
                scan_run_id,
            ),
        )
        return _lastrowid(cursor)

    media_file_id = existing["id"]
    connection.execute(
        """
        UPDATE media_files
        SET library_id = ?, relative_path = ?, filename = ?, extension = ?,
            parent_directory = ?, layout = ?, size_bytes = ?, mtime = ?,
            state = 'ACTIVE', missing_since_scan_id = NULL,
            last_seen_scan_id = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            library_id,
            scanned_file.relative_path,
            scanned_file.filename,
            scanned_file.extension,
            scanned_file.parent_directory,
            scanned_file.layout.value,
            scanned_file.size_bytes,
            mtime,
            scan_run_id,
            media_file_id,
        ),
    )
    return int(media_file_id)


def _update_media_info_success(connection: sqlite3.Connection, media_file_id: int, media_info: MediaInfo) -> None:
    connection.execute(
        """
        UPDATE media_files
        SET container = ?, duration_seconds = ?, overall_bitrate = ?,
            media_info_error = NULL, media_info_probed_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (media_info.container, media_info.duration_seconds, media_info.overall_bitrate, media_file_id),
    )


def _update_media_info_failure(connection: sqlite3.Connection, media_file_id: int, error: str) -> None:
    # Deliberately does not touch container/duration_seconds/overall_bitrate
    # or the track tables: a failed probe records the failure without
    # discarding the last successful metadata.
    connection.execute(
        """
        UPDATE media_files
        SET media_info_error = ?, media_info_probed_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (error, media_file_id),
    )


def _replace_tracks(connection: sqlite3.Connection, media_file_id: int, media_info: MediaInfo) -> None:
    connection.execute("DELETE FROM video_tracks WHERE media_file_id = ?", (media_file_id,))
    connection.execute("DELETE FROM audio_tracks WHERE media_file_id = ?", (media_file_id,))
    connection.execute("DELETE FROM subtitle_tracks WHERE media_file_id = ?", (media_file_id,))

    for index, video_track in enumerate(media_info.video_tracks):
        connection.execute(
            """
            INSERT INTO video_tracks (
                media_file_id, track_index, codec, width, height,
                aspect_ratio, frame_rate, hdr_format, bit_depth, scan_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                media_file_id,
                index,
                video_track.codec,
                video_track.width,
                video_track.height,
                video_track.aspect_ratio,
                video_track.frame_rate,
                video_track.hdr_format,
                video_track.bit_depth,
                video_track.scan_type,
            ),
        )

    for index, audio_track in enumerate(media_info.audio_tracks):
        connection.execute(
            """
            INSERT INTO audio_tracks (
                media_file_id, track_index, codec, language, channels, bitrate, is_default
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                media_file_id,
                index,
                audio_track.codec,
                audio_track.language,
                audio_track.channels,
                audio_track.bitrate,
                int(audio_track.default),
            ),
        )

    for index, subtitle_track in enumerate(media_info.subtitle_tracks):
        connection.execute(
            """
            INSERT INTO subtitle_tracks (
                media_file_id, track_index, language, is_default, is_forced
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (media_file_id, index, subtitle_track.language, int(subtitle_track.default), int(subtitle_track.forced)),
        )


def _apply_media_info(connection: sqlite3.Connection, media_file_id: int, scanned_file: ScannedFile) -> None:
    if scanned_file.media_info is not None:
        _update_media_info_success(connection, media_file_id, scanned_file.media_info)
        _replace_tracks(connection, media_file_id, scanned_file.media_info)
    elif scanned_file.media_info_error is not None:
        _update_media_info_failure(connection, media_file_id, scanned_file.media_info_error)


def persist_category_scan(
    connection: sqlite3.Connection,
    category_scan: CategoryScan,
    *,
    library_id: int,
    scan_run_id: int,
    metadata_enabled: bool,
) -> None:
    """Reconcile one already-scanned category's files into `media_files`.

    Only call this for a `CategoryScan` whose root existed; callers must
    not invoke this (or `mark_missing_files`) for a missing root.
    """
    for scanned_file in category_scan.files:
        media_file_id = _upsert_media_file(
            connection, scanned_file, library_id=library_id, scan_run_id=scan_run_id
        )
        if metadata_enabled:
            _apply_media_info(connection, media_file_id, scanned_file)


def mark_missing_files(connection: sqlite3.Connection, *, library_id: int, scan_run_id: int) -> None:
    """Flip ACTIVE files in this library not seen by this scan to MISSING.

    Track rows are left untouched — only an actual `media_files` delete
    (never performed here) cascades to them.
    """
    connection.execute(
        """
        UPDATE media_files
        SET state = 'MISSING', missing_since_scan_id = ?
        WHERE library_id = ? AND state = 'ACTIVE' AND last_seen_scan_id < ?
        """,
        (scan_run_id, library_id, scan_run_id),
    )


def persist_scan(
    connection: sqlite3.Connection,
    report: InventoryReport,
    categories: dict[str, str],
    *,
    metadata_enabled: bool,
    mediainfo_version: str | None,
) -> int:
    """Persist one `mams inventory scan` result. Returns the scan_runs id.

    See the module docstring for the two-phase commit/rollback strategy.
    Re-raises whatever exception caused a failure, after recording it on
    the scan_runs row, so the caller can decide how to surface it.
    """
    with connection:
        library_ids = sync_libraries(connection, categories)
        scan_run_id = start_scan_run(
            connection, metadata_enabled=metadata_enabled, mediainfo_version=mediainfo_version
        )

    try:
        with connection:
            for category_scan in report.categories:
                if not category_scan.exists:
                    continue
                library_id = library_ids[category_scan.category]
                persist_category_scan(
                    connection,
                    category_scan,
                    library_id=library_id,
                    scan_run_id=scan_run_id,
                    metadata_enabled=metadata_enabled,
                )
                mark_missing_files(connection, library_id=library_id, scan_run_id=scan_run_id)
            complete_scan_run(
                connection,
                scan_run_id,
                file_count=report.file_count,
                total_size_bytes=report.total_size_bytes,
            )
    except Exception as exc:
        with connection:
            fail_scan_run(connection, scan_run_id, error_message=str(exc))
        raise

    return scan_run_id
