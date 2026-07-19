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
