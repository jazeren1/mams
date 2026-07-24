-- Local, deterministic parsing candidates: one row per ACTIVE media file,
-- interpreting its path/filename into a structured (but unconfirmed) movie
-- or TV identity. See docs/DATABASE.md ("identification_candidates") for
-- full schema, lifecycle, and FK-retention rationale.
--
-- This milestone never calls TMDb/TVDB/Plex or any external service --
-- candidates are local interpretations of evidence, not confirmed
-- identities. See src/mams/identification.py for the parser (pure, no SQL)
-- and src/mams/identification_repository.py for reconciliation/queries.
--
-- Identity/uniqueness is UNIQUE(media_file_id): a candidate is the current
-- interpretation of a file, not an immutable history of past parses, so a
-- re-parse updates the existing row in place rather than inserting a new
-- one (unlike scan_changes/findings, which accumulate history).
--
-- FK retention deliberately differs from scan_changes/findings, which use
-- ON DELETE SET NULL because they are audit evidence that must outlive the
-- row they describe. A candidate is not evidence of history -- it is a
-- live interpretation of a still-existing file's path, discarded and
-- reparsed on every change to that path. If a media_files row is ever
-- actually deleted (nothing does this today; MISSING is a state flip, not
-- a delete), an interpretation of a file that no longer exists is
-- meaningless, so ON DELETE CASCADE removes it along with the file.
CREATE TABLE IF NOT EXISTS identification_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id INTEGER NOT NULL UNIQUE REFERENCES media_files(id) ON DELETE CASCADE,
    candidate_type TEXT NOT NULL CHECK (candidate_type IN ('MOVIE','EPISODE','SPECIAL','EXTRA','UNKNOWN')),
    parsed_title TEXT,
    parsed_year INTEGER,
    parsed_series_title TEXT,
    season_number INTEGER,
    episode_number INTEGER,
    episode_numbers_json TEXT,
    episode_title TEXT,
    edition TEXT,
    part_number INTEGER,
    special_type TEXT,
    confidence TEXT NOT NULL CHECK (confidence IN ('HIGH','MEDIUM','LOW','UNKNOWN')),
    parser_version INTEGER NOT NULL,
    evidence_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_identification_candidates_media_file_id
    ON identification_candidates(media_file_id);
CREATE INDEX IF NOT EXISTS idx_identification_candidates_type ON identification_candidates(candidate_type);
CREATE INDEX IF NOT EXISTS idx_identification_candidates_confidence ON identification_candidates(confidence);
CREATE INDEX IF NOT EXISTS idx_identification_candidates_season_number ON identification_candidates(season_number);
CREATE INDEX IF NOT EXISTS idx_identification_candidates_parsed_year ON identification_candidates(parsed_year);
