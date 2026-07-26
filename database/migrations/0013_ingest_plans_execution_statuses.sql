-- Widens ingest_plans.status to add Milestone 8's execution lifecycle
-- states (EXECUTING, EXECUTED, EXECUTION_FAILED, RECOVERY_REQUIRED).
-- SQLite has no ALTER TABLE ... ALTER COLUMN, so a CHECK-constraint
-- change requires the standard 12-step table rebuild, not a CHECK-
-- constraint drop (project decision -- DB-level enum enforcement stays
-- as rigorous as every other status column in this schema).
--
-- ingest_plan_actions is untouched by this migration: it references
-- ingest_plans(id) by name, and this rebuild ends by renaming the new
-- table back to the identical name "ingest_plans". SQLite only rewrites
-- another table's REFERENCES clause on a rename when the referenced
-- table is renamed to a genuinely different name while something still
-- points at the old one -- not the case here, so no changes to
-- ingest_plan_actions's schema text are needed or made.
--
-- PRAGMA foreign_keys must be toggled outside any transaction (it is a
-- no-op if changed while one is open), which is why it brackets the
-- BEGIN/COMMIT below rather than living inside it.
PRAGMA foreign_keys=OFF;

BEGIN TRANSACTION;

CREATE TABLE ingest_plans_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    identification_candidate_id INTEGER REFERENCES identification_candidates(id) ON DELETE SET NULL,
    media_identity_assignment_id INTEGER REFERENCES media_identity_assignments(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN
        ('DRAFT','READY_FOR_REVIEW','REVIEW_REQUIRED','BLOCKED','APPROVED','SUPERSEDED',
         'EXECUTING','EXECUTED','EXECUTION_FAILED','RECOVERY_REQUIRED')),
    source_path TEXT NOT NULL,
    source_size_bytes INTEGER,
    source_mtime REAL,
    destination_library TEXT,
    destination_directory TEXT,
    destination_filename TEXT,
    plan_version INTEGER NOT NULL DEFAULT 1,
    verification_status TEXT CHECK (verification_status IN ('PASS','WARNING','FAIL','NOT_PROBED')),
    verification_json TEXT,
    blocking_reasons_json TEXT,
    summary TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at TEXT,
    approved_by TEXT,
    executed_at TEXT
);

INSERT INTO ingest_plans_new (
    id, media_file_id, identification_candidate_id, media_identity_assignment_id, status,
    source_path, source_size_bytes, source_mtime, destination_library, destination_directory,
    destination_filename, plan_version, verification_status, verification_json,
    blocking_reasons_json, summary, created_at, updated_at, approved_at, approved_by, executed_at
)
SELECT
    id, media_file_id, identification_candidate_id, media_identity_assignment_id, status,
    source_path, source_size_bytes, source_mtime, destination_library, destination_directory,
    destination_filename, plan_version, verification_status, verification_json,
    blocking_reasons_json, summary, created_at, updated_at, approved_at, approved_by, executed_at
FROM ingest_plans;

DROP TABLE ingest_plans;

ALTER TABLE ingest_plans_new RENAME TO ingest_plans;

CREATE UNIQUE INDEX IF NOT EXISTS idx_ingest_plans_current_media_file
    ON ingest_plans(media_file_id) WHERE status != 'SUPERSEDED';
CREATE INDEX IF NOT EXISTS idx_ingest_plans_media_file_id ON ingest_plans(media_file_id);
CREATE INDEX IF NOT EXISTS idx_ingest_plans_status ON ingest_plans(status);
CREATE INDEX IF NOT EXISTS idx_ingest_plans_destination_library ON ingest_plans(destination_library);

COMMIT;

PRAGMA foreign_keys=ON;
