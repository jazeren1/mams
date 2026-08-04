# Decision Log

- D-001: Archive masters are MakeMKV remuxes, not transcodes.
- D-002: Plex is not the source of truth.
- D-003: Filenames remain simple; technical metadata belongs in SQLite.
- D-004: New episodic media uses season folders; no mass migration is required.
- D-005: Replacement remains reversible until verification succeeds.
- D-006: SQLite is the first database.
- D-007: Ambiguous TV episode mapping requires human review.
- D-008: Music is out of scope for version 1.
- D-009: The Mac is staging only; permanent storage belongs on the NAS.
- D-010: Plex scans and path verification are enough for initial integration.
- D-011: TMDb is the only external identity provider for Milestone 7B;
  TVDB/IMDb/OMDb/Plex metadata are explicitly deferred.
- D-012: MAMS never infers kids-vs-adult library classification from
  TMDb genre data; the destination category is always an explicit
  human input, defaulting to REVIEW_REQUIRED when omitted.
- D-013: A movie's local year is required for auto-resolution; a movie
  or episode identity may still be manually confirmed without one.
- D-014: Confirmed external identities, local identification candidates,
  resolution attempts/matches, and dry-run ingest plans are four
  separate concepts, never merged into one table -- a local candidate
  never becomes a confirmed identity by being overwritten in place.
- D-015: Dry-run ingest planning computes proposed actions but performs
  none; approval is a database state change only until a future
  milestone implements an executor.
- D-016: "Which identity a file has" (`resolve select`) and "an operator
  has reviewed that manual choice for ingest" (`ingest confirm-identity`)
  are two separate confirmations, not one -- a `MANUAL` assignment is
  never treated as ingest-ready merely because it was selected.
- D-017: A `MISSING` canonical inventory row and an `EXECUTED`/
  `SUPERSEDED` ingest plan never block a fresh plan for the same
  destination -- both are terminal historical facts, never reused or
  reactivated, and must never be mistaken for an existing collision.
  Every other plan status still blocks.
