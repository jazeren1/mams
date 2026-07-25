# Ingest Workflow (Operator Guide)

This document walks the exact, current CLI workflow from a disposable file
in `Incoming` through an execution-readiness audit. **No command described
here executes a media action.** No filesystem executor exists anywhere in
MAMS yet (that is Milestone 8's job) — every command below is either
read-only or a database-only write.

All commands take `--config path/to/config.yaml` (defaults to
`config/config.yaml`) and `--json` for machine-readable output.

## The workflow

### 1. Put a disposable or newly ripped media file in Incoming

Copy a real media file (not a blank/zero-byte placeholder) into one of the
directories configured under `ingest.incoming_roots` in `config.yaml`.

### 2. Run inventory scan with metadata

```
mams inventory scan --metadata
```

Discovers the file (Incoming roots are scanned as additional categories
automatically — see `docs/DATABASE.md`, "Incoming as a category") and
probes it with `mediainfo` so verification later has real technical data
to check. Read-only against the file itself; writes only to the
database.

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

### 10. Approve the plan

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

### 11. Run the execution-readiness audit

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

### 12. Stop — execution is not yet implemented

There is no command past this point. No filesystem executor exists in
MAMS; `readiness.py`'s audit is the contract a future Milestone 8
executor is expected to require before acting on a plan.

## Quick reference: status vocabulary

| Concept | Values | Meaning |
|---|---|---|
| Identification source | `PARSED` / `MANUAL_OVERRIDE` | Which candidate resolution actually used (`mams identify show-effective`). |
| Resolution attempt | `RESOLVED` / `REVIEW_REQUIRED` / `NO_MATCH` / `FAILED` / `SKIPPED` | Outcome of one TMDb search+score against one candidate. |
| Assignment method | `AUTO` / `MANUAL` | How the current external identity was confirmed for a file. |
| Plan status | `READY_FOR_REVIEW` / `REVIEW_REQUIRED` / `BLOCKED` / `APPROVED` / `SUPERSEDED` | Where a dry-run plan stands. |
| Readiness status | `READY_FOR_EXECUTOR` / `STALE` / `BLOCKED` / `INCOMPLETE` | Whether an approved plan is still safe to hand to a future executor. |

## Safety guarantees, restated

- No command in this workflow creates a directory, renames, moves,
  copies, or deletes a file — anywhere, ever, in this version of MAMS.
- No command requests a Plex scan.
- Every proposed plan action is persisted `PROPOSED_NOT_EXECUTED`; no
  code path can turn one into a real filesystem operation.
- `mams resolve provider-status`/`resolve evaluate` never print, log, or
  persist your TMDb token anywhere — not in output, not in the cache, not
  in an error message.
