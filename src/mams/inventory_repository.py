"""Persistence and reconstruction for inventory scans: writes
`InventoryReport` results into the SQLite inventory schema (`libraries`,
`scan_runs`, `media_files`, `video_tracks`, `audio_tracks`,
`subtitle_tracks`), and reads that schema back into the same domain models.

This module owns all inventory-related SQL. `inventory.py` stays a pure,
DB-unaware filesystem scanner; `cli.py` only calls `persist_scan()` and
`read_inventory_report()` and handles the results. It never renames, moves,
or deletes anything on the NAS — the only filesystem interaction here is a
read-only `stat()` per file to capture `mtime` on write (see `_stat_mtime`)
and a read-only `is_dir()` per category to reconstruct `CategoryScan.exists`
on read (see `read_inventory_report`).

## Write path

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

## Read path

`read_inventory_report()` reconstructs an `InventoryReport` identical in
shape to what `inventory.scan_categories()` would produce, from the
normalized tables — the tracks tables are canonical, there is no JSON blob
to fall back on. Only `ACTIVE` files are included; `MISSING` rows stay in
the database but are excluded from this export, matching the "database is
canonical, filesystem is discovery" principle in docs/DATABASE.md. It reads
in a fixed, small number of queries (libraries, media_files, and one query
per track table, each joined/grouped in Python) regardless of file count —
never one query per file — and never re-walks the filesystem.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from .inventory import CategoryScan, InventoryReport, Layout, ScannedFile
from .mediainfo import AudioTrack, MediaInfo, SubtitleTrack, VideoTrack


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


# --- read path -------------------------------------------------------------


def _row_to_video_track(row: sqlite3.Row) -> VideoTrack:
    return VideoTrack(
        codec=row["codec"],
        width=row["width"],
        height=row["height"],
        aspect_ratio=row["aspect_ratio"],
        frame_rate=row["frame_rate"],
        hdr_format=row["hdr_format"],
        bit_depth=row["bit_depth"],
        scan_type=row["scan_type"],
    )


def _row_to_audio_track(row: sqlite3.Row) -> AudioTrack:
    return AudioTrack(
        codec=row["codec"],
        language=row["language"],
        channels=row["channels"],
        bitrate=row["bitrate"],
        default=bool(row["is_default"]),
    )


def _row_to_subtitle_track(row: sqlite3.Row) -> SubtitleTrack:
    return SubtitleTrack(
        language=row["language"],
        default=bool(row["is_default"]),
        forced=bool(row["is_forced"]),
    )


def _fetch_tracks_by_media_file_id(
    connection: sqlite3.Connection, table: str
) -> dict[int, list[sqlite3.Row]]:
    # `table` is always one of three hardcoded literals from this module's
    # own callers below, never external input.
    rows = connection.execute(
        f"""
        SELECT {table}.*
        FROM {table}
        JOIN media_files ON media_files.id = {table}.media_file_id
        WHERE media_files.state = 'ACTIVE'
        ORDER BY {table}.media_file_id, {table}.track_index
        """
    ).fetchall()
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["media_file_id"], []).append(row)
    return grouped


def _reconstruct_media_info(
    file_row: sqlite3.Row,
    *,
    video_tracks: list[sqlite3.Row],
    audio_tracks: list[sqlite3.Row],
    subtitle_tracks: list[sqlite3.Row],
) -> tuple[MediaInfo | None, str | None]:
    """Reconstruct (media_info, media_info_error) for one media_files row.

    Three states, matching what the write path can leave behind:
    - never probed (media_info_error and media_info_probed_at both NULL):
      metadata stays absent, (None, None).
    - last probe failed (media_info_error set): the general fields and
      track tables hold whatever the last *successful* probe left there
      (see `_update_media_info_failure`), which is not re-exposed here —
      only the error is, matching what a failed scan records today.
    - last probe succeeded (media_info_probed_at set, media_info_error
      NULL): reconstruct the full MediaInfo, tracks included.
    """
    if file_row["media_info_error"] is not None:
        return None, file_row["media_info_error"]

    if file_row["media_info_probed_at"] is None:
        return None, None

    media_info = MediaInfo(
        container=file_row["container"],
        duration_seconds=file_row["duration_seconds"],
        overall_bitrate=file_row["overall_bitrate"],
        video_tracks=tuple(_row_to_video_track(row) for row in video_tracks),
        audio_tracks=tuple(_row_to_audio_track(row) for row in audio_tracks),
        subtitle_tracks=tuple(_row_to_subtitle_track(row) for row in subtitle_tracks),
    )
    return media_info, None


def _row_to_scanned_file(
    file_row: sqlite3.Row,
    *,
    category: str,
    video_tracks_by_file: dict[int, list[sqlite3.Row]],
    audio_tracks_by_file: dict[int, list[sqlite3.Row]],
    subtitle_tracks_by_file: dict[int, list[sqlite3.Row]],
) -> ScannedFile:
    media_file_id = file_row["id"]
    media_info, media_info_error = _reconstruct_media_info(
        file_row,
        video_tracks=video_tracks_by_file.get(media_file_id, []),
        audio_tracks=audio_tracks_by_file.get(media_file_id, []),
        subtitle_tracks=subtitle_tracks_by_file.get(media_file_id, []),
    )
    return ScannedFile(
        category=category,
        absolute_path=file_row["absolute_path"],
        relative_path=file_row["relative_path"],
        filename=file_row["filename"],
        extension=file_row["extension"],
        parent_directory=file_row["parent_directory"],
        size_bytes=file_row["size_bytes"],
        layout=Layout(file_row["layout"]),
        media_info=media_info,
        media_info_error=media_info_error,
    )


def read_inventory_report(connection: sqlite3.Connection, categories: dict[str, str]) -> InventoryReport:
    """Reconstruct the canonical `InventoryReport` from the database.

    `categories` (normally `config.nas_categories`) drives category order
    and `root_path`/`exists` — the same authority config.yaml has on write
    (see `sync_libraries`). A category not yet present in `libraries`
    (never synced by a scan) simply contributes no files.

    Only `ACTIVE` media_files rows are included; `MISSING` rows remain in
    the database but are excluded here. Runs a fixed number of queries
    (libraries, media_files, and the three track tables) regardless of how
    many files exist, and never touches the filesystem beyond a single
    `is_dir()` check per configured category.
    """
    library_rows = {row["category"]: row for row in connection.execute("SELECT * FROM libraries").fetchall()}

    file_rows = connection.execute(
        "SELECT * FROM media_files WHERE state = 'ACTIVE' ORDER BY library_id, relative_path"
    ).fetchall()
    files_by_library_id: dict[int, list[sqlite3.Row]] = {}
    for row in file_rows:
        files_by_library_id.setdefault(row["library_id"], []).append(row)

    video_tracks_by_file = _fetch_tracks_by_media_file_id(connection, "video_tracks")
    audio_tracks_by_file = _fetch_tracks_by_media_file_id(connection, "audio_tracks")
    subtitle_tracks_by_file = _fetch_tracks_by_media_file_id(connection, "subtitle_tracks")

    category_scans: list[CategoryScan] = []
    for category, configured_root in categories.items():
        root_path = str(Path(configured_root))
        library_row = library_rows.get(category)
        rows_for_category = files_by_library_id.get(library_row["id"], []) if library_row else []
        scanned_files = tuple(
            _row_to_scanned_file(
                row,
                category=category,
                video_tracks_by_file=video_tracks_by_file,
                audio_tracks_by_file=audio_tracks_by_file,
                subtitle_tracks_by_file=subtitle_tracks_by_file,
            )
            for row in rows_for_category
        )
        category_scans.append(
            CategoryScan(
                category=category,
                root_path=root_path,
                exists=Path(configured_root).is_dir(),
                files=scanned_files,
            )
        )

    return InventoryReport(categories=tuple(category_scans))


# --- query layer (list / stats / search) ------------------------------------
#
# Distinct from read_inventory_report() above: these return flat, lightweight
# records for browsing/filtering (mams inventory list/stats/find), not the
# fully reconstructed nested MediaInfo domain model. Every function here
# runs a fixed, small number of queries regardless of how many media_files
# rows match — never one query per row.


@dataclass(frozen=True)
class MediaFileRecord:
    """One media_files row, flattened with its category and track counts."""

    id: int
    category: str
    absolute_path: str
    relative_path: str
    filename: str
    extension: str
    parent_directory: str
    layout: str
    size_bytes: int
    mtime: float | None
    state: str
    container: str | None
    duration_seconds: float | None
    overall_bitrate: int | None
    media_info_error: str | None
    media_info_probed_at: str | None
    video_track_count: int
    audio_track_count: int
    subtitle_track_count: int
    first_seen_scan_id: int
    last_seen_scan_id: int
    missing_since_scan_id: int | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class LibraryStats:
    category: str
    root_path: str
    active_count: int
    missing_count: int
    active_total_size_bytes: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RecentScanSummary:
    id: int
    status: str
    started_at: str
    completed_at: str | None
    metadata_enabled: bool
    mediainfo_version: str | None
    file_count: int | None
    total_size_bytes: int | None
    added_count: int | None
    updated_count: int | None
    missing_count: int | None
    restored_count: int | None
    error_message: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class InventoryStats:
    libraries: tuple[LibraryStats, ...]
    active_file_count: int
    missing_file_count: int
    active_total_size_bytes: int
    layout_counts: dict[str, int]
    extension_counts: dict[str, int]
    metadata_success_count: int
    metadata_error_count: int
    metadata_not_probed_count: int
    video_track_count: int
    audio_track_count: int
    subtitle_track_count: int
    most_recent_scan: RecentScanSummary | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_MEDIA_FILE_BASE_SELECT = """
    SELECT
        media_files.*,
        libraries.category AS category,
        (SELECT COUNT(*) FROM video_tracks WHERE video_tracks.media_file_id = media_files.id)
            AS video_track_count,
        (SELECT COUNT(*) FROM audio_tracks WHERE audio_tracks.media_file_id = media_files.id)
            AS audio_track_count,
        (SELECT COUNT(*) FROM subtitle_tracks WHERE subtitle_tracks.media_file_id = media_files.id)
            AS subtitle_track_count
    FROM media_files
    JOIN libraries ON libraries.id = media_files.library_id
"""


def _row_to_media_file_record(row: sqlite3.Row) -> MediaFileRecord:
    return MediaFileRecord(
        id=row["id"],
        category=row["category"],
        absolute_path=row["absolute_path"],
        relative_path=row["relative_path"],
        filename=row["filename"],
        extension=row["extension"],
        parent_directory=row["parent_directory"],
        layout=row["layout"],
        size_bytes=row["size_bytes"],
        mtime=row["mtime"],
        state=row["state"],
        container=row["container"],
        duration_seconds=row["duration_seconds"],
        overall_bitrate=row["overall_bitrate"],
        media_info_error=row["media_info_error"],
        media_info_probed_at=row["media_info_probed_at"],
        video_track_count=row["video_track_count"],
        audio_track_count=row["audio_track_count"],
        subtitle_track_count=row["subtitle_track_count"],
        first_seen_scan_id=row["first_seen_scan_id"],
        last_seen_scan_id=row["last_seen_scan_id"],
        missing_since_scan_id=row["missing_since_scan_id"],
    )


def list_media_files(
    connection: sqlite3.Connection,
    *,
    category: str | None = None,
    state: str | None = None,
    layout: str | None = None,
    metadata_error: bool | None = None,
    limit: int | None = None,
) -> list[MediaFileRecord]:
    """List media_files rows for browsing, with deterministic ordering.

    All filters are optional and combined with AND. `metadata_error=True`
    restricts to rows with a recorded probe error; `metadata_error=False`
    restricts to rows without one; `None` (default) applies no filter.
    Ordered by (category, relative_path, id) — stable regardless of
    insertion order.
    """
    clauses: list[str] = []
    params: list[object] = []
    if category is not None:
        clauses.append("libraries.category = ?")
        params.append(category)
    if state is not None:
        clauses.append("media_files.state = ?")
        params.append(state)
    if layout is not None:
        clauses.append("media_files.layout = ?")
        params.append(layout)
    if metadata_error is True:
        clauses.append("media_files.media_info_error IS NOT NULL")
    elif metadata_error is False:
        clauses.append("media_files.media_info_error IS NULL")

    sql = _MEDIA_FILE_BASE_SELECT
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY libraries.category, media_files.relative_path, media_files.id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    rows = connection.execute(sql, params).fetchall()
    return [_row_to_media_file_record(row) for row in rows]


def search_media_files(
    connection: sqlite3.Connection,
    query: str,
    *,
    category: str | None = None,
    state: str | None = None,
    limit: int | None = None,
) -> list[MediaFileRecord]:
    """Case-insensitive substring search over filename/relative_path/absolute_path.

    Parameterized throughout — `query` is never interpolated into the SQL
    text. Case-insensitivity relies on SQLite's built-in `LOWER()`, which is
    ASCII-only without a loaded extension; non-ASCII characters (e.g. an
    accented title) won't case-fold, though they still match as typed.
    Ordered the same deterministic way as `list_media_files`.
    """
    pattern = f"%{query.lower()}%"
    clauses = [
        "(LOWER(media_files.filename) LIKE ? "
        "OR LOWER(media_files.relative_path) LIKE ? "
        "OR LOWER(media_files.absolute_path) LIKE ?)"
    ]
    params: list[object] = [pattern, pattern, pattern]
    if category is not None:
        clauses.append("libraries.category = ?")
        params.append(category)
    if state is not None:
        clauses.append("media_files.state = ?")
        params.append(state)

    sql = _MEDIA_FILE_BASE_SELECT + " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY libraries.category, media_files.relative_path, media_files.id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    rows = connection.execute(sql, params).fetchall()
    return [_row_to_media_file_record(row) for row in rows]


def _row_to_recent_scan_summary(row: sqlite3.Row) -> RecentScanSummary:
    return RecentScanSummary(
        id=row["id"],
        status=row["status"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        metadata_enabled=bool(row["metadata_enabled"]),
        mediainfo_version=row["mediainfo_version"],
        file_count=row["file_count"],
        total_size_bytes=row["total_size_bytes"],
        added_count=row["added_count"],
        updated_count=row["updated_count"],
        missing_count=row["missing_count"],
        restored_count=row["restored_count"],
        error_message=row["error_message"],
    )


def get_inventory_stats(connection: sqlite3.Connection) -> InventoryStats:
    """Aggregate current-inventory statistics in six fixed queries, none of
    them per-file: per-library counts/sizes, layout counts, extension
    counts, metadata success/error/not-probed counts, track counts, and the
    single most recent scan_runs row (regardless of its status)."""
    library_rows = connection.execute(
        """
        SELECT
            libraries.category,
            libraries.root_path,
            COUNT(CASE WHEN media_files.state = 'ACTIVE' THEN 1 END) AS active_count,
            COUNT(CASE WHEN media_files.state = 'MISSING' THEN 1 END) AS missing_count,
            COALESCE(SUM(CASE WHEN media_files.state = 'ACTIVE' THEN media_files.size_bytes END), 0)
                AS active_total_size_bytes
        FROM libraries
        LEFT JOIN media_files ON media_files.library_id = libraries.id
        GROUP BY libraries.id
        ORDER BY libraries.category
        """
    ).fetchall()
    libraries = tuple(
        LibraryStats(
            category=row["category"],
            root_path=row["root_path"],
            active_count=row["active_count"],
            missing_count=row["missing_count"],
            active_total_size_bytes=row["active_total_size_bytes"],
        )
        for row in library_rows
    )

    layout_counts = {
        row["layout"]: row["n"]
        for row in connection.execute(
            "SELECT layout, COUNT(*) AS n FROM media_files WHERE state = 'ACTIVE' GROUP BY layout"
        ).fetchall()
    }
    extension_counts = {
        row["extension"]: row["n"]
        for row in connection.execute(
            "SELECT extension, COUNT(*) AS n FROM media_files WHERE state = 'ACTIVE' GROUP BY extension"
        ).fetchall()
    }

    metadata_row = connection.execute(
        """
        SELECT
            SUM(CASE WHEN media_info_error IS NOT NULL THEN 1 ELSE 0 END) AS error_count,
            SUM(CASE WHEN media_info_error IS NULL AND media_info_probed_at IS NOT NULL THEN 1 ELSE 0 END)
                AS success_count,
            SUM(CASE WHEN media_info_probed_at IS NULL THEN 1 ELSE 0 END) AS not_probed_count
        FROM media_files
        WHERE state = 'ACTIVE'
        """
    ).fetchone()

    track_row = connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM video_tracks JOIN media_files ON media_files.id = video_tracks.media_file_id
                WHERE media_files.state = 'ACTIVE') AS video_count,
            (SELECT COUNT(*) FROM audio_tracks JOIN media_files ON media_files.id = audio_tracks.media_file_id
                WHERE media_files.state = 'ACTIVE') AS audio_count,
            (SELECT COUNT(*) FROM subtitle_tracks JOIN media_files ON media_files.id = subtitle_tracks.media_file_id
                WHERE media_files.state = 'ACTIVE') AS subtitle_count
        """
    ).fetchone()

    scan_row = connection.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()

    return InventoryStats(
        libraries=libraries,
        active_file_count=sum(lib.active_count for lib in libraries),
        missing_file_count=sum(lib.missing_count for lib in libraries),
        active_total_size_bytes=sum(lib.active_total_size_bytes for lib in libraries),
        layout_counts=layout_counts,
        extension_counts=extension_counts,
        metadata_success_count=metadata_row["success_count"] or 0,
        metadata_error_count=metadata_row["error_count"] or 0,
        metadata_not_probed_count=metadata_row["not_probed_count"] or 0,
        video_track_count=track_row["video_count"],
        audio_track_count=track_row["audio_count"],
        subtitle_track_count=track_row["subtitle_count"],
        most_recent_scan=_row_to_recent_scan_summary(scan_row) if scan_row is not None else None,
    )
