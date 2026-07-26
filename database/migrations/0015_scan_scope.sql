-- Distinguishes a full multi-category `mams inventory scan` from a
-- `--category CATEGORY` scoped scan, so scan history stays legible about
-- which libraries a given scan_runs row actually walked and reconciled.
-- See docs/DATABASE.md ("scan_runs") and docs/ARCHITECTURE.md for the
-- full rationale.
--
-- SQLite's ALTER TABLE ADD COLUMN has no IF NOT EXISTS form, so this
-- migration is not safely re-runnable if a crash lands between this
-- script finishing and its schema_version row being recorded -- the same
-- narrow, accepted caveat documented in 0003_scan_changes.sql,
-- 0012_ingest_plan_source_snapshot.sql, and 0013's ALTER TABLE.
--
-- Every historical row (full scans, and the Milestone 8 executor's
-- targeted single-file `triggered_by='EXECUTION'` refresh) defaults to
-- scan_scope='FULL', scope_category=NULL -- neither ever had a
-- category-scoped meaning before this column existed, so the default
-- describes them accurately without rewriting history.
ALTER TABLE scan_runs ADD COLUMN scan_scope TEXT NOT NULL DEFAULT 'FULL'
    CHECK (scan_scope IN ('FULL','CATEGORY'));
ALTER TABLE scan_runs ADD COLUMN scope_category TEXT;
