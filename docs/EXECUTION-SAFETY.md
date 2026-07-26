# Execution Safety (Milestone 8)

This document describes the safe, one-plan-at-a-time approved-plan
executor: its state machine, locking, transfer strategies, checksum
policy, no-overwrite guarantee, source-removal boundary, post-transfer
verification, inventory reconciliation, Plex behavior, failure states,
recovery scenarios, and why duplicate copies are always preferred over
data loss.

Every milestone through 7C is entirely read-only against the NAS
(`docs/ARCHITECTURE.md`, `docs/INGEST-WORKFLOW.md`) — no directory was
ever created, no file was ever renamed, moved, copied, or deleted.
Milestone 8 is the first code in this repository that mutates the
filesystem at all, and it does so for exactly one `APPROVED` plan at a
time, only on explicit operator confirmation.

## Primary safety principle

**No plan action may be trusted merely because it was approved
earlier, or even because `readiness.py`'s audit passed a moment ago.**
Every consequential precondition is revalidated immediately before it
matters:

1. `mams ingest execute PLAN_ID --confirm-plan PLAN_ID` re-runs
   `ingest_service.audit_plan()` fresh, before acquiring anything.
2. Only after the execution lock is acquired does the executor gather
   *live* filesystem evidence (a fresh `stat()` of the source, a fresh
   `stat()` of the destination root, fresh device IDs, fresh free-space)
   and run `execution.evaluate_preflight()` — 20 checks — again. This
   is deliberately redundant with the audit: time has passed, however
   little, since the audit ran, and the audit itself only ever reads
   the database plus one `Path.exists()` check, never a full stat of
   the source file.
3. Nothing is written to the filesystem until every preflight check
   passes.

## Execution state machine

```text
ingest_plans.status:

READY_FOR_REVIEW
    |
APPROVED
    |
EXECUTING
    |
    +--> EXECUTED               (success; executed_at set)
    +--> EXECUTION_FAILED       (failed before any mutation began)
    +--> RECOVERY_REQUIRED      (failed at or after a mutation began)
```

Rules enforced by the code, not just convention:

- Only `APPROVED` may enter `EXECUTING` —
  `execution_repository.transition_plan_to_executing()`'s `UPDATE ...
  WHERE id = ? AND status = 'APPROVED'` *is* the concurrency guard: a
  `rowcount` of 0 means the plan moved since it was last checked, and
  the caller aborts with nothing created and nothing touched. This is
  deliberately stronger than `ingest_repository.approve_plan()`'s
  existing SELECT-then-UPDATE pattern (fine for a single-process,
  database-only state flip; not fine here, where a missed race would
  begin a filesystem mutation).
- `EXECUTED` is written from exactly one call site: the last line of a
  successful `execute_plan()` run. A crash at any point before that
  line leaves the plan `RECOVERY_REQUIRED`, never silently `EXECUTED`
  and never silently back to `APPROVED`.
- `SUPERSEDED` and `BLOCKED` plans can never enter `EXECUTING`.
- There is no code path from `EXECUTING` back to `APPROVED`. Once a
  filesystem mutation might have begun, the only forward states are
  `EXECUTED`, `EXECUTION_FAILED`, or `RECOVERY_REQUIRED` — see "Retry
  policy" below for why there is deliberately no automatic way back.

`ingest_executions.status` mirrors this at the execution-attempt level:
`EXECUTING` → `SUCCEEDED` / `FAILED` / `RECOVERY_REQUIRED`. A completed
execution is retained forever as historical evidence — nothing deletes
an `ingest_executions` row, and a plan whose execution `SUCCEEDED` can
never be executed again (its plan status is `EXECUTED`, not
`APPROVED`).

## Confirmation requirement

`mams ingest execute PLAN_ID` alone only prints a preview (source,
destination, best-effort strategy guess, and the current readiness
status) and "NO ACTIONS WERE EXECUTED." Execution only proceeds with an
exact `--confirm-plan PLAN_ID` matching the same plan id — checked by
the CLI itself, before any service call. There is no `--force`, no
bulk mode, no wildcard plan selection, and no "execute latest"
shortcut.

## Locking

Two independent locks must both succeed before any mutation:

1. **Database lock** — `transition_plan_to_executing()` (above),
   inside the same transaction as `start_execution()` (which creates
   the `ingest_executions` row and seeds its `ingest_execution_steps`).
2. **Filesystem lock** — `execution_lock.acquire_lock()` creates a lock
   file at `{execution.state_directory}/ingest-plan-{plan_id}.lock`
   using `os.open(path, O_CREAT | O_EXCL | O_WRONLY)` — atomic,
   exclusive create; a second attempt fails loudly with
   `FileExistsError`, never silently overwrites. The lock file records
   `{token, pid, hostname, plan_id, acquired_at}`. `release_lock()`
   only ever removes a lock whose stored token matches the caller's
   own. `read_lock()` is strictly read-only, for `mams ingest
   recovery` — a stale lock is never auto-removed; deciding whether one
   is safe to clear is a human judgment call, not this codebase's.

`execution.state_directory` must be local scratch disk (same
convention as `project.database_path`), never the NAS share:
`O_EXCL`'s exclusive-create guarantee is unreliable on some network
filesystems. If it isn't configured, execution refuses to proceed at
all (no implicit fallback path is chosen for something this
safety-critical).

## Transfer strategies

Decided from live device IDs (`os.stat(...).st_dev`), never a path
prefix:

- **`SAME_FILESYSTEM_ATOMIC_RENAME`** — source and destination share a
  device. The "rename" is actually `os.link(source, destination)`
  followed by `os.unlink(source)`: `os.link()` is atomic and raises
  `FileExistsError` natively if the destination already exists, so
  there is no separate exists-check and no TOCTOU race the way a
  check-then-`os.rename()` would have. Inode equality is confirmed
  before the source is unlinked.
- **`CROSS_FILESYSTEM_COPY_VERIFY_REMOVE`** — copy, flush, `fsync`,
  checksum, atomic commit, verify, *then* remove the source. Never
  `shutil.copy`/`shutil.move` as a black box: this milestone requires
  the copy/checksum/commit boundaries to be distinct and individually
  verifiable, which `shutil.move`'s cross-filesystem fallback would
  hide. The temporary file (`.{filename}.mams-partial-{token}`) is
  always written *inside* the final destination directory, so the
  final commit (temp → final) is always a same-device operation and
  reuses the exact same no-clobber `os.link`+`os.unlink` primitive as
  the same-filesystem strategy.

## Checksum policy

Cross-filesystem transfers: SHA-256 by default
(`execution.checksum_algorithm`), computed twice independently —
once incrementally while streaming the copy (proves what was read from
the source), once by re-reading the fully-written temporary file from
disk (proves what is actually persisted, catching silent
write/buffering corruption the first hash alone can't). Both are
recorded on the `ingest_executions` row. A mismatch blocks the final
commit; the source is never touched.

Same-filesystem moves: no independent checksum is computed. The hard
link makes byte-identity structural (the same inode, not a copy), so a
second checksum would prove nothing a `stat()` doesn't already
guarantee. Size and a fresh MediaInfo probe are still verified after
the move.

## No-overwrite guarantee

Enforced structurally, not just by a pre-check:

- `finalize_same_device_move()` (the shared commit primitive for both
  strategies) uses `os.link()`, which raises `FileExistsError`
  natively and atomically if the destination exists — there is no
  window between "check" and "write" for a real NAS mount to race.
- Preflight independently confirms the destination path does not exist
  and the destination directory isn't occupied by a plain file,
  immediately before any mutation.
- No plan action is ever `overwrite=True`; the readiness audit already
  blocks that, and preflight never re-introduces it.

## Source-removal boundary

Source removal applies only to the cross-filesystem strategy, and only
after: the temporary file copied fully, its checksum matched, it was
committed to the final destination path, and the final destination
verified successfully. The source is stat-checked again immediately
before removal (still matches the plan's snapshot). If removal itself
fails, both copies are retained and the execution is marked
`RECOVERY_REQUIRED` — never retried automatically. `remove_source_file()`
removes exactly the one source file: never recursive, never the
Incoming parent directory.

Same-filesystem moves have no separate removal step: the hard-link
move already unlinks the source as part of the single atomic commit,
guarded by the same inode-equality check.

## Post-transfer verification

A fresh MediaInfo probe runs against the *destination* file — never
reused from the plan-time snapshot, which only describes the source
file before it ever moved. Twelve checks
(`execution.verify_destination()`): existence, regular-file-ness, path
match, non-zero size, expected size, successful probe, container
present, plausible duration, video track present, audio track present
(warning-only, same conservative convention as `verify_media()`),
plausible container/extension (warning-only), and — cross-filesystem
only — checksum match. A `FAIL` status here is treated as a step
failure, converting the execution to `RECOVERY_REQUIRED` rather than
`EXECUTED`.

## Canonical inventory reconciliation

`inventory_repository.relocate_media_file()` updates the moved file's
existing `media_files` row **by id**, never as a MISSING+ADDED pair —
every row that already references it (an ingest plan, an identity
assignment, a finding) keeps describing the same logical file across
the move. Deliberately not a category walk: it never calls
`scan_category`/`persist_category_scan`, so a single rip never re-walks
an entire NAS category root. Records exactly one immutable
`scan_changes` `'UPDATED'` event with `previous_absolute_path`
populated — a column this schema carried, unused, since
`0003_scan_changes.sql`. The resulting `scan_runs` row is tagged
`triggered_by='EXECUTION'`, distinguishing it from a real directory-walk
scan in scan history.

If the filesystem transfer and destination verification both succeed
but this refresh fails, the verified destination is never reversed —
the execution is marked `RECOVERY_REQUIRED` with recovery status
`INVENTORY_REFRESH_INCOMPLETE`, and the plan is not marked `EXECUTED`.

## Plex boundary

`execution.enable_plex_refresh` is `false` by default. No Plex HTTP
client exists anywhere in this codebase (`plex.enabled` is also `false`
by default), so the `PLEX_REFRESH` step always records `SKIPPED` —
either `disabled_by_config` or `plex_client_not_implemented` — and a
successful transfer completes regardless. Building a real Plex refresh
call is explicitly out of scope for this milestone.

## Failure states and recovery scenarios

Every failure point is one of the 13 named `FaultPoint`s (used for
deterministic failure-injection testing, `tests/test_execution_service.py`),
each mapping to an exact `(plan_status, execution_status,
recovery_status)` triple:

| Failure point | Plan status | Execution status | `recovery_status` |
|---|---|---|---|
| Before/during destination-directory creation | `EXECUTION_FAILED` | `FAILED` | `NONE` |
| Before copy start / during copy / after copy / before or after checksum compute / before final rename | `RECOVERY_REQUIRED` | `RECOVERY_REQUIRED` | `PARTIAL_DESTINATION_SOURCE_INTACT` |
| After final rename, before or during destination verification (cross-filesystem: source still present) | `RECOVERY_REQUIRED` | `RECOVERY_REQUIRED` | `DESTINATION_VERIFIED_SOURCE_NOT_REMOVED` |
| After final rename, before or during destination verification (same-filesystem: source already gone via the link+unlink move) | `RECOVERY_REQUIRED` | `RECOVERY_REQUIRED` | `DESTINATION_UNVERIFIED_SOURCE_REMOVED` |
| After destination verification passes, before source removal completes (cross-filesystem) | `RECOVERY_REQUIRED` | `RECOVERY_REQUIRED` | `DESTINATION_VERIFIED_SOURCE_NOT_REMOVED` |
| After source removal (or the equivalent same-filesystem point), before or during inventory refresh, or before finalize | `RECOVERY_REQUIRED` | `RECOVERY_REQUIRED` | `INVENTORY_REFRESH_INCOMPLETE` |
| Process killed with no exception ever caught | stays `EXECUTING` | stays `EXECUTING` | classified live by `mams ingest recovery` as `INTERRUPTED_STATE_UNKNOWN` |

**Why duplicate copies are preferred over data loss:** at every one of
these points, the executor's default is to leave *more* on disk than
strictly necessary — a partial temp file, a duplicate source-and-destination
pair, an empty directory — rather than guess and delete something that
might be the only good copy. `execution_filesystem.py`'s mutating
functions never delete what they've already written on a failure path;
a partial copy is retained as recovery evidence, never resumed and
never cleaned up automatically.

`mams ingest recovery EXECUTION_ID` is strictly read-only: it re-derives
live evidence (does the source exist? the destination? a
`.mams-partial-*` temp file? does the lock file exist and match the
recorded token?) and returns plain-English guidance. It never mutates
the database or the filesystem, and never repairs anything — recovery
requires an operator to look and decide.

## Retry policy

There is no `ingest retry` command in this milestone. A failed or
`RECOVERY_REQUIRED` execution requires the operator to inspect via
`mams ingest recovery`, resolve whatever the guidance describes, and
generate a brand-new plan for a fresh attempt — a stale plan is never
reused. This keeps the retry surface at zero: there is no code path
that can repeat a filesystem mutation automatically.

## Unsupported actions

This milestone does not perform, and has no code path for: overwriting
or replacing an existing library file, deleting a canonical library
file, bulk or queued or scheduled execution, automatic cleanup of
duplicate media, transcoding, remuxing, re-encoding, media repair,
automated quality replacement, arbitrary shell commands, cross-plan
batching, permanent source deletion before destination verification, or
an automatic retry loop of any kind.

## Operator response procedures

1. **`ingest execute` refuses with a usage error** (unapproved plan,
   stale/blocked/incomplete audit, lock already held, preflight
   failure): nothing was touched. Fix the underlying condition (approve
   the plan, regenerate a stale plan, wait for the other process) and
   re-run.
2. **Execution reports `EXECUTION_FAILED`**: nothing was mutated
   (`recovery_status: NONE`). Regenerate the plan and try again.
3. **Execution reports `RECOVERY_REQUIRED`**: run `mams ingest recovery
   EXECUTION_ID`, read its recommendation, and follow it manually.
   Never delete the source until the destination is independently
   confirmed correct (in VLC, or via `mams ingest execution EXECUTION_ID`'s
   recorded checksums).
