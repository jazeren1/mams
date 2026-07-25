# Changelog

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
