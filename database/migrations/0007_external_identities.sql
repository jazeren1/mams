-- Confirmed or selected external media identities (TMDb only in this
-- milestone). This is canonical domain storage, not a cache -- see
-- provider_cache (0006), which this table is deliberately kept separate
-- from. Only the specific fields MAMS needs for matching, display,
-- planning, and future verification are stored; an uncontrolled full
-- provider response is never persisted here. See docs/DATABASE.md
-- ("external_identities") for full rationale.
--
-- Identity/uniqueness is (provider, media_type, provider_id): TMDb's id
-- namespaces are per-endpoint, not global, so a movie id and a tv id (or
-- an episode id) can collide numerically -- media_type must be part of
-- the natural key. series_provider_id denormalizes the TMDb show id onto
-- an EPISODE row (rather than a foreign key to a SERIES row) so an
-- episode identity is self-contained and never requires a SERIES row to
-- exist first.
--
-- TMDb has no dedicated "special" concept -- specials are season-0
-- episodes. A season-0 TMDb episode is stored as media_type='EPISODE'
-- with season_number=0; media_type='SPECIAL' is reserved schema for a
-- future non-TMDb provider or explicit local override and is not produced
-- by this milestone's TMDb resolution path.
--
-- No foreign key to media_files/identification_candidates: an external
-- identity is provider-scoped truth about a movie/show/episode,
-- independent of which local files (if any) end up linked to it via
-- media_identity_assignments.
CREATE TABLE IF NOT EXISTS external_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL CHECK (provider IN ('TMDB')),
    media_type TEXT NOT NULL CHECK (media_type IN ('MOVIE','SERIES','EPISODE','SPECIAL')),
    provider_id INTEGER NOT NULL,
    title TEXT,
    original_title TEXT,
    release_year INTEGER,
    release_date TEXT,
    series_provider_id INTEGER,
    season_number INTEGER,
    episode_number INTEGER,
    episode_title TEXT,
    runtime_seconds INTEGER,
    original_language TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_external_identities_provider_type_id
    ON external_identities(provider, media_type, provider_id);
CREATE INDEX IF NOT EXISTS idx_external_identities_series_provider_id
    ON external_identities(series_provider_id);
CREATE INDEX IF NOT EXISTS idx_external_identities_media_type ON external_identities(media_type);
CREATE INDEX IF NOT EXISTS idx_external_identities_release_year ON external_identities(release_year);
