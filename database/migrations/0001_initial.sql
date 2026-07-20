-- Cataloging schema: the rip -> identify -> replace -> Plex pipeline
-- described in docs/ARCHITECTURE.md. Not yet wired to any code.

CREATE TABLE IF NOT EXISTS discs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    media_type TEXT NOT NULL CHECK (media_type IN ('DVD','BLURAY','UHD_BLURAY')),
    edition TEXT,
    season_number INTEGER,
    disc_number INTEGER,
    barcode TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    disc_id TEXT,
    asset_type TEXT NOT NULL CHECK (asset_type IN ('MOVIE','EPISODE','BONUS')),
    category TEXT NOT NULL CHECK (category IN ('MOVIES','KIDS_MOVIES','TV','KIDS_SHOWS','FITNESS')),
    title TEXT NOT NULL,
    year INTEGER,
    series_title TEXT,
    season_number INTEGER,
    episode_number INTEGER,
    episode_title TEXT,
    source_type TEXT NOT NULL CHECK (source_type IN ('DVD','BLURAY','UHD_BLURAY')),
    status TEXT NOT NULL DEFAULT 'NOT_STARTED',
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (disc_id) REFERENCES discs(id)
);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('STAGING','DESTINATION','BACKUP','OLD_VERSION')),
    path TEXT NOT NULL,
    container TEXT,
    video_codec TEXT,
    audio_summary TEXT,
    subtitle_summary TEXT,
    runtime_seconds REAL,
    size_bytes INTEGER,
    sha256 TEXT,
    mediainfo_json TEXT,
    verified_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (asset_id) REFERENCES assets(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_files_path ON files(path);
CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);
CREATE INDEX IF NOT EXISTS idx_assets_title_year ON assets(title, year);
CREATE INDEX IF NOT EXISTS idx_assets_series_episode ON assets(series_title, season_number, episode_number);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    asset_id TEXT,
    job_type TEXT NOT NULL,
    state TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 1,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    error_message TEXT,
    detail_json TEXT,
    FOREIGN KEY (asset_id) REFERENCES assets(id)
);

CREATE TABLE IF NOT EXISTS replacements (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    old_file_id TEXT,
    new_file_id TEXT NOT NULL,
    backup_path TEXT,
    plex_verified INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (asset_id) REFERENCES assets(id),
    FOREIGN KEY (old_file_id) REFERENCES files(id),
    FOREIGN KEY (new_file_id) REFERENCES files(id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id TEXT,
    event_type TEXT NOT NULL,
    detail_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (asset_id) REFERENCES assets(id)
);
