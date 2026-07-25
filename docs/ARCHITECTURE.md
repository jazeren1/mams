# Architecture

```text
Physical Disc → MakeMKV → Local Incoming → Verification and Identification
→ SQLite Catalog → Safe Copy or Replacement → QNAP NAS → Plex Validation
```

## Components

- Ingestion: discovers MakeMKV output and records disc context.
- Verification: parses MediaInfo, checks runtime and streams, computes checksums.
- Catalog: stores discs, assets, files, jobs, replacements, and events.
- Naming: generates canonical paths and filenames.
- Replacement engine: detects old files, backs them up, copies, verifies, rolls back, and cleans up.
- Plex integration: requests scans and confirms expected paths.

## State model

```text
NOT_STARTED
RIPPING
RIPPED
IDENTIFIED
VERIFIED
READY_TO_COPY
COPYING
COPIED
DESTINATION_VERIFIED
PLEX_SCAN_REQUESTED
PLEX_VERIFIED
COMPLETE
NEEDS_REVIEW
FAILED
```

Every transition must be logged, safe to retry, and reversible until completion. File-moving commands must support dry-run mode.

## Milestone 7B: external identity resolution and dry-run ingest planning

Milestone 7B implements the "Verification and Identification" and part of
the "Naming" stages against TMDb and the canonical inventory:
`identification_candidates` (Milestone 7A's local parse) is resolved
against TMDb into a confirmed `external_identities`/
`media_identity_assignments` pair, verified against already-collected
MediaInfo data, and turned into a canonical destination path plus a
structured, ordered dry-run plan (`ingest_plans`/`ingest_plan_actions`).

This is the first milestone to *compute* proposed file operations rather
than only observe read-only state — a meaningful boundary shift from
every milestone through 7A. It still fully honors "File-moving commands
must support dry-run mode": no executor exists yet at all. Every
proposed action is persisted labeled `PROPOSED_NOT_EXECUTED`; `mams
ingest approve` only flips a database status, never performs the move.
The "Safe Copy or Replacement" and "Plex Validation" stages — actually
moving a file, verifying its checksum at the destination, and requesting
a Plex scan — remain unimplemented, deferred to a future milestone.
