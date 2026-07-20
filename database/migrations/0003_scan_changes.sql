-- Immutable per-scan change events, and per-scan change-count summaries.
-- See docs/DATABASE.md ("scan_changes") for full design rationale,
-- including why the foreign keys below use SET NULL rather than CASCADE.

-- Summary counts, mirroring the existing file_count/total_size_bytes
-- precedent: populated once at scan completion from a single aggregate
-- query over scan_changes, so "most recent scan summary" reads never need
-- to touch scan_changes directly. NULL until a scan completes, same as
-- file_count/total_size_bytes.
--
-- SQLite's ALTER TABLE ADD COLUMN has no IF NOT EXISTS form, so unlike the
-- CREATE TABLE/INDEX statements in this and prior migrations, this
-- migration is not safely re-runnable if a crash lands between this
-- script finishing and its schema_version row being recorded. That window
-- is narrow (a live process crash at that exact instant) and the fix is a
-- manual one-time correction, not a recurring risk; adding a bespoke
-- idempotency mechanism for one edge case was judged not worth it.
ALTER TABLE scan_runs ADD COLUMN added_count INTEGER;
ALTER TABLE scan_runs ADD COLUMN updated_count INTEGER;
ALTER TABLE scan_runs ADD COLUMN missing_count INTEGER;
ALTER TABLE scan_runs ADD COLUMN restored_count INTEGER;

CREATE TABLE IF NOT EXISTS scan_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_run_id INTEGER NOT NULL REFERENCES scan_runs(id),
    media_file_id INTEGER REFERENCES media_files(id) ON DELETE SET NULL,
    library_id INTEGER REFERENCES libraries(id) ON DELETE SET NULL,
    change_type TEXT NOT NULL CHECK (change_type IN ('ADDED','UPDATED','MISSING','RESTORED')),
    absolute_path TEXT NOT NULL,
    previous_absolute_path TEXT,
    details_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scan_changes_scan_run_id ON scan_changes(scan_run_id);
CREATE INDEX IF NOT EXISTS idx_scan_changes_media_file_id ON scan_changes(media_file_id);
CREATE INDEX IF NOT EXISTS idx_scan_changes_library_id ON scan_changes(library_id);
CREATE INDEX IF NOT EXISTS idx_scan_changes_change_type ON scan_changes(change_type);
CREATE INDEX IF NOT EXISTS idx_scan_changes_absolute_path ON scan_changes(absolute_path);
