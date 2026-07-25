-- HTTP-level cache for external metadata provider requests (TMDb search
-- and detail lookups). This is explicitly a cache, not canonical domain
-- storage -- unlike every other table added for external identity
-- resolution, it is safe to store the raw provider response here, and
-- safe to expire/overwrite rows without losing anything MAMS itself
-- owns. See docs/DATABASE.md ("provider_cache") for retention and
-- invalidation rationale.
--
-- Identity/uniqueness is (provider, request_key): request_key is a
-- deterministic digest of the normalized request (endpoint + parameters),
-- computed by src/mams/tmdb.py, so the same logical request always maps
-- to the same cache row. A fresh fetch overwrites the row in place
-- (UPDATE, not INSERT) -- there is no reason to keep stale cache history.
--
-- No foreign keys: a pure HTTP cache has no relationship to any other
-- table.
CREATE TABLE IF NOT EXISTS provider_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL CHECK (provider IN ('TMDB')),
    request_key TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    response_json TEXT,
    status_code INTEGER,
    error_message TEXT,
    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_cache_provider_request_key
    ON provider_cache(provider, request_key);
CREATE INDEX IF NOT EXISTS idx_provider_cache_expires_at ON provider_cache(expires_at);
