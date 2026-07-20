-- Inventory schema: what `mams inventory scan` discovers on the NAS.
-- See docs/DATABASE.md for the full design rationale. The database is the
-- canonical inventory; the filesystem is the source of discovery, not the
-- source of truth.

CREATE TABLE IF NOT EXISTS libraries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL UNIQUE,
    root_path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_libraries_category ON libraries(category);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('RUNNING','COMPLETE','FAILED')) DEFAULT 'RUNNING',
    metadata_enabled INTEGER NOT NULL DEFAULT 0,
    mediainfo_version TEXT,
    file_count INTEGER,
    total_size_bytes INTEGER,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id INTEGER NOT NULL REFERENCES libraries(id),
    absolute_path TEXT NOT NULL UNIQUE,
    relative_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    extension TEXT NOT NULL,
    parent_directory TEXT NOT NULL,
    layout TEXT NOT NULL CHECK (layout IN
        ('movie_flat','movie_folder','movie_collection_folder',
         'tv_series_folder','tv_season_folder','unknown')),
    size_bytes INTEGER NOT NULL,
    mtime REAL,
    state TEXT NOT NULL CHECK (state IN ('ACTIVE','MISSING')) DEFAULT 'ACTIVE',

    container TEXT,
    duration_seconds REAL,
    overall_bitrate INTEGER,
    media_info_error TEXT,
    media_info_probed_at TEXT,

    first_seen_scan_id INTEGER NOT NULL REFERENCES scan_runs(id),
    last_seen_scan_id INTEGER NOT NULL REFERENCES scan_runs(id),
    missing_since_scan_id INTEGER REFERENCES scan_runs(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_media_files_absolute_path ON media_files(absolute_path);
CREATE INDEX IF NOT EXISTS idx_media_files_library_id ON media_files(library_id);
CREATE INDEX IF NOT EXISTS idx_media_files_state ON media_files(state);
CREATE INDEX IF NOT EXISTS idx_media_files_library_layout ON media_files(library_id, layout);
CREATE INDEX IF NOT EXISTS idx_media_files_last_seen ON media_files(last_seen_scan_id);

CREATE TABLE IF NOT EXISTS video_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    track_index INTEGER NOT NULL,
    codec TEXT,
    width INTEGER,
    height INTEGER,
    aspect_ratio TEXT,
    frame_rate REAL,
    hdr_format TEXT,
    bit_depth INTEGER,
    scan_type TEXT
);

CREATE INDEX IF NOT EXISTS idx_video_tracks_media_file_id ON video_tracks(media_file_id);
CREATE INDEX IF NOT EXISTS idx_video_tracks_hdr ON video_tracks(hdr_format) WHERE hdr_format IS NOT NULL;

CREATE TABLE IF NOT EXISTS audio_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    track_index INTEGER NOT NULL,
    codec TEXT,
    language TEXT,
    channels INTEGER,
    bitrate INTEGER,
    is_default INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_audio_tracks_media_file_id ON audio_tracks(media_file_id);
CREATE INDEX IF NOT EXISTS idx_audio_tracks_language ON audio_tracks(language);

CREATE TABLE IF NOT EXISTS subtitle_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    track_index INTEGER NOT NULL,
    language TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    is_forced INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_subtitle_tracks_media_file_id ON subtitle_tracks(media_file_id);
CREATE INDEX IF NOT EXISTS idx_subtitle_tracks_language ON subtitle_tracks(language);
