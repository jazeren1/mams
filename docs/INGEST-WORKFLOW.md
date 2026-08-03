# Ingest Workflow (Operator Guide)

This document walks the exact, current CLI workflow from a disposable file
in `Incoming` through execution and canonical inventory reconciliation.
Steps 1 through 12 are entirely read-only or database-only writes —
**no command through the execution-readiness audit executes a media
action.** Step 13, `mams ingest execute`, is the one command that
mutates the filesystem, and only for one `APPROVED`,
`READY_FOR_EXECUTOR` plan at a time, only with an exact
`--confirm-plan` match. See `docs/EXECUTION-SAFETY.md` for its full
safety model.

All commands take `--config path/to/config.yaml` (defaults to
`config/config.yaml`) and `--json` for machine-readable output.

## The workflow

### 1. Put a disposable or newly ripped media file in Incoming

Copy a real media file (not a blank/zero-byte placeholder) into one of the
directories configured under `ingest.incoming_roots` in `config.yaml`.

### 2. Run inventory scan with metadata

```
mams inventory scan --category incoming --metadata
```

Discovers the file (Incoming roots are scanned as additional categories
automatically — see `docs/DATABASE.md`, "Incoming as a category") and
probes it with `mediainfo` so verification later has real technical data
to check. Read-only against the file itself; writes only to the
database. `--category incoming` restricts this scan to just the
`incoming` category — see "Rolling ingest (Incoming-only)" below for why
this is the normal way to run this step. A periodic full audit (no
`--category`) still works exactly as before.

### 3. Review findings

```
mams findings evaluate
mams findings list --category incoming
```

Surfaces deterministic conditions (zero-byte file, missing tracks,
metadata probe errors, ...) before you invest time resolving an identity
for a broken file.

### 4. Evaluate local identification

```
mams identify evaluate
mams identify list --category incoming
```

Parses the file's path/filename into a **parsed candidate** —
`identification.py`'s deterministic, local, non-authoritative
interpretation. This never calls TMDb. A well-formed `Title (Year).ext`
file typically parses `HIGH` confidence; a year-less file directly under
`Incoming` (e.g. `Alien.mkv`) parses `UNKNOWN` — this is a known,
deliberate limitation, not a bug (see step 5).

```
mams identify show CANDIDATE_ID
```

shows one parsed candidate's full detail, always labeled a parsed
interpretation, never a confirmed identity.

### 5. Add a manual identification override when necessary

If step 4 produced `UNKNOWN` (or a wrong guess), record an explicit
override instead of relying on the parser to guess:

```
mams identify override MEDIA_FILE_ID --type MOVIE --title "Alien" --year 1979
mams identify override MEDIA_FILE_ID --type EPISODE --series "Carnivale" --season 1 --episode 2
```

An override is **database-only** and never renames the file. It is kept
in a separate table (`identification_overrides`) from the parser's own
output — the parsed candidate from step 4 is never modified or lost, and
remains visible via `mams identify show`. Creating a new override
replaces any existing active one for that file (the old one is retained,
marked cleared, not deleted).

```
mams identify show-effective MEDIA_FILE_ID
```

shows the **effective candidate** — whichever of "the active override"
or "the parsed candidate" resolution will actually use — tagged
`MANUAL_OVERRIDE` or `PARSED` so it's always clear which one is in
effect.

```
mams identify clear-override MEDIA_FILE_ID
```

reverts the effective candidate back to the parsed one.

### 6. Resolve against TMDb

First, confirm TMDb is reachable and your token is valid:

```
mams resolve provider-status
```

Never prints the token itself; distinguishes "not configured," "invalid
token," "rate limited," "network failure," and "success." Then:

```
mams resolve evaluate
```

Resolves the effective candidate (override if active, else parsed)
against the live TMDb API: searches, scores every result deterministically
(`scoring.py`), and persists one historical **resolution attempt** with
its ranked, scored matches. A confident, unambiguous top result
auto-resolves; an ambiguous or below-threshold result requires manual
review; nothing plausible is `NO_MATCH`. Costs a real TMDb request per
candidate (unlike step 4), so `--limit` defaults to 10 — use
`--media-file-id`/`--candidate-id`/`--category` to scope it, and
`--force` to re-evaluate a candidate that already has an attempt.

Repeated identical requests are served from the local `provider_cache`,
not a second network call — check with:

```
mams resolve cache-stats
mams resolve cache-clear --expired-only
```

(`cache-clear` only ever removes already-expired rows; it can never
affect a confirmed identity, an assignment, or a plan.)

### 7. Review or manually select a match

```
mams resolve list --status review_required
mams resolve show ATTEMPT_ID
```

`show` prints every ranked alternative with its component scores and
plain-language reasons. If the top pick is right:

```
mams resolve select ATTEMPT_ID MATCH_ID
```

confirms that specific match (not necessarily rank 1) and creates a
`MANUAL` assignment — distinct from an automatic `AUTO` assignment made
in step 6, both of which live in `media_identity_assignments`, distinct
in turn from the **external match** candidates recorded on the attempt
itself. If none of the ranked matches is right:

```
mams resolve reject ATTEMPT_ID
```

marks the attempt `NO_MATCH`; no assignment is created, and every ranked
alternative is preserved for later review.

### 8. Generate an ingest plan

```
mams ingest plan MEDIA_FILE_ID --destination-category movie
```

(`movie`/`kids_movie`/`tv`/`kids_tv` — must match the resolved identity's
media type.) Verifies basic media health from the metadata step 2 already
collected, computes the canonical destination path, and checks for
collisions — against canonical inventory, other active plans, and a
read-only filesystem existence check (never a write). Every plan prints
"No actions were executed."

### 9. Review verification, destination, collisions, and proposed actions

```
mams ingest show PLAN_ID
```

Shows the verification checks and their PASS/WARNING/FAIL status, the
proposed destination path, any blocking/review reasons, and every
proposed action explicitly labeled `(PROPOSED -- NOT EXECUTED)`. A plan's
status is:

- **`READY_FOR_REVIEW`** — no blocking or review issues found.
- **`REVIEW_REQUIRED`** — plausible but needs a human look (no destination
  category given, the identity was manually selected and not yet
  confirmed, or verification only warned).
- **`BLOCKED`** — a real problem (no resolved identity, failed
  verification, a collision) that must be fixed before this plan can be
  approved.

### 10. Confirm a manually selected identity for ingest (if required)

If step 9 shows `REVIEW_REQUIRED` with the reason "identity was manually
selected and has not yet been confirmed for ingest" (i.e. step 7 used
`resolve select`, not an auto-resolved match), `resolve select` alone is
**not** sufficient to approve this plan — it confirms *which* external
identity the file has, but not that an operator has separately reviewed
and accepted that specific manual choice *for this ingest*. Runtime
disagreement (or any other scoring gap large enough to miss the
auto-resolve bar) is exactly the situation this second, explicit gate
exists for: an operator, not the scorer, made the final call, so a
second command makes that call auditable on its own, distinct from the
plan approval that follows.

```
mams ingest confirm-identity PLAN_ID
```

Confirms the exact identity assignment `PLAN_ID` was generated against
— it fails with a clear error, rather than confirming the wrong thing,
if a later `resolve select`/`resolve evaluate` has since changed the
file's active assignment (regenerate the plan first in that case), or if
the assignment was already automatically resolved (`AUTO` needs no
confirmation). **Database-only**; no filesystem change. Repeating it is
safe — an already-confirmed assignment is reported unchanged. This
command never changes `PLAN_ID`'s own status: it only records that the
identity has been reviewed. To see that reflected as `READY_FOR_REVIEW`,
regenerate the plan:

```
mams ingest plan MEDIA_FILE_ID --destination-category CATEGORY
```

`mams ingest plan` reconciles the *existing* current plan for that media
file in place (same plan id, incremented `plan_version`) rather than
creating a new one — the plan created back in step 8 is the plan that
should be approved next, not a new one. `mams ingest confirm-identity`
prints these two exact follow-up commands (with real ids filled in)
after it succeeds.

An auto-resolved plan never shows this reason and never needs this step.

### 11. Approve the plan

```
mams ingest approve PLAN_ID
```

Only valid for a `READY_FOR_REVIEW` plan. Flips its status to
`APPROVED` — a **database-only** state change; prints "Plan approved. No
actions executed." Regenerating a plan later for the same file
(`mams ingest plan` again, e.g. after a source or identity change)
updates an unapproved plan in place, but marks an **`APPROVED`** plan
**`SUPERSEDED`** instead of silently mutating it, then inserts a fresh
current plan — an approval is a snapshot, never edited under you.

`--confirm-plan` (step 13) is a completely separate confirmation from
step 10's identity confirmation: step 10 confirms *which identity* an
operator accepted for review; `approve` confirms the *plan* (identity,
destination, verification, actions) is ready to execute;
`--confirm-plan PLAN_ID` on `ingest execute` confirms *this exact
already-`APPROVED` plan, right now, is the one to execute*. None of the
three substitutes for either of the others.

### 12. Run the execution-readiness audit

```
mams ingest audit PLAN_ID
```

A **read-only** audit of an `APPROVED` plan's current state — 25 checks
covering plan status, source existence/state/path/size/mtime (compared
against what the plan recorded when it was generated), whether the
identity behind the plan is still current, the verification snapshot,
destination availability, and the proposed action list's integrity.
Reports one of:

- **`READY_FOR_EXECUTOR`** — every check passed; nothing has changed
  since approval.
- **`STALE`** — the world moved since approval (source changed, the
  candidate or assignment changed — including a new/cleared override,
  verification no longer holds, or destination configuration drifted).
  Regenerate the plan (step 8) rather than trusting this one.
- **`BLOCKED`** — a hard stop (unapproved/superseded plan, a new blocking
  finding, the destination is now occupied, a competing plan targets the
  same destination, or an overwrite was somehow requested).
- **`INCOMPLETE`** — the plan's own action list is malformed (missing,
  misordered, an unsupported type, or a non-`PROPOSED_NOT_EXECUTED`
  state) — shouldn't happen given the schema's constraints, but checked
  anyway.

Every audit result includes `"execution_status": "NOT_EXECUTED"` and
prints "EXECUTION WAS NOT PERFORMED." This audit is distinct from step 9's
media verification: verification judges whether the *file* is
structurally plausible; the audit judges whether the *plan* is still an
accurate, actionable description of what should happen right now. It
never regenerates a plan or re-resolves an identity by itself — it only
reports.

### 13. Execute the plan

```
mams ingest execute PLAN_ID
```

Without `--confirm-plan`, this only prints a preview (source,
destination, best-effort transfer-strategy guess, current readiness
status) and "NO ACTIONS WERE EXECUTED." — it performs no mutation.
Execute for real:

```
mams ingest execute PLAN_ID --confirm-plan PLAN_ID
```

The exact match is required — a typo'd or omitted `--confirm-plan`
always falls back to the preview. Immediately before doing anything,
this re-runs the execution-readiness audit fresh (never trusting a
prior result), acquires a database and filesystem lock for this plan,
then re-gathers live filesystem evidence a second time and refuses to
proceed if any of 20 preflight checks fail. Only then does it move the
file — either an atomic hard-link move (same filesystem) or a
copy/checksum/verify/remove sequence (cross filesystem) — verify the
destination with a fresh MediaInfo probe, refresh canonical inventory
for just that one file, and mark the plan `EXECUTED`. See
`docs/EXECUTION-SAFETY.md` for the complete state machine and every
failure/recovery scenario.

```
mams ingest executions [--plan-id] [--status] [--recovery-status] [--limit]
mams ingest execution EXECUTION_ID
```

List execution history, or show one execution's full step-by-step
record. Both read-only.

```
mams ingest recovery EXECUTION_ID
```

If an execution reports `FAILED` or `RECOVERY_REQUIRED`, this
re-derives live evidence (does the source still exist? the
destination? a partial temp file? is the lock still held?) and prints
plain-English guidance. Strictly read-only — it never repairs or
deletes anything; recovery requires an operator to look and decide.
There is no `ingest retry` command: a failed execution requires
generating a fresh plan, never reusing the stale one.

## Rolling ingest (Incoming-only)

A full `mams inventory scan --metadata` walks and MediaInfo-probes every
configured category. On the production library (~3,500 files, 5.9 TB)
this takes roughly two hours — not viable to run after every one or two
discs when local `Incoming` space is limited. `mams inventory scan
--category incoming --metadata` (Milestone 8.2) restricts discovery and
reconciliation to just `incoming`, so this becomes the normal
per-disc-batch step instead of a full audit.

The rolling loop:

1. Rip one or two discs into `Incoming`.
2. Rename the ripped files using canonical, local, parse-friendly names
   (`Title (Year).ext`, or a series/season layout) — see step 1/4 above
   for how naming affects local identification confidence.
3. `mams inventory scan --category incoming --metadata`
4. `mams identify evaluate` then `mams identify list --category incoming`
5. `mams resolve evaluate`
6. `mams ingest plan MEDIA_FILE_ID --destination-category ...`, then
   `mams ingest show PLAN_ID` to inspect it. If step 5 auto-resolved, this
   is `READY_FOR_REVIEW`; if it required `resolve select` (step 7 above,
   e.g. a runtime disagreement), this is `REVIEW_REQUIRED` and needs the
   confirm-then-replan pair below first.
6a. Only if `resolve select` was used: `mams ingest confirm-identity
    PLAN_ID`, then re-run step 6's `mams ingest plan ...` to reach
    `READY_FOR_REVIEW`.
7. `mams ingest approve PLAN_ID`
8. `mams ingest audit PLAN_ID`
9. `mams ingest execute PLAN_ID --confirm-plan PLAN_ID` — one plan at a
   time
10. Verify the file landed correctly on the NAS
11. Repeat once `Incoming` disk space is freed

Notes on this loop:

- `mams inventory scan --category incoming` never walks or probes
  `movies`/`kids_movies`/`tv`/`kids_shows`/`fitness` (or any other
  configured category) — it succeeds even if the NAS is unmounted, since
  those roots are never inspected in the first place.
- A category omitted from a scoped scan is never implied to be missing,
  changed, or reconciled — its `media_files` rows, state, and scan
  history are left exactly as they were before the scoped scan ran (see
  `docs/DATABASE.md`, "Category-scoped scanning").
- Scoped scanning is a **discovery/reconciliation step only** — it never
  triggers identification, resolution, planning, or execution by itself,
  same as a full scan.
- A periodic full `mams inventory scan --metadata` (no `--category`)
  remains the right tool for a whole-library audit; the scoped form is
  for the per-disc-batch ingest loop specifically.
- Scanning, scoped or full, is read-only with respect to media files —
  it discovers and records, never moves, renames, or deletes anything.

## Quick reference: status vocabulary

| Concept | Values | Meaning |
|---|---|---|
| Identification source | `PARSED` / `MANUAL_OVERRIDE` | Which candidate resolution actually used (`mams identify show-effective`). |
| Resolution attempt | `RESOLVED` / `REVIEW_REQUIRED` / `NO_MATCH` / `FAILED` / `SKIPPED` | Outcome of one TMDb search+score against one candidate. |
| Assignment method | `AUTO` / `MANUAL` | How the current external identity was confirmed for a file. |
| Ingest confirmation | `confirmed_for_ingest_at` set / unset | Whether a `MANUAL` assignment has been explicitly confirmed for ingest (`mams ingest confirm-identity PLAN_ID`, step 10); always unset for `AUTO`, never required for it. |
| Plan status | `READY_FOR_REVIEW` / `REVIEW_REQUIRED` / `BLOCKED` / `APPROVED` / `SUPERSEDED` / `EXECUTING` / `EXECUTED` / `EXECUTION_FAILED` / `RECOVERY_REQUIRED` | Where a plan stands, through and including execution. |
| Readiness status | `READY_FOR_EXECUTOR` / `STALE` / `BLOCKED` / `INCOMPLETE` | Whether an approved plan is still safe to execute right now. |
| Execution status | `EXECUTING` / `SUCCEEDED` / `FAILED` / `RECOVERY_REQUIRED` | One execution attempt's outcome. |
| Recovery status | `NONE` / `PARTIAL_DESTINATION_SOURCE_INTACT` / `DESTINATION_VERIFIED_SOURCE_NOT_REMOVED` / `DESTINATION_UNVERIFIED_SOURCE_REMOVED` / `INVENTORY_REFRESH_INCOMPLETE` / `INTERRUPTED_STATE_UNKNOWN` / `OTHER_REQUIRES_MANUAL_INSPECTION` | What an operator needs to check after a non-`NONE` failure. |

## Safety guarantees, restated

- Steps 1 through 12 create no directory, rename, move, copy, or delete
  no file — anywhere, ever. Step 13 is the sole exception, and only for
  one plan at a time with an exact `--confirm-plan` match.
- No destination is ever overwritten — enforced structurally (an atomic
  exclusive-create primitive), not just by a pre-check.
- The source file is removed only after the destination is copied,
  checksummed, committed, and independently verified — never before.
- No command requests a Plex scan unless `execution.enable_plex_refresh`
  is explicitly enabled, and no Plex client exists in this codebase yet
  regardless — the step always records `SKIPPED`.
- Every proposed plan action is persisted `PROPOSED_NOT_EXECUTED` before
  approval; execution's own step history is a separate, real record of
  what was actually attempted.
- `mams resolve provider-status`/`resolve evaluate` never print, log, or
  persist your TMDb token anywhere — not in output, not in the cache, not
  in an error message.
