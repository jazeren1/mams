# Changelog

## 0.8.1

Fixed a Milestone 8 defect discovered during the first real NAS
acceptance run: `mams ingest execute PLAN_ID --confirm-plan PLAN_ID`'s
`CROSS_FILESYSTEM_COPY_VERIFY_REMOVE` strategy failed at `FINAL_RENAME`
with `[Errno 45] Operation not supported`, because its finalization
primitive relied on `os.link()`, which the production SMB-mounted NAS
destination does not support even between two paths reporting the same
device id. The source remained intact, the destination never existed
under its final name, and the execution correctly recorded
`RECOVERY_REQUIRED` -- a completeness gap, not a safety gap. Full
incident record in `docs/VALIDATION.md`'s "Milestone 8.1" entry.

`execution_filesystem.py` now has two distinct finalization primitives
instead of one shared one: `finalize_same_filesystem_source_move()`
(unchanged `os.link()`+`os.unlink()`, used only by the
`SAME_FILESYSTEM_ATOMIC_RENAME` strategy) and
`finalize_verified_temp_file()` (new, used only by the cross-filesystem
strategy's temp-to-final commit). The new primitive never calls
`os.link()`: it tries macOS's native `renamex_np(..., RENAME_EXCL)`
atomic no-clobber rename first, and falls back to a lock-protected,
explicitly-documented-limitation `lstat`-then-`os.rename()` path when
that's unsupported, as on SMB. Neither primitive ever calls
`os.replace()` or `shutil.move()`, and a real destination collision
always raises `DestinationCollisionError` rather than overwriting or
silently choosing an alternate filename. Full design in
`docs/EXECUTION-SAFETY.md`'s "Cross-filesystem finalization" section.

`mams ingest recovery EXECUTION_ID` now reports the exact discovered
temporary-file path(s) (`RecoveryGuidance.temp_file_paths`), not just a
boolean, in both JSON and text output.

No retry, force, overwrite, automatic cleanup, or alternate-filename
behavior was introduced. Execution #1 and Plan #1 (the historical
failure record from the real acceptance attempt) were not modified.

## 0.8.0

Added Milestone 8: safe, one-plan-at-a-time approved-plan execution --
the first code in this repository that mutates the filesystem. Full
safety model in `docs/EXECUTION-SAFETY.md`.

`mams ingest execute PLAN_ID --confirm-plan PLAN_ID` re-runs the
Milestone 7C readiness audit fresh (never trusting an earlier result),
acquires a database (`ingest_plans.status` `APPROVED` -> `EXECUTING`,
guarded by the UPDATE's own `WHERE status = 'APPROVED'` clause rather
than a prior SELECT) and filesystem lock
(`execution.state_directory`/ingest-plan-N.lock`, atomic
`O_CREAT|O_EXCL` create), then -- only after the lock is held --
re-gathers live filesystem evidence a second time
(`execution.evaluate_preflight()`, 20 checks) before any mutation
begins. Without an exact `--confirm-plan` match, the command only
prints a preview and performs no mutation.

Two transfer strategies, decided from live `os.stat().st_dev` values,
never a path heuristic: `SAME_FILESYSTEM_ATOMIC_RENAME` (an
`os.link()`+`os.unlink()` hard-link move -- atomic and inherently
no-clobber, since `os.link()` raises `FileExistsError` natively rather
than silently overwriting the way `os.rename()` would) and
`CROSS_FILESYSTEM_COPY_VERIFY_REMOVE` (streamed copy with an
incrementally-computed SHA-256, an independent re-read checksum of the
written temp file, an atomic commit using the same no-clobber
hard-link primitive, fresh destination verification, and only then
source removal). `execution_filesystem.py` never uses `shutil.copy`/
`shutil.move` as a black box, and never deletes a partial temp file on
failure -- a partial copy is retained as recovery evidence, never
resumed or cleaned up automatically.

A fresh MediaInfo probe runs against the destination file after
transfer (`execution.verify_destination()`, 12 checks) -- never the
plan-time snapshot, which only describes the source before it moved.
Canonical inventory is refreshed for just the one file that moved
(`inventory_repository.relocate_media_file()`, a new targeted,
single-file reconciliation that keeps the file's existing
`media_files` id rather than walking an entire NAS category root or
modeling the move as MISSING+ADDED), recording one `scan_changes`
`'UPDATED'` event with `previous_absolute_path` populated -- a column
this schema has carried, unused, since `0003_scan_changes.sql`. Plex
refresh stays disabled by default (`execution.enable_plex_refresh:
false`); no Plex client exists in this codebase, so the step always
records `SKIPPED` regardless.

A mid-execution failure is never re-raised -- it is a fully recorded,
legitimate outcome (`RECOVERY_REQUIRED` or `EXECUTION_FAILED`, with an
exact `recovery_status` describing what's safe to assume about the
source and destination), returned normally just like a `BLOCKED` plan
already was. `mams ingest recovery EXECUTION_ID` is strictly read-only
guidance for exactly that scenario; there is no `ingest retry` command
in this milestone -- a failed execution requires generating a fresh
plan, never reusing a stale one.

`mams ingest executions`/`ingest execution EXECUTION_ID` add read-only
browsing over the new execution history; `ingest audit` now also shows
a plan's most recent execution alongside its readiness result.

Two new migrations: `0013_ingest_plans_execution_statuses.sql` (widens
`ingest_plans.status` via a 12-step table rebuild -- SQLite has no
`ALTER TABLE ALTER COLUMN` -- rather than dropping the `CHECK`
constraint) and `0014_ingest_executions.sql` (`ingest_executions`,
`ingest_execution_steps`, and `scan_runs.triggered_by`).

New safety test (`tests/test_safety_controlled_execution.py`) proves
every rejection path in `ingest execute` itself -- an unapproved plan, a
plan gone stale after approval, a missing or mismatched
`--confirm-plan`, a lock already held, an unconfigured state directory
-- produces zero filesystem mutation, using the same
unconditional-raise monkeypatch technique as
`tests/test_safety_no_execution.py` (left untouched: the read-only
pre-execution workflow it covers is still true). A 13-point
fault-injection suite (`tests/test_execution_service.py`) proves the
exact recorded outcome at every named mutation boundary.

## 0.7.0

Added Milestone 7C: live TMDb acceptance, an operator-controlled
identification override workflow, and a read-only execution-readiness
audit. No filesystem executor exists; this milestone still executes
nothing.

`mams resolve provider-status` diagnoses TMDb configuration/authentication
with at most one minimal, cached request (`TMDbClient.verify_credentials`,
`GET /authentication`) -- distinguishes not-configured, invalid token,
rate limited, network failure, malformed response, and success, and never
prints, logs, or returns the token itself. `mams resolve cache-stats`/
`cache-clear --expired-only` give read-only visibility into
`provider_cache` and let expired rows be deleted on demand; neither
touches `external_identities`, resolution attempts/assignments, or ingest
plans.

`mams identify override MEDIA_FILE_ID --type MOVIE|EPISODE ...` records an
explicit, database-only manual identification for a file in a new
`identification_overrides` table, entirely separate from
`identification_candidates` -- the parser's own evidence is never
overwritten. Resolves the known Incoming limitation (a year-less movie
directly under `Incoming` parses `UNKNOWN`, per `_parse_unclassified()`'s
deliberately unweakened year requirement) with an explicit operator
action rather than a parser relaxation. `mams identify show-effective`
shows whichever candidate resolution will actually use -- the active
override if one exists, else the parsed candidate, tagged `PARSED` or
`MANUAL_OVERRIDE`; `mams identify clear-override` reverts to the parsed
candidate. `mams resolve evaluate` now resolves against this effective
candidate; `resolution_attempts` records which override (if any) was
active at resolve time, so a later override change can be told apart from
no change at all.

`mams ingest audit PLAN_ID` is a new, purely read-only execution-readiness
audit for an `APPROVED` dry-run plan -- 25 checks
(`READY_FOR_EXECUTOR`/`STALE`/`BLOCKED`/`INCOMPLETE`) covering plan
approval/supersession state, source existence/state/path/size/mtime
against a plan-time snapshot, current-candidate and current-assignment
drift (including an override added or cleared after the plan was
approved), external identity existence, the verification snapshot,
destination configuration/collision/competing-plan state, and the
proposed action list's completeness, ordering, supported types, and
`PROPOSED_NOT_EXECUTED` state. Every result carries
`"execution_status": "NOT_EXECUTED"`. `ingest_plans` gained
`source_size_bytes`/`source_mtime` snapshot columns (populated at
generation time), folded into the existing content-comparison/
supersede-on-regeneration logic so a source file that changes size or
mtime after approval is caught by both the audit and a subsequent
regeneration, with no new reconciliation logic required.

Two new migrations: `0011_identification_overrides.sql`
(`identification_overrides` plus `resolution_attempts.identification_override_id`)
and `0012_ingest_plan_source_snapshot.sql`
(`ingest_plans.source_size_bytes`/`source_mtime`).

Live TMDb acceptance tests (`tests/test_live_tmdb.py`) exercise the real
API against a small, representative set of movies/episodes when
explicitly enabled (`pytest -m live`, plus a real `TMDB_API_TOKEN`) --
excluded from the default suite regardless of whether a token happens to
be set. A new safety test
(`tests/test_safety_no_execution.py`) monkeypatches every
filesystem-mutating primitive (`Path.mkdir/rename/replace`,
`shutil.move/copy/copy2`, `os.rename/replace/remove/unlink`,
`subprocess.run/Popen`) across the full scan-to-audit workflow and
confirms none of them fire.

## 0.6.0

Added Milestone 7B: external identity resolution against TMDb and
dry-run ingest planning. Seven new tables (`provider_cache`,
`external_identities`, `resolution_attempts`, `resolution_matches`,
`media_identity_assignments`, `ingest_plans`, `ingest_plan_actions`).

`mams resolve evaluate` searches TMDb for each local identification
candidate (movie search by title+year, or TV series search followed by
an episode lookup), scores every result deterministically
(`scoring.py`: normalized title similarity, exact/close/missing year,
season/episode exactness, episode-title corroboration, runtime
corroboration -- provider popularity is never a scoring input, only a
documented final tie-break), and persists one historical
`resolution_attempts` row with its ranked `resolution_matches`. A top
score >= 0.90 with a >= 0.10 gap and no conflicts auto-resolves
(`HIGH` confidence); a plausible but ambiguous result requires review;
nothing plausible is `NO_MATCH`; a provider error is `FAILED`; a local
candidate that's `UNKNOWN` or `EXTRA` is `SKIPPED` without ever querying
TMDb. `mams resolve list/show/select/reject/stats` provide read-only
browsing plus non-destructive manual review -- selecting or rejecting a
match never touches media, preserves every ranked alternative, and
never creates duplicate assignment history for an unchanged outcome.
Confirmed identities are stored in `external_identities`, separate from
both the local candidate and the `media_identity_assignments` row that
links a file to one (`AUTO`/`MANUAL`, superseded rather than overwritten
when the confirmed identity changes).

`mams ingest plan MEDIA_FILE_ID` generates a structured dry-run plan for
one file under a configured `ingest.incoming_roots` root: verifies basic
media health from already-collected MediaInfo data (`verification.py`,
narrowly phrased as "structurally plausible," never "confirmed
uncorrupted"), computes a canonical destination path
(`destination.py`, sanitized against path traversal, reserved names, and
Unicode inconsistencies), and checks for collisions against canonical
inventory, other active plans, and the filesystem (a read-only
existence check only). A plan is `BLOCKED` on an unresolved identity,
failed verification, or a collision; `REVIEW_REQUIRED` when no
destination category was given, the identity was manually selected, or
verification only warned; otherwise `READY_FOR_REVIEW`. Every plan
prints "No actions were executed"; every proposed action
(`VALIDATE_SOURCE`/`VERIFY_MEDIA`/`CREATE_DIRECTORY`/`MOVE`/`RENAME`/
`REFRESH_INVENTORY`/`REQUEST_PLEX_REFRESH`) is tagged
`PROPOSED_NOT_EXECUTED`; no executor exists anywhere in this codebase.
`mams ingest approve PLAN_ID` flips a `READY_FOR_REVIEW` plan to
`APPROVED` as a database-only state change.

Incoming files are scanned into the existing canonical inventory as
additional categories (`AppConfig.incoming_categories`, merged into
`mams inventory scan` alongside the NAS categories) -- no changes to
`inventory.py` itself. This milestone never renames, moves, copies,
deletes, or replaces a media file, and never creates a directory or
calls Plex. Requires a TMDb API token (`TMDB_API_TOKEN` by default,
configurable via `tmdb.token_env_var`); every existing command continues
to work with no token configured.

## 0.5.0

Added a deterministic local parsing layer: `mams identify evaluate` parses
every `ACTIVE` media file's path/filename/category/layout into a structured
`IdentificationCandidate` (movie title+year, or TV series/season/episode)
and persists it to a new `identification_candidates` table, reconciling in
place (`UNIQUE(media_file_id)`) rather than accumulating history --
candidates are local interpretations of evidence, not confirmed media
identities. Movie parsing recognizes parenthesized/bracketed/plain-year
forms, common technical release tokens, edition labels, and part/disc
markers. TV parsing recognizes `S01E02`, `s01e02`, `S01E02E03`,
`S01E02-E03`, `1x02`, and `Season 01 Episode 02`, with season-folder
evidence filling in a series title or corroborating (never overriding) a
filename season number, season 0 classified `SPECIAL`, and recognized
bonus-content keywords classified `EXTRA`. Confidence is `HIGH` only when
strong evidence was actually parsed (never merely from a movie/TV
directory), `MEDIUM` when partial, `LOW` on conflicting filename/folder
evidence or a too-weak title, `UNKNOWN` with no usable evidence at all.
`mams identify list/stats/show` provide read-only browsing, filtering, and
detail lookup; every surface labels results as parsed candidates, not
confirmed identities.

A file going `MISSING` is never re-evaluated -- its last candidate is
retained untouched rather than removed, so a later `RESTORED` file keeps
its interpretation throughout. This milestone never calls TMDb, TVDB,
Plex, or any other external service, and remains entirely read-only
against the NAS: no file operations, no external identity resolution.

## 0.4.0

Added a read-only findings engine: `mams findings evaluate` runs nine
deterministic rules (`missing_file`, `metadata_error`,
`metadata_not_probed`, `unknown_layout`, `zero_byte_file`,
`suspiciously_small_media`, `no_video_track`, `no_audio_track`,
`unexpected_extension`) against the canonical inventory and persists the
results to a new `findings` table, reconciling in place rather than
reinserting — repeated evaluation against unchanged inventory produces no
duplicate findings, preserves each finding's id and original detection
time, and only touches `updated_at` when a finding's content actually
changed. A condition that disappears resolves its finding; one that
reappears reactivates it, preserving `first_detected_at`. `mams findings
list/stats/show` provide read-only browsing, filtering, and detail lookup.

This milestone is entirely read-only against the NAS and Plex: it only
reads already-collected inventory data and writes to the new `findings`
table. No file operations, no identification, no automation.

## 0.3.1

Fixed a severe (~100x) `mediainfo` invocation slowdown on macOS: capturing
the child process's stdout through a pipe (`subprocess.run(...,
capture_output=True)`) measured ~54s for output `mediainfo` itself produces
in ~0.4s at the terminal. `MediaInfoProvider.probe` now directs stdout to
an anonymous `tempfile.TemporaryFile`, reads it back after the process
exits, and decodes stdout/stderr manually (with `errors="replace"` as a
decoding fallback) instead of relying on `subprocess`'s text-mode pipe
capture. Timeout and graceful per-file error handling are unchanged. No
behavior change for callers — `MediaInfoOutcome` and the `MetadataProvider`
interface are untouched.

## 0.3.0

Added a `MediaInfo` provider abstraction (`src/mams/mediainfo.py`) that
extracts technical metadata — container, duration, overall bitrate, video
track detail (codec, resolution, aspect ratio, frame rate, HDR format, bit
depth, scan type), audio track detail (codec, language, channels, bitrate,
default flag), and subtitle track detail (language, default flag, forced
flag) — from the `mediainfo` CLI tool's JSON output. The inventory scanner
depends on it only through a `MetadataProvider` protocol, so it stays
decoupled from `mediainfo` execution and swappable for a future provider.

`mams inventory scan --metadata` optionally enriches each discovered file
with this metadata; without the flag, scanning behaves exactly as before.
Added `mams mediainfo <path>` as a read-only developer diagnostic command
that runs the same parser against a single file. A missing `mediainfo`
executable, a corrupt file, or malformed JSON output is recorded as an
error on the affected file and never stops the scan. This milestone
remains 100% read-only: no files are modified, no checksums are computed,
no database or Plex writes occur.

## 0.2.1

Added a `movie_collection_folder` layout classification to the inventory
scanner for movies stored under a collection/franchise grouping folder
(e.g. `Movies/Star Wars/A New Hope/A New Hope_001.mp4`). Fixes the 52
movie files a real NAS scan reported as `unknown`. `movie_flat` and
`movie_folder` detection are unchanged; paths nested deeper than the
collection pattern still report `unknown`.

## 0.2.0

Added a read-only library inventory scanner (`mams inventory scan`). Recursively
discovers media files under the NAS category paths in `config/config.yaml`,
detects flat/folder-per-movie and series/season TV layouts, and writes a
JSON report and a human-readable summary under `reports/`. The scanner never
renames, moves, deletes, checksums, or otherwise modifies scanned media.

## 0.1.0

Initial project foundation: instructions, current state, playbook, architecture, naming, NAS policy, verification, Plex strategy, schema, roadmap, decision log, Python scaffolding, and inventory templates.
