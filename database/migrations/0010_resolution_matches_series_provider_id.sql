-- Adds the TMDb series id to resolution_matches for EPISODE-type matches.
-- provider_id on resolution_matches (see 0008_resolution.sql) already
-- holds the episode's own distinct TMDb id, needed for
-- external_identities uniqueness; this column separately captures which
-- series that episode belongs to, so a manual `resolve select` can later
-- confirm/upsert the external identity from the persisted match alone,
-- without a live TMDb lookup. NULL for MOVIE/SERIES matches.
--
-- ALTER TABLE ADD COLUMN has no "IF NOT EXISTS" form in SQLite (same
-- accepted narrow-window risk as 0003_scan_changes.sql).
ALTER TABLE resolution_matches ADD COLUMN series_provider_id INTEGER;

CREATE INDEX IF NOT EXISTS idx_resolution_matches_series_provider_id
    ON resolution_matches(series_provider_id);
