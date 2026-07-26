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

## Milestone 7C: live provider acceptance and the execution-readiness audit

Milestone 7C validates 7B's pipeline against the real TMDb API instead of
only a fake in-memory provider, adds an explicit operator override for
the one local-parsing gap that blocked some Incoming files from ever
reaching TMDb (`identification_overrides`, resolved via the *effective
candidate* — override if active, else the parsed one), and adds
`readiness.py`'s execution-readiness audit (`mams ingest audit PLAN_ID`)
as the formal contract between this milestone and Milestone 8.

That contract is deliberately narrow: the audit *reads* an `APPROVED`
plan's current state — source, identity, verification snapshot,
destination, and proposed actions — against fresh database/config/
read-only-filesystem lookups, and reports one of
`READY_FOR_EXECUTOR`/`STALE`/`BLOCKED`/`INCOMPLETE`. It never mutates
anything, never regenerates a plan, and never re-resolves an identity —
"the audit reports state; explicit commands perform changes." A future
Milestone 8 executor is expected to require `READY_FOR_EXECUTOR` from
this audit immediately before acting on a plan, but no executor exists
anywhere in this codebase yet, and this milestone still moves, copies,
renames, deletes, or replaces nothing, and never requests a Plex scan.

## Milestone 8: safe approved-plan execution

Milestone 8 implements the "Safe Copy or Replacement" stage — the first
code in this repository that mutates the filesystem at all. `mams
ingest execute PLAN_ID --confirm-plan PLAN_ID` moves exactly one
`APPROVED`, `READY_FOR_EXECUTOR` plan's file from Incoming to its
canonical NAS destination, one plan at a time, only on explicit
operator confirmation. See `docs/EXECUTION-SAFETY.md` for the full
state machine, locking strategy, transfer strategies, checksum policy,
and recovery scenarios.

The primary safety principle: no plan action is trusted merely because
it was approved earlier, or even because the readiness audit passed a
moment ago. The audit is re-run fresh immediately before acquiring the
execution lock, and — after the lock is held — live filesystem
evidence (fresh `stat()`s, device IDs, free space) is gathered and
checked again (`execution.evaluate_preflight()`) before any mutation
begins.

Two transfer strategies, decided from live device IDs rather than a
path heuristic: `SAME_FILESYSTEM_ATOMIC_RENAME` (an `os.link` +
`os.unlink` hard-link move — atomic and inherently no-clobber, since
`os.link()` raises natively if the destination exists) and
`CROSS_FILESYSTEM_COPY_VERIFY_REMOVE` (stream copy with an
incrementally-computed checksum, an independent re-read checksum of
the written file, an atomic commit using the same no-clobber
hard-link primitive, fresh destination verification, and only then
source removal). No destination is ever overwritten; a failure at any
point leaves duplicate or partial evidence on disk rather than
guessing and deleting something that might be the only good copy.

Canonical inventory is refreshed for the one file that moved
(`inventory_repository.relocate_media_file()`), never by re-walking an
entire NAS category — the moved file keeps its existing `media_files`
row and id, so every plan/assignment/finding that already references it
stays correctly linked across the move. Plex refresh stays disabled by
default (`execution.enable_plex_refresh: false`); no Plex client exists
in this codebase, so the step always records `SKIPPED`.

There is no automatic retry: a failed or `RECOVERY_REQUIRED` execution
requires an operator to inspect (`mams ingest recovery EXECUTION_ID`,
strictly read-only) and generate a fresh plan for another attempt.
