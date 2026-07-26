-- Milestone 8 execution history: one row per attempt to execute an
-- APPROVED plan (ingest_executions), plus an ordered, immutable-once-
-- written sequence of the concrete steps that attempt took
-- (ingest_execution_steps). This is deliberately a separate vocabulary
-- from ingest_plan_actions.action_type: that table holds *proposed*
-- actions from planning time; this one holds *actual* attempted steps
-- from execution time, and the two never need to stay in lockstep.
-- See docs/EXECUTION-SAFETY.md for the full state-machine rationale.
CREATE TABLE IF NOT EXISTS ingest_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingest_plan_id INTEGER NOT NULL REFERENCES ingest_plans(id) ON DELETE CASCADE,
    plan_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('EXECUTING','SUCCEEDED','FAILED','RECOVERY_REQUIRED')),
    transfer_strategy TEXT CHECK (transfer_strategy IN
        ('SAME_FILESYSTEM_ATOMIC_RENAME','CROSS_FILESYSTEM_COPY_VERIFY_REMOVE')),
    source_path TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    source_device_id INTEGER,
    destination_device_id INTEGER,
    checksum_algorithm TEXT NOT NULL DEFAULT 'sha256',
    source_checksum TEXT,
    destination_checksum TEXT,
    source_size_bytes INTEGER NOT NULL,
    destination_size_bytes INTEGER,
    lock_token TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    failure_step TEXT,
    failure_message TEXT,
    recovery_status TEXT CHECK (recovery_status IN
        ('NONE','PARTIAL_DESTINATION_SOURCE_INTACT','DESTINATION_VERIFIED_SOURCE_NOT_REMOVED',
         'DESTINATION_UNVERIFIED_SOURCE_REMOVED','INVENTORY_REFRESH_INCOMPLETE',
         'INTERRUPTED_STATE_UNKNOWN','OTHER_REQUIRES_MANUAL_INSPECTION')),
    source_removed_at TEXT,
    inventory_refresh_completed_at TEXT,
    plex_refresh_status TEXT CHECK (plex_refresh_status IN ('SKIPPED','SUCCEEDED','FAILED')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ingest_executions_plan_id ON ingest_executions(ingest_plan_id);
CREATE INDEX IF NOT EXISTS idx_ingest_executions_status ON ingest_executions(status);
CREATE INDEX IF NOT EXISTS idx_ingest_executions_recovery_status ON ingest_executions(recovery_status);

-- Steps are mutated in place (PENDING -> RUNNING -> a terminal status),
-- not append-only: each row corresponds to exactly one real, currently
-- in-flight attempt at that step. There is no supersede/replace concept
-- here the way there is for ingest_plans/media_identity_assignments --
-- a future retry (explicitly deferred, see docs/EXECUTION-SAFETY.md)
-- would need a whole new ingest_executions row, never a new step row.
CREATE TABLE IF NOT EXISTS ingest_execution_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingest_execution_id INTEGER NOT NULL REFERENCES ingest_executions(id) ON DELETE CASCADE,
    step_order INTEGER NOT NULL CHECK (step_order >= 1),
    step_type TEXT NOT NULL CHECK (step_type IN
        ('VALIDATE_SOURCE','CREATE_DESTINATION_DIRECTORY','STREAM_COPY_WITH_CHECKSUM',
         'COMPUTE_DESTINATION_CHECKSUM','VERIFY_CHECKSUM_MATCH','ATOMIC_RENAME','FINAL_RENAME',
         'VERIFY_DESTINATION_MEDIA','REMOVE_SOURCE','REFRESH_INVENTORY','PLEX_REFRESH','FINALIZE')),
    status TEXT NOT NULL CHECK (status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','SKIPPED')),
    started_at TEXT,
    completed_at TEXT,
    detail_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ingest_execution_steps_execution_order
    ON ingest_execution_steps(ingest_execution_id, step_order);
CREATE INDEX IF NOT EXISTS idx_ingest_execution_steps_execution_id
    ON ingest_execution_steps(ingest_execution_id);

-- Distinguishes a real directory-walk scan from the single-file
-- canonical-inventory refresh an execution performs (Milestone 8 never
-- walks a whole category root for one file), so scan history stays
-- legible about why a scan_runs row exists absent a directory walk.
--
-- SQLite's ALTER TABLE ADD COLUMN has no IF NOT EXISTS form, so this
-- statement is not safely re-runnable if a crash lands between this
-- script finishing and its schema_version row being recorded -- the
-- same narrow, accepted caveat documented in 0003_scan_changes.sql and
-- 0012_ingest_plan_source_snapshot.sql.
ALTER TABLE scan_runs ADD COLUMN triggered_by TEXT NOT NULL DEFAULT 'SCAN'
    CHECK (triggered_by IN ('SCAN','EXECUTION'));
