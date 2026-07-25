-- Dry-run ingest plans: one current plan per media file, proposing a
-- canonical destination and an ordered sequence of proposed (never
-- executed) actions. See docs/DATABASE.md ("ingest_plans",
-- "ingest_plan_actions") for full rationale. No action executor exists in
-- this milestone -- ingest_plan_actions rows are proposals for human
-- review, not a work queue.
--
-- Identity/uniqueness: at most one *current* (non-SUPERSEDED) plan per
-- media_file_id, enforced by a partial unique index -- the same pattern
-- as media_identity_assignments' at-most-one-ACTIVE-row rule.
-- Regenerating a plan against unchanged inputs updates the current row in
-- place (no SUPERSEDED, no churn); regenerating against changed inputs
-- also updates the current row in place, UNLESS it is APPROVED, in which
-- case the approved row is marked SUPERSEDED (an approval is a snapshot
-- that must never be silently mutated) and a new current plan is
-- inserted. SUPERSEDED is a schema addition beyond the milestone's
-- suggested status list, needed specifically for this rule.
CREATE TABLE IF NOT EXISTS ingest_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    identification_candidate_id INTEGER REFERENCES identification_candidates(id) ON DELETE SET NULL,
    media_identity_assignment_id INTEGER REFERENCES media_identity_assignments(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN
        ('DRAFT','READY_FOR_REVIEW','REVIEW_REQUIRED','BLOCKED','APPROVED','SUPERSEDED')),
    source_path TEXT NOT NULL,
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_ingest_plans_current_media_file
    ON ingest_plans(media_file_id) WHERE status != 'SUPERSEDED';
CREATE INDEX IF NOT EXISTS idx_ingest_plans_media_file_id ON ingest_plans(media_file_id);
CREATE INDEX IF NOT EXISTS idx_ingest_plans_status ON ingest_plans(status);
CREATE INDEX IF NOT EXISTS idx_ingest_plans_destination_library ON ingest_plans(destination_library);

-- ingest_plan_actions: ordered, proposed-only actions for one plan.
-- Recreated wholesale whenever its parent plan's content is regenerated
-- (never partially patched) -- so action_order is always a dense,
-- contiguous 1..N sequence for the plan's current content. Owned child
-- rows: ON DELETE CASCADE removes them along with their plan.
CREATE TABLE IF NOT EXISTS ingest_plan_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingest_plan_id INTEGER NOT NULL REFERENCES ingest_plans(id) ON DELETE CASCADE,
    action_order INTEGER NOT NULL CHECK (action_order >= 1),
    action_type TEXT NOT NULL CHECK (action_type IN
        ('VALIDATE_SOURCE','VERIFY_MEDIA','CREATE_DIRECTORY','RENAME','MOVE',
         'REFRESH_INVENTORY','REQUEST_PLEX_REFRESH')),
    source_path TEXT,
    destination_path TEXT,
    details_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ingest_plan_actions_plan_order
    ON ingest_plan_actions(ingest_plan_id, action_order);
CREATE INDEX IF NOT EXISTS idx_ingest_plan_actions_plan_id ON ingest_plan_actions(ingest_plan_id);
