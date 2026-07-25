-- Resolution lifecycle: one historical resolution_attempts row per
-- evaluation of a local identification_candidate against TMDb, with its
-- ranked resolution_matches persisted alongside it, and the confirmed
-- outcome (if any) recorded as a media_identity_assignments row. See
-- docs/DATABASE.md ("resolution_attempts", "resolution_matches",
-- "media_identity_assignments") for full rationale.
--
-- resolution_attempts is an append-only history, like scan_changes/
-- findings, not a reconcile-in-place table like identification_candidates:
-- a candidate can be re-evaluated (`mams resolve evaluate --force`, or a
-- manual re-run) many times over its life, and every attempt is kept, not
-- overwritten -- "resolution attempts should be historical records and
-- should not disappear if a candidate changes later" (Milestone 7B).
-- identification_candidate_id/media_file_id therefore use ON DELETE SET
-- NULL: the same "evidence must outlive its subject" reasoning as
-- scan_changes/findings (nothing deletes these rows today, but if it ever
-- did, the historical attempt survives, only losing its resolvable link).
--
-- selected_match_id forward-references resolution_matches, defined later
-- in this same file. SQLite does not require the referenced table to
-- exist at CREATE TABLE time for a foreign key to be declared -- only at
-- the time a row is actually inserted/updated -- so this circular
-- reference (attempts -> matches -> attempts) is safe.
CREATE TABLE IF NOT EXISTS resolution_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identification_candidate_id INTEGER REFERENCES identification_candidates(id) ON DELETE SET NULL,
    media_file_id INTEGER REFERENCES media_files(id) ON DELETE SET NULL,
    provider TEXT NOT NULL CHECK (provider IN ('TMDB')),
    status TEXT NOT NULL CHECK (status IN ('PENDING','RESOLVED','REVIEW_REQUIRED','NO_MATCH','FAILED','SKIPPED')),
    query_text TEXT,
    query_year INTEGER,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    error_message TEXT,
    selected_match_id INTEGER REFERENCES resolution_matches(id) ON DELETE SET NULL,
    algorithm_version INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_resolution_attempts_candidate_id
    ON resolution_attempts(identification_candidate_id);
CREATE INDEX IF NOT EXISTS idx_resolution_attempts_media_file_id ON resolution_attempts(media_file_id);
CREATE INDEX IF NOT EXISTS idx_resolution_attempts_status ON resolution_attempts(status);
CREATE INDEX IF NOT EXISTS idx_resolution_attempts_started_at ON resolution_attempts(started_at);

-- resolution_matches: the ranked, scored candidate matches returned for
-- one attempt -- "preserve the ranked alternatives used to make or review
-- a decision" (Milestone 7B). Owned child rows of their attempt:
-- ON DELETE CASCADE mirrors identification_candidates' rationale (nothing
-- deletes resolution_attempts today; if it ever did, a match without its
-- parent attempt describes nothing). scoring_json is a compact,
-- deterministic encoding (json.dumps(..., sort_keys=True,
-- separators=(",", ":"))) of the component scores/evidence that produced
-- `score` -- never the raw TMDb response (see provider_cache for that).
CREATE TABLE IF NOT EXISTS resolution_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resolution_attempt_id INTEGER NOT NULL REFERENCES resolution_attempts(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('TMDB')),
    provider_media_type TEXT NOT NULL CHECK (provider_media_type IN ('MOVIE','SERIES','EPISODE')),
    provider_id INTEGER NOT NULL,
    title TEXT,
    release_year INTEGER,
    series_title TEXT,
    season_number INTEGER,
    episode_number INTEGER,
    score REAL NOT NULL CHECK (score >= 0.0 AND score <= 1.0),
    rank INTEGER NOT NULL CHECK (rank >= 1),
    scoring_json TEXT NOT NULL,
    selected INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_resolution_matches_attempt_rank
    ON resolution_matches(resolution_attempt_id, rank);
CREATE UNIQUE INDEX IF NOT EXISTS idx_resolution_matches_attempt_provider_id
    ON resolution_matches(resolution_attempt_id, provider_media_type, provider_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_resolution_matches_one_selected_per_attempt
    ON resolution_matches(resolution_attempt_id) WHERE selected = 1;
CREATE INDEX IF NOT EXISTS idx_resolution_matches_attempt_id ON resolution_matches(resolution_attempt_id);

-- media_identity_assignments: links a media file to the external identity
-- confirmed for it (AUTO or MANUAL). Append-only history, like
-- resolution_attempts -- selecting a new identity supersedes the prior
-- assignment (status -> SUPERSEDED) rather than overwriting it in place,
-- so "a local candidate and a confirmed external identity are different
-- concepts" (Milestone 7B) stays auditable across changes of mind.
-- A partial unique index enforces at most one ACTIVE assignment per file
-- at a time. external_identity_id has no ON DELETE clause: nothing in
-- this milestone ever deletes an external_identities row.
CREATE TABLE IF NOT EXISTS media_identity_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    identification_candidate_id INTEGER REFERENCES identification_candidates(id) ON DELETE SET NULL,
    external_identity_id INTEGER NOT NULL REFERENCES external_identities(id),
    resolution_attempt_id INTEGER REFERENCES resolution_attempts(id) ON DELETE SET NULL,
    assignment_method TEXT NOT NULL CHECK (assignment_method IN ('AUTO','MANUAL')),
    confidence TEXT NOT NULL CHECK (confidence IN ('HIGH','MEDIUM','LOW')),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','SUPERSEDED','REVOKED')) DEFAULT 'ACTIVE',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_media_identity_assignments_active_media_file
    ON media_identity_assignments(media_file_id) WHERE status = 'ACTIVE';
CREATE INDEX IF NOT EXISTS idx_media_identity_assignments_media_file_id
    ON media_identity_assignments(media_file_id);
CREATE INDEX IF NOT EXISTS idx_media_identity_assignments_external_identity_id
    ON media_identity_assignments(external_identity_id);
CREATE INDEX IF NOT EXISTS idx_media_identity_assignments_status
    ON media_identity_assignments(status);
CREATE INDEX IF NOT EXISTS idx_media_identity_assignments_resolution_attempt_id
    ON media_identity_assignments(resolution_attempt_id);
