-- Identification overrides (Milestone 7C, Phase D): an explicit,
-- database-only, operator-controlled override of a file's local
-- identification, kept entirely separate from identification_candidates
-- so the parser's own evidence (parsed_title/parsed_year/evidence_json/
-- etc.) is never overwritten or lost. See docs/DATABASE.md
-- ("identification_overrides") for full rationale.
--
-- Identity/uniqueness: at most one *active* (cleared_at IS NULL) override
-- per media_file_id, enforced by a partial unique index -- the same
-- at-most-one-current-row pattern as media_identity_assignments'
-- at-most-one-ACTIVE-row rule and ingest_plans' at-most-one-current-plan
-- rule. Creating a new override clears any existing active one first
-- (identification_repository.create_override) rather than overwriting it
-- in place, so the override history (what was overridden, when, and why)
-- stays fully auditable across changes of mind -- the same "supersede,
-- don't overwrite" discipline as media_identity_assignments.
--
-- media_file_id uses ON DELETE CASCADE, not SET NULL: like
-- identification_candidates (see docs/DATABASE.md), an override is the
-- current operator interpretation of a file that still exists, not
-- evidence that must outlive it. Nothing deletes media_files rows today.
CREATE TABLE IF NOT EXISTS identification_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    candidate_type TEXT NOT NULL CHECK (candidate_type IN ('MOVIE','EPISODE')),
    title TEXT,
    year INTEGER,
    series_title TEXT,
    season_number INTEGER,
    episode_numbers_json TEXT,
    episode_title TEXT,
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    cleared_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_identification_overrides_active_media_file
    ON identification_overrides(media_file_id) WHERE cleared_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_identification_overrides_media_file_id
    ON identification_overrides(media_file_id);

-- Links a resolution attempt to whichever override (if any) was active
-- when the attempt ran, so a later readiness audit (Phase F) can tell
-- "an override changed since this attempt/assignment" apart from
-- "nothing changed" -- without that, current_candidate_matches_plan has
-- no way to detect an override added/cleared after the fact, since the
-- underlying identification_candidates row itself doesn't change. NULL
-- means the attempt was made against the plain parsed candidate.
--
-- SQLite's ALTER TABLE ADD COLUMN has no IF NOT EXISTS form, so unlike
-- the CREATE TABLE/INDEX statements above, this statement is not safely
-- re-runnable if a crash lands between this script finishing and its
-- schema_version row being recorded -- the same narrow, accepted caveat
-- documented in 0003_scan_changes.sql.
ALTER TABLE resolution_attempts ADD COLUMN identification_override_id
    INTEGER REFERENCES identification_overrides(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_resolution_attempts_override_id
    ON resolution_attempts(identification_override_id);
