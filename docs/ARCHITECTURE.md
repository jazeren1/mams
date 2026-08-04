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

## Milestone 8.2: category-scoped inventory scanning

A full `mams inventory scan --metadata` walks and MediaInfo-probes every
configured category — on the production library (~3,500 files, 5.9 TB)
this takes roughly two hours, which is not viable for the intended
rolling ingest workflow of ripping one or two discs into `Incoming` at a
time (see `docs/INGEST-WORKFLOW.md`, "Rolling ingest (Incoming-only)").

`mams inventory scan --category CATEGORY [--metadata]` restricts *both*
discovery and reconciliation to exactly one configured category —
typically `incoming`. This is not a new scanner: `inventory.py`'s
filesystem walk, `mediainfo.py`'s probing, and
`inventory_repository.py`'s reconciliation are all already generic over
"whatever category → root-path mapping they're given"; a scoped scan
simply passes a single-entry mapping through the same pipeline a full
scan uses. Concretely, this means:

- Categories absent from that mapping are never walked, never probed,
  and never reconciled — `sync_libraries()` never touches their
  `libraries` row, and `mark_missing_files()` (which flips stale `ACTIVE`
  rows to `MISSING`) is only ever invoked for a category present in the
  scan's result set, so it structurally cannot mark an unselected
  category's files missing.
- A missing/unmounted NAS root outside the selected category is never
  even `stat()`-ed, let alone reported on — an Incoming-only scan
  succeeds whether or not the NAS is mounted.
- `scan_runs` gains `scan_scope` (`FULL`/`CATEGORY`) and
  `scope_category`, so scan history distinguishes "walked everything"
  from "walked just this one category" (see `docs/DATABASE.md`).
- Scoped reports write to `reports/library-{category}.json` /
  `reports/library-summary-{category}.txt` by default, never the
  full-scan `reports/library.json` — a bare `--category CATEGORY` can
  never overwrite the full-library report.

Omitting `--category` preserves the original full-scan behavior exactly
(same default report paths, same JSON/text shape). No identification,
resolution, planning, or execution is triggered by a scan, scoped or
not — that remains a separate, explicit step in the operator workflow.

## Milestone 8.3: explicit ingest confirmation for manually selected identities

`resolve select ATTEMPT_ID MATCH_ID` confirms *which* external identity a
file has (creating a `MANUAL` `media_identity_assignments` row), but
`ingest_service.generate_plan` has always separately required that
identity to be reviewed *for ingest* before a plan can reach
`READY_FOR_REVIEW` — a `MANUAL` assignment unconditionally produced the
review reason "identity was manually selected and has not yet been
confirmed for ingest". No command ever existed to satisfy that reason:
`resolve select` doesn't clear it (by design — it's a different,
identity-level confirmation), and no amount of re-running `ingest plan`
changes a permanent `assignment_method`. A plan generated against a
`MANUAL` assignment (e.g. after a runtime-disagreement `REVIEW_REQUIRED`
resolution attempt) was therefore permanently stuck `REVIEW_REQUIRED`,
unable to reach `APPROVED` through any existing command.

`mams ingest confirm-identity PLAN_ID` closes that gap: it validates that
the plan's snapshotted identity assignment is still the file's current
`ACTIVE` one (rejecting a stale/superseded assignment with a clear error
directing the operator to regenerate the plan first), then sets
`media_identity_assignments.confirmed_for_ingest_at`/`confirmed_by`
(migration `0016`) on that exact assignment row via
`resolution_repository.confirm_assignment_for_ingest`. It never changes
the plan's own status — the operator must regenerate the plan
(`mams ingest plan MEDIA_FILE_ID ...`) afterward, the same "explicit
commands perform changes" discipline every other blocking/review reason
already follows. `generate_plan` now only adds the review reason when
`assignment_method == 'MANUAL' and confirmed_for_ingest_at is None`, so
an `AUTO` plan's path is completely unaffected, and a fresh `MANUAL`
assignment still requires this explicit step (confirmation is never
implied by selection alone).

Confirmation lives on the assignment row, not the plan, because
`assign_identity()` always inserts a brand-new row for a changed
identity rather than updating one in place — a superseding manual
selection therefore starts unconfirmed by construction, with no
additional code needed to prevent confirmation from silently carrying
over to a different identity. `readiness.py`'s execution-readiness audit
needed no change: its existing `active_assignment_matches_plan` check
already fails a plan whose approved assignment has since been replaced,
regardless of confirmation state.

## Milestone 8.4: replacement ingest after a manually removed destination

An executed file can be removed from the NAS by an operator outside of
MAMS entirely (e.g. it had the wrong audio commentary track). The next
`mams inventory scan` of that category correctly reconciles the
canonical `media_files` row to `state='MISSING'` — never deleted, per
"never destroy data" — but a corrected replacement file placed in
Incoming and pointed at the exact same destination could never get a
plan past `BLOCKED`: `_check_collisions()` (`generate_plan`) and the
identically-patterned code in `audit_plan()` both checked canonical
inventory with no `state` filter (`SELECT 1 FROM media_files WHERE
absolute_path = ?`) and excluded only `SUPERSEDED` from the
competing-plan query, so a `MISSING` historical row and the original
`EXECUTED` plan both still reported a collision, permanently.

Fixed at the query level in both call sites (via two small shared
helpers, `_canonical_inventory_occupies_destination`/
`_competing_plan_id`, so the same fix can't drift out of sync between
`generate_plan` and `audit_plan` the way the original bug did): canonical
inventory now requires `state = 'ACTIVE'`; the competing-plan query now
excludes an explicit `_NON_COMPETING_PLAN_STATUSES = {EXECUTED,
SUPERSEDED}` — both terminal, never reused or reactivated by any code
path — rather than only `SUPERSEDED`. Deliberately an exclusion list, not
an inclusion list, so an unrecognized future status defaults to
blocking. Every other status, including `EXECUTION_FAILED`/
`RECOVERY_REQUIRED` (which may still have unresolved or partial
filesystem state — see `docs/EXECUTION-SAFETY.md`) and any genuinely
active or unresolved plan (`DRAFT`, `READY_FOR_REVIEW`,
`REVIEW_REQUIRED`, `BLOCKED`, `APPROVED`, `EXECUTING`) for a *different*
media file targeting the same destination, keeps blocking exactly as
before — this fix narrows a false positive, it does not weaken
collision protection. The real on-disk existence check
(`Path(plan.full_path).exists()`) is completely independent of database
state and was never affected. Purely a query-side change: no migration,
no new command, no write to the historical `EXECUTED` plan or `MISSING`
media file record.
