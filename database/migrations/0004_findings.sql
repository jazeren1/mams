-- Read-only findings engine: persisted, deterministic conditions detected
-- by evaluating canonical inventory records. See docs/DATABASE.md
-- ("findings") for full schema, lifecycle, and reconciliation rationale.
--
-- Identity/uniqueness is (rule_code, media_file_id): one row per rule/file
-- pair, reconciled in place on every `mams findings evaluate` run rather
-- than reinserted. media_file_id/library_id are nullable with
-- ON DELETE SET NULL -- the same "evidence must outlive the row it
-- describes" reasoning as scan_changes (see docs/DATABASE.md) -- so a
-- finding's history is never destroyed by a future media_files delete,
-- even though nothing deletes media_files rows today.
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_code TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('INFO','WARNING','ERROR','CRITICAL')),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','RESOLVED','IGNORED')) DEFAULT 'ACTIVE',
    media_file_id INTEGER REFERENCES media_files(id) ON DELETE SET NULL,
    library_id INTEGER REFERENCES libraries(id) ON DELETE SET NULL,
    summary TEXT NOT NULL,
    evidence_json TEXT,
    recommendation TEXT,
    first_detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_findings_rule_media_file ON findings(rule_code, media_file_id);
CREATE INDEX IF NOT EXISTS idx_findings_status ON findings(status);
CREATE INDEX IF NOT EXISTS idx_findings_severity ON findings(severity);
CREATE INDEX IF NOT EXISTS idx_findings_rule_code ON findings(rule_code);
CREATE INDEX IF NOT EXISTS idx_findings_media_file_id ON findings(media_file_id);
CREATE INDEX IF NOT EXISTS idx_findings_library_id ON findings(library_id);
