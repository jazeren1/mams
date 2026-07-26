# Automation Roadmap

## Phase 0 — Foundation
Documentation, configuration, SQLite schema, Python scaffolding, dry-run convention, logging.

## Phase 1 — Inventory and logging
Add discs and assets, update statuses, list pending work, import CSV.

## Phase 2 — Verification
MediaInfo JSON parsing, stream inventory, checksums, verification reports, review queue.

## Phase 3 — Identification and naming
FileBot-assisted matching, canonical names, episode mapping, ambiguity stops.

## Phase 4 — Copy and replacement
Destination planning, old-file detection, temporary backup, copy, checksum, rollback, cleanup.

**Implemented by Milestone 8** (`mams ingest execute`, see
`docs/EXECUTION-SAFETY.md`): destination planning (Milestone 7B),
same-filesystem atomic move and cross-filesystem copy-verify-remove,
SHA-256 checksum verification, and a "prefer duplicate evidence over
data loss" recovery model in place of automatic rollback (no
`ingest retry` command; a failed execution requires a fresh plan).
Old-file detection/replacement and temporary-backup staging are still
out of scope — this milestone moves a file only to a destination that
does not already exist; it never replaces an existing library file.

## Phase 5 — Plex integration
Configure Plex, map libraries, request scans, poll for expected path.

## Phase 6 — Reporting
Progress, storage, pending discs, failures, upgrade history.

## Phase 7 — Optional intelligence
Disc-layout knowledge base, runtime-assisted episode matching, title-card recognition, duplicate detection, dashboard.
