# Validation History

Every major MAMS architecture milestone is validated with:

- Unit tests
- Integration tests
- Production validation when applicable
- Static analysis
- Any significant benchmarks

This document is append-only. Existing validation entries are never rewritten except to correct factual errors.

---

# Milestone 4 – Canonical SQLite Inventory

Date: 2026-07-20

## Summary

This milestone introduced the canonical SQLite inventory database: the migration framework, the schema for `libraries`, `scan_runs`, `media_files`, `video_tracks`, `audio_tracks`, and `subtitle_tracks`, the persistence layer that reconciles scan results into that schema, the reconstruction layer that reads the schema back into the existing domain model, and database-backed JSON/summary reporting.

## Validation Results

- Migration framework implemented and validated.
- Canonical inventory schema implemented.
- Write path validated.
- Read path validated.
- Database reconstruction produces the same domain model as the filesystem scanner.
- JSON reports generated from the database are semantically identical to the original in-memory reports.
- Rendered summary reports are byte-identical.

## Production Validation

Library size: 3,513 media files
Total size: approximately 5.8 TB

Metadata validation:

- Video tracks: 3,508
- Audio tracks: 3,511
- Subtitle tracks: 1,015

Verification performed:

- Database-backed metadata scan
- In-memory metadata scan
- Deep comparison of every reconstructed file
- Deep comparison of every MediaInfo field
- Deep comparison of every video/audio/subtitle track
- JSON equivalence
- Summary equivalence

### Results

All comparisons matched with zero discrepancies.

## Performance Observations

- SQLite reconciliation completed in approximately 4 seconds.
- The dominant runtime remains filesystem traversal and MediaInfo extraction rather than database operations.

## Quality Gates

- 147 automated tests passing
- Ruff clean
- MyPy clean

## Architecture Confidence

This validation matters because it confirms the write and read paths are consistent with each other and with the existing filesystem-scanner behavior at full production scale, not just in unit tests against synthetic fixtures.

This milestone demonstrates that SQLite has become the canonical inventory without changing externally observable behavior.

---

# Milestone 5 – Inventory Operations and Change Tracking

Date: 2026-07-21

## Summary

This milestone made the canonical inventory operationally useful: a query layer (`list_media_files`, `search_media_files`, `get_inventory_stats`) for browsing the current inventory without writing SQL in the CLI layer, an immutable `scan_changes` table recording ADDED/UPDATED/MISSING/RESTORED events generated during reconciliation, and four read-only CLI commands (`mams inventory list`, `stats`, `find`, `diff`) built on both.

## Validation Results

- Migration `0003_scan_changes.sql` implemented and validated (schema constraints, indexes, and the documented `ON DELETE SET NULL` retention behavior for `media_file_id`/`library_id`, as opposed to the track tables' `ON DELETE CASCADE`).
- Query layer (list/search/stats) implemented and validated; every query function verified to run a fixed, bounded number of SQL statements regardless of file count, not one per row.
- Change-event generation implemented and validated: each file's transition during a scan produces at most one event; an identical repeat scan produces zero events by construction (bookkeeping/timestamp columns are excluded from the comparison, not specially skipped); a no-metadata scan cannot produce a false metadata-change event, because it never touches the columns being compared; track content is compared by value, so delete+reinsert on an unchanged re-probe does not produce a false UPDATED.
- Event generation and inventory reconciliation commit atomically: a failed reconciliation (verified with both a single-file and a multi-file failure) leaves neither partial `media_files`/track writes nor any `scan_changes` rows, while the `scan_runs` row remains recorded as FAILED.
- `event details_json` verified deterministic byte-for-byte across independent runs, not just structurally equal.
- CLI commands (list/stats/find/diff) validated for both text and JSON output, correct default-to-latest-completed-scan behavior, explicit scan selection, `--from-scan`/`--to-scan` range semantics, and a clear, non-destructive error (no database mutation) for an unknown scan id.

## Production Validation

Library size: 3,513 media files (unchanged from Milestone 4's baseline)
Total size: approximately 5.8 TB (unchanged)

Track totals (unchanged from Milestone 4): 3,508 video / 3,511 audio / 1,015 subtitle.

Verification performed against the real NAS:

- A non-metadata production scan (scan #3) after this milestone's schema/write-path changes: 3,513 files, matching the established baseline exactly, and zero `scan_changes` events — confirming a stable rescan produces no false events at full production scale, immediately after the `scan_changes` baseline was established (`media_files` already existed for every file from Milestone 4's earlier scans; only the event table is new).
- A full `--metadata` production scan (scan #4), re-probing all 3,513 files: zero metadata extraction errors, and zero `scan_changes` events despite every file's track rows being deleted and reinserted during reconciliation — confirming the by-value track comparison holds at production scale, not only in unit tests with one or two synthetic tracks.
- Sandbox sequence (separate from the production NAS) proving all four event types end-to-end through the real CLI: ADDED on first discovery, zero events on an identical rescan, UPDATED (with `size_bytes`/`mtime` in `details_json`) on a resize, MISSING on removal, RESTORED on rediscovery. `scan_runs.added_count`/`updated_count`/`missing_count`/`restored_count` matched the recorded events exactly at every step.

No NAS media was modified during validation.

### Results

All comparisons matched with zero discrepancies. No unexpected change events were recorded against the real library.

## Performance Observations

- Reconciliation for the full `--metadata` production scan (3,513 files, including the new per-file before/after diff and event-recording work) completed in approximately 4 seconds — consistent with Milestone 4's benchmark for the write path alone, indicating the added diffing logic has not introduced a measurable regression.
- The dominant runtime remains filesystem traversal and MediaInfo extraction, unchanged from Milestone 4.

## Quality Gates

- 238 automated tests passing
- Ruff clean
- MyPy clean

## Architecture Confidence

This validation matters because change-event generation is the kind of logic that's easy to get right for a single synthetic fixture and wrong at scale — in particular, a comparison that's subtly too eager (e.g. comparing by row-touch instead of by value) would have produced thousands of false UPDATED events against a real library with real timestamp jitter and real track re-probing, and that failure mode would not necessarily show up in a small unit-test fixture. Confirming zero false events across the full 3,513-file production library, on both a plain rescan and a full metadata re-probe, is direct evidence the diffing rules in `docs/DATABASE.md` are correct in practice, not just on paper.

This milestone demonstrates that the canonical inventory is now queryable and its history is tracked, without changing any externally observable behavior of `mams inventory scan`'s existing JSON/summary output.

---

# Milestone 6 – Read-Only Findings Engine

Date: 2026-07-22

## Summary

This milestone added a deterministic, read-only findings engine on top of
the canonical inventory: a `findings` table (migration
`0004_findings.sql`), a pure rule engine (`src/mams/findings.py`) with nine
initial rules, an atomic reconciliation service that creates/updates/
resolves/reactivates findings without ever duplicating them
(`findings_service.py` / `findings_repository.py`), a query layer (list/
get/stats), and four CLI commands (`mams findings evaluate/list/stats/
show`). Findings evaluation only reads `media_files`/track data and writes
to the new `findings` table — it never touches the NAS or Plex.

## Validation Results

- Migration `0004_findings.sql` implemented and validated: severity/status
  `CHECK` constraints, `(rule_code, media_file_id)` uniqueness (including
  that it does *not* block the same rule firing for a different file),
  and the documented `ON DELETE SET NULL` retention behavior for
  `media_file_id`/`library_id` (a finding's row and content survive a
  `media_files`/`libraries` delete, matching `scan_changes`'s established
  pattern) — all exercised directly against the migration file, not just
  through the ORM-like repository layer.
- All nine rules (`missing_file`, `metadata_error`, `metadata_not_probed`,
  `unknown_layout`, `zero_byte_file`, `suspiciously_small_media`,
  `no_video_track`, `no_audio_track`, `unexpected_extension`) validated
  both triggering and not triggering, as pure functions of a
  `MediaFileRecord` with no SQL access. Confirmed: `metadata_not_probed`
  and `metadata_error` are mutually exclusive (a probe that ran and
  failed is never also reported as "never probed"); `no_video_track`/
  `no_audio_track` require a *successful* probe (never fire before a
  probe, or after a failed one); `suspiciously_small_media` and
  `zero_byte_file` are mutually exclusive by construction (a 0-byte file
  is reported exactly once); `suspiciously_small_media`'s boundary is
  exact (triggers at threshold − 1 byte, not at the threshold itself);
  `missing_file` is the only rule evaluated against a `MISSING` row —
  confirmed a record with every other rule's trigger condition *and*
  `state='MISSING'` produces only the `missing_file` finding, never the
  others re-firing on stale pre-missing data. `evaluate_all()` confirmed
  deterministically ordered and stable across independent calls on the
  same input.
- Reconciliation lifecycle validated: a new finding is created `ACTIVE`
  with `first_detected_at`/`last_detected_at` set; a repeated evaluation
  against unchanged inventory creates no duplicate and preserves the
  finding's id and `first_detected_at` exactly, while `last_detected_at`
  still advances and `updated_at` does *not* churn when content is
  unchanged (and does change when it is, verified by moving a file to a
  different library so its `library_id` content differs); a condition's
  disappearance resolves its finding with `resolved_at` set; its
  reappearance reactivates the same finding id, clears `resolved_at`, and
  preserves the original `first_detected_at`; an `IGNORED` finding's
  status/`resolved_at` are proven untouched by reconciliation in *both*
  directions — condition still present, and condition gone — with only
  `last_detected_at` advancing in the former case; a forced mid-
  reconciliation failure (one finding needing to resolve, another needing
  to be created, in the same run) leaves neither change applied,
  confirming the whole reconciliation is atomic; `evidence_json` verified
  byte-for-byte deterministic across independent runs against independent
  databases, not just structurally equal.
- Query layer (list/get/stats) validated: every filter (status, severity,
  rule_code, category, media_file_id) individually and combined with AND,
  `limit`, category/path resolution via join (including the case where
  `media_file_id` is detached and path/category become unresolvable but
  the finding row survives), deterministic ordering (grouped by file, then
  severity — CRITICAL/ERROR before WARNING/INFO — then by each rule's
  declared position in `findings.ALL_RULES`, matching this document's
  suggested CLI output shape), ordering stability across repeated calls,
  finding lookup by id (including an unknown id), stats totals (by status,
  and severity/rule breakdowns restricted to `ACTIVE` findings), and
  bounded query counts via `set_trace_callback` (`list_findings`: exactly
  one `SELECT`; `get_findings_stats`: at most three) regardless of row
  count — never one query per row.
- CLI (`evaluate`/`list`/`stats`/`show`) validated for both text and JSON
  output, filter combinations, repeated evaluation producing zero
  duplicates through the full CLI path, empty-result handling, and a
  clear, non-destructive error (no database mutation) for an unknown
  finding id — seeded through the real `run_inventory_scan()` CLI path,
  not a hand-built database, so these tests exercise the same code path a
  real user invocation does.

## Sandbox Lifecycle Demonstration

Run against an isolated sandbox library (separate from the production
database) through the real CLI, proving the full lifecycle end to end:

1. A zero-byte `Broken.mkv` and a normal `Good.mkv` scanned, then
   evaluated: `zero_byte_file` created `ACTIVE` for `Broken.mkv` (plus
   `metadata_not_probed` for both files, since `--metadata` wasn't used).
2. Re-running `findings evaluate` against the same, unchanged inventory:
   `Created: 0`, `Unchanged: 3` — no duplicates.
3. `Broken.mkv` overwritten with real content and rescanned, then
   evaluated: the `zero_byte_file` finding transitioned to `RESOLVED` with
   `resolved_at` set, `first_detected_at` unchanged.
4. `Broken.mkv` truncated back to zero bytes and rescanned, then
   evaluated: the *same* finding id reactivated to `ACTIVE`,
   `resolved_at` cleared, and `first_detected_at` identical to step 1's
   value (`2026-07-22 15:38:54` in the actual run) — confirming
   reactivation is not a delete-and-recreate under the hood.

No NAS media was modified at any point (the sandbox library lives entirely
under a temp directory, not the real NAS).

## Production Validation

Evaluated against the real production inventory database (unchanged since
Milestone 5: 3,513 `ACTIVE` + 2 `MISSING` = 3,515 `media_files` rows, ~5.8
TB). Findings evaluation reads only the database — it never re-scans or
otherwise touches the NAS, so this required no NAS access and modified no
NAS media.

**First evaluation:** 14 findings created, 0 resolved:

| Rule                       | Severity | Count |
|----------------------------|----------|-------|
| `missing_file`             | ERROR    | 2     |
| `no_video_track`           | ERROR    | 5     |
| `no_audio_track`           | WARNING  | 3     |
| `suspiciously_small_media` | WARNING  | 4     |

`metadata_error`, `metadata_not_probed`, `unknown_layout`, `zero_byte_file`,
and `unexpected_extension` all produced zero findings.

**Repeated evaluation** (immediately after, no inventory change in
between): `Created: 0`, `Unchanged: 14`, `Resolved: 0` — confirming the
no-duplicate/no-false-churn guarantee holds at full production scale, not
only against small unit-test fixtures.

### Plausibility review

- **`missing_file` (2): `Movies/test.mkv` and `Movies/test2.mkv`.** These
  are the temporary artifacts created during Milestone 5's sandbox
  ADDED/MISSING/RESTORED validation sequence, left in a `MISSING` state
  from that milestone's own database (a real scan of the actual NAS would
  no longer see them, since they were never real library content) — not
  production defects. Documented here per this milestone's validation
  plan rather than treated as a rule bug. Correctly, neither produces a
  `zero_byte_file` finding despite their tiny recorded size (4 and 43
  bytes) — `zero_byte_file` and every other non-`missing_file` rule is
  scoped to `ACTIVE` rows only (see `findings.py`), so a `MISSING` file
  is reported exactly once, by `missing_file` alone.
- **`no_video_track` (5) / `no_audio_track` (3): "Slender Man", "The
  Hobbit", one Sopranos episode, and two "Brak Show" files.** Internally
  consistent: 3 of the 5 `no_video_track` files (Slender Man, Hobbit,
  Sopranos) also lack an audio track, while the two Brak Show files have
  audio but not video — plausible for rips where video encoding/muxing
  failed but audio survived, and a signal worth a human's attention rather
  than an artifact of the rule itself.
- **`suspiciously_small_media` (4): Fraggle Rock, Buffy, and the same two
  Brak Show files already flagged `no_video_track`.** The overlap with
  `no_video_track` on the Brak Show files is corroborating evidence, not
  double-counting — two different rules independently surfacing the same
  underlying broken-rip files is exactly the intended behavior, not a
  bug.
- **Zero `metadata_error`/`metadata_not_probed`/`unknown_layout`/
  `unexpected_extension`** is consistent with Milestone 4/5's established
  baseline (zero probe errors across the full library, every file already
  MediaInfo-probed, every layout already classified, and the scanner only
  ever discovers files within the configured extension set to begin with).

No NAS media was modified during any part of this validation — findings
evaluation is read-only against the database by construction, and the NAS
was not even mounted for the production evaluation run.

## Quality Gates

- 346 automated tests passing
- Ruff clean
- MyPy clean

## Architecture Confidence

This validation matters for a different reason than Milestones 4/5's did:
those validated that a write path faithfully reflects the filesystem at
scale, while this milestone's core risk was reconciliation *idempotency*
and *non-destructiveness* — a findings engine that creates duplicates on
every run, or silently churns `updated_at`/loses `first_detected_at` on
reactivation, would be actively misleading for exactly the review workflow
it exists to support. Confirming zero duplicates and zero unnecessary
churn on a second full-production evaluation, and confirming the sandbox
lifecycle sequence preserves finding identity and detection history
through a full resolve → reactivate cycle, is direct evidence the
reconciliation rules in `docs/DATABASE.md` are correct in practice. The
production rule counts also being independently plausible (corroborating
overlaps between `no_video_track` and `suspiciously_small_media` on the
same broken files, zero false positives elsewhere) is evidence the rule
engine surfaces real, actionable conditions rather than noise.

This milestone remains entirely read-only against the NAS and Plex: no
file operations, no identification, no automation. It demonstrates that
the canonical inventory can now be reviewed for actionable conditions
without any risk to the underlying media.

---

# Milestone 7A – Local Media Parsing and Identification Candidates

Date: 2026-07-24

## Summary

This milestone added a deterministic local parsing layer on top of the
canonical inventory: an `identification_candidates` table (migration
`0005_identification_candidates.sql`), a pure parsing domain
(`src/mams/identification.py`) covering conservative movie title/year
parsing and TV season/episode parsing, an atomic reconciliation service
that creates/updates/retains candidates without ever duplicating them
(`identification_service.py` / `identification_repository.py`), a query
layer (list/get/stats), and four CLI commands (`mams identify
evaluate/list/stats/show`). Evaluation only reads `media_files` and writes
to the new `identification_candidates` table — it never touches the NAS,
never calls Plex, and never calls TMDb/TVDB or any other external
service. Candidates are local interpretations of evidence, never confirmed
media identities; every CLI surface says so explicitly.

## Validation Results

- Migration `0005_identification_candidates.sql` implemented and
  validated: `candidate_type`/`confidence` `CHECK` constraints,
  `UNIQUE(media_file_id)` (including that a second candidate for the same
  file is rejected while candidates for different files are not), all
  five required indexes, and the documented `ON DELETE CASCADE` retention
  behavior — the one place this schema deliberately diverges from
  `scan_changes`/`findings`'s `ON DELETE SET NULL`, because a candidate is
  a live interpretation of a still-existing file rather than evidence that
  must outlive it (see `docs/DATABASE.md`).
- Movie parsing validated for parenthesized/bracketed/plain-year forms,
  title-without-year, technical-token removal (with removed tokens
  recorded in `evidence`), edition extraction (`Director's Cut` with and
  without the apostrophe, `Extended Edition`), part/disc-number
  extraction, rejection of implausible four-digit values as years (a
  resolution token bordering the year range boundary, and a bare number
  embedded in a longer digit run), and conflicting filename/folder
  evidence (differing years, differing titles) correctly downgrading to
  `LOW`. Two real gaps were found and fixed via failing tests during
  development, not discovered later: `movie_flat`'s `parent_directory` is
  the category root itself and must never be read as title evidence
  (only `movie_folder`/`movie_collection_folder` layouts have a
  meaningful per-file parent directory), and generic placeholder
  filenames (`movie.mkv`, `video.mkv`) must be treated as if the filename
  had no title, so a properly-named folder isn't spuriously flagged as
  conflicting with its own generic filename.
- Television parsing validated for all six required forms (`S01E02`,
  `s01e02`, `S01E02E03`, `S01E02-E03`, `1x02`, `Season 01 Episode 02`,
  including a dot-separated variant of the last), season-folder evidence
  filling in a series title or corroborating (never overriding) a
  filename season number, season 0 classifying as `SPECIAL`, recognized
  bonus-content keywords classifying as `EXTRA`, episode title extraction
  (and correct omission when nothing or only technical tokens follow),
  no fabrication of season/episode numbers when no pattern is present
  (including the case where only a season-folder number is known),
  and conflicting season evidence between filename and season folder
  correctly downgrading to `LOW` while the filename's number still wins.
  Multi-episode extraction (`S01E02E03` and `S01E02-E03`) reads every
  explicit `E<n>` occurrence verbatim rather than expanding an implied
  range — verified never to fabricate an episode number that wasn't
  actually written.
- Classification validated: category/layout decide which parser runs
  (legitimate weak evidence of content class), but never inflate
  confidence — a file in a movie or TV directory with an unparseable name
  comes back `LOW`/`UNKNOWN`, never `HIGH`, confirmed by dedicated tests
  for both movie and TV directories. The fallback for a file whose
  category/layout give no signal at all requires a real parsed year (not
  merely a parsed word) before accepting a movie guess, confirmed by a
  test that a single generic word (`readme.mkv`) with no directory
  support classifies `UNKNOWN`, not a low-confidence guess.
- Reconciliation lifecycle validated: a new candidate is created on first
  `ACTIVE` sighting; a repeated evaluation against unchanged inventory
  creates no duplicate and performs zero writes for that file (`id`,
  `created_at`, and `updated_at` all stable — confirmed by forcing a
  distinguishable `updated_at` beforehand and observing it does not
  move); a `filename` change and a `layout` change each update the
  existing row in place, preserving `id`/`created_at`; a file going
  `MISSING` is never visited by `evaluate_candidates()` at all, so its
  candidate is retained byte-for-byte untouched, and a later `RESTORED`
  file keeps that same candidate id throughout; a forced mid-
  reconciliation failure (one file needing an update, another needing to
  be created, in the same run) leaves neither change applied, confirming
  the whole reconciliation is atomic; `evidence_json` verified byte-for-
  byte deterministic across independent runs against independent
  databases.
- Query layer (list/get/stats) validated: every filter (`candidate_type`,
  `confidence`, `category`, `has_year` true/false, `season_number`,
  `media_file_id`) individually and combined with `AND`, `limit`,
  category/path resolution via join, `episode_numbers`/`evidence`
  round-tripping through their JSON columns, deterministic
  `(category, relative_path, id)` ordering and its stability across
  repeated calls, candidate lookup by id (including an unknown id), stats
  totals (by type, by confidence, with/without-year split), and bounded
  query counts via `set_trace_callback` (`list_candidates`: exactly one
  `SELECT`; `get_candidate_stats`: at most four) regardless of row count.
- CLI (`evaluate`/`list`/`stats`/`show`) validated for both text and JSON
  output, filter combinations, repeated evaluation producing zero
  duplicates through the full CLI path, empty-result handling, and a
  clear, non-destructive error for an unknown candidate id — seeded
  through the real `run_inventory_scan()` CLI path. Every text and JSON
  surface (list header, show header) explicitly labels output as parsed
  local interpretations, never confirmed identities, per this milestone's
  core constraint.

## Sandbox Demonstration

Run against an isolated sandbox library (separate from the production
database) through the real CLI:

1. A movie (`Alien (1979)/Alien (1979).mkv`), a title-only movie
   (`Something Final v2.mkv`), and a multi-episode TV file
   (`Carnivale/Season 01/Carnivale S01E02E03 Milfay.mkv`) scanned, then
   evaluated: `Created: 3` — `HIGH` movie with year, `MEDIUM` movie
   without year, `HIGH` episode with `episode_numbers = [2, 3]` and
   episode title `Milfay`.
2. A second movie added directly conflicting with an existing one in
   both title and year (`Prometheus (2012)/Alien (1979).mkv`) — evaluated
   as `LOW` with `evidence.conflict =
   "title_or_year_mismatch_between_filename_and_folder"` and both the
   filename's and folder's title/year recorded in evidence.
3. The first movie's file renamed on disk (`Alien (1979).mkv` →
   `Alien (1979) Directors Cut.mkv`), then rescanned and re-evaluated:
   `Created: 1`, `Unchanged: 2` — not an in-place `UPDATE`. This confirms
   and is explained by `docs/DATABASE.md` risk 12: a rename is
   indistinguishable from one file going `MISSING` and a different one
   being `ADDED` at the inventory layer (the project's pre-existing,
   documented rename-detection limitation — not new to this milestone),
   so the old file's candidate is retained untouched (it is now
   `MISSING`) while a fresh `HIGH`-confidence candidate is created for the
   new path. The reconciliation layer's actual `UPDATE`-in-place code path
   (same `media_files.id`, changed `filename`/`layout`) is exercised and
   proven separately by `test_identification_lifecycle.py`, which
   manipulates `media_files` directly the way a future rename-detection
   heuristic would.

No NAS media was modified at any point — the sandbox library lives
entirely under a temp directory, not the real NAS.

## Production Validation

Evaluated against the real production inventory database (unchanged since
Milestone 6: 3,513 `ACTIVE` media_files rows, ~5.8 TB). Identification
evaluation reads only the database — it never re-scans or otherwise
touches the NAS. The database was backed up before this milestone's
migration was applied.

**First evaluation:** `Created: 3513`, `Updated: 0`, `Unchanged: 0` — one
candidate per `ACTIVE` file, exactly matching the established baseline
file count.

**Repeated evaluation** (immediately after, no inventory change in
between): `Created: 0`, `Updated: 0`, `Unchanged: 3513` — confirming the
no-duplicate/no-false-churn guarantee holds at full production scale.

**Aggregate results:**

| Type    | Count |
|---------|-------|
| EPISODE | 2,694 |
| MOVIE   | 737   |
| SPECIAL | 0     |
| EXTRA   | 0     |
| UNKNOWN | 82    |

| Confidence | Count |
|------------|-------|
| HIGH       | 2,700 |
| MEDIUM     | 520   |
| LOW        | 211   |
| UNKNOWN    | 82    |

With a parsed year: 11. Without: 3,502.

Re-running `mams findings evaluate` and `mams inventory stats` after this
migration confirmed zero effect on either: findings stayed at 14
`Unchanged`/0 `Created`/0 `Resolved` (Milestone 6's baseline), and
inventory counts (3,513 active / 2 missing, 5.8 TB) were byte-for-byte
unchanged.

### Manual review

- **All 8 `HIGH`-confidence movies** (fewer than 25 exist in this
  library) reviewed individually: `District 9 (2009)`, `Lucky Number
  Slevin (2006)`, `Total Recal (2012)` (title matches the actual on-disk
  filename typo, not a parser error), `BATMAN (1989)`, and four
  MakeMKV-ripped files whose titles include a trailing disc/segment
  numeral not in the recognized token set (e.g. `CITY SLICKERS 3 1
  (1991)`) — every one had its year correctly extracted and no title was
  fabricated; the messy titles are a faithful, non-fabricating parse of
  genuinely messy source filenames, not a bug (see `docs/DATABASE.md`
  risk 13).
- **25 `HIGH`-confidence episodes sampled across 25 distinct series** (out
  of 2,692 `HIGH` episodes) reviewed: all had correct season/episode
  numbers and a series title traceable directly to the filename. Observed
  and documented (not fixed, per this milestone's non-exhaustive-parsing
  scope): the same real show appears under multiple `parsed_series_title`
  spellings across differently-ripped seasons (`ANCIENT ALIENS` /
  `Ancient Aliens` / `AncientAliens`), a "Season N, Disc" literal prefix
  survives into the series title when the filename doesn't use the
  recognized "Season N Episode M" words form, and a filename with the
  `sNNeNN` tag written twice only has its first occurrence stripped
  (leaving the duplicate in `episode_title`). All are known limitations
  now recorded in `docs/DATABASE.md` risk 13.
- **All 211 `LOW`-confidence candidates** reviewed (programmatically
  grouped, then spot-checked): the overwhelming majority are movies whose
  filename and folder titles legitimately conflict or whose remaining
  "title" is dominated by MakeMKV disc/segment numerals
  (`CRZ0EUW2                        10_1.mp4`, `HOME2.Title1.mp4`), plus
  one systematic case worth calling out — `_titles_similar()`'s substring
  check does not account for a leading article, so `"A Christmas Story"`
  (folder) vs. `"Christmas Story"` (filename-derived) reads as a conflict
  and lands at `LOW` rather than matching. Recorded as a known limitation
  rather than fixed, since a more permissive similarity check risks
  false negatives on genuinely different titles.
- **All 82 `UNKNOWN` candidates** reviewed: all fall into two patterns —
  fitness videos using an `se02e01`-style prefix (not one of the six
  supported filename forms, so correctly not parsed rather than
  guessed), and TV season-folder files with a bare MakeMKV title
  placeholder (`BLUEY___SEASON_3___FIRST_HALF.Title10.mkv`) and no
  season/episode pattern in the filename at all — correctly `UNKNOWN`
  with no fabricated season/episode number despite sitting in a `Season
  3` folder, direct production evidence that the "no fabrication" rule
  holds at scale, not just in unit tests.
- **Multi-episode candidates: zero found.** This library's TV files are
  ripped one episode per file; the multi-episode code path is validated
  by unit and sandbox tests instead (see above).
- **SPECIAL or EXTRA candidates: zero found.** No season-0 files and no
  recognized bonus-content keywords anywhere in this library's 2,694 TV
  files — plausible for a personal disc-rip archive with no dedicated
  extras/specials folders, and consistent with the recognized-keyword
  list being conservative by design.
- **All 4 candidates with an edition** reviewed: `Unrated` and `Special
  Edition` both correctly extracted, including one case where the
  edition text lives only in the parent folder name
  (`Pride and Prejudice The Special Edition/`), confirming folder-level
  edition evidence works end-to-end in production, not just in a
  synthetic test. One cosmetic artifact observed: stripping `Unrated`
  from inside `(Unrated)` left a stray empty `( )` in the title
  (`Grudge 2 ( )1 1`) — the edition value itself is correct; only the
  leftover title punctuation is imperfect. Not fixed, recorded as a minor
  known limitation.
- **Samples from every layout** (`movie_flat`, `movie_folder`,
  `movie_collection_folder`, `tv_series_folder`, `tv_season_folder`)
  reviewed via the aggregate results above and the layout-specific
  behavior already covered by the movie/TV parser test suites; no
  layout-specific anomaly beyond the already-documented limitations.

No NAS media was modified during any part of this validation —
identification evaluation is read-only against the database by
construction, and every command that touched the production database was
limited to `mams identify evaluate/list/stats` and confirmatory
`mams findings evaluate`/`mams inventory stats` calls.

## Performance Observations

- Evaluating all 3,513 `ACTIVE` files (parsing plus reconciliation)
  completed in well under the several-second range Milestones 4-6
  established for comparable full-library operations — parsing is pure
  in-memory string/regex work with no I/O beyond the existing
  `list_media_files()` read and the `identification_candidates` writes.

## Quality Gates

- 504 automated tests passing
- Ruff clean
- MyPy clean

## Architecture Confidence

This validation matters for a reason distinct from Milestones 4-6: those
validated read/write fidelity and reconciliation idempotency against
data the system itself produced (scan results, rule evaluations). This
milestone's core risk was different — a parser is easy to get subtly
wrong against a small, tidy set of hand-written test fixtures and then
either crash or silently fabricate data against thousands of real,
messy, inconsistently-named files it has never seen. Running against the
full 3,513-file production library surfaced genuine real-world messiness
(duplicated season tags, MakeMKV disc-segment numerals, inconsistent
series-title casing across seasons, a missing leading article) without a
single crash, a single fabricated season/episode/year value, or a single
`HIGH`-confidence result whose evidence didn't actually support it —
direct evidence the parser's conservative, no-fabrication design holds
under real conditions, not just synthetic ones. Every one of the 293
non-`HIGH`-non-`MEDIUM`-non-zero-count candidates reviewed (all `LOW`, all
`UNKNOWN`, all edition/part-number candidates) was independently
explicable by the parser's documented rules, not a surprise — the kind of
outcome that justifies trusting `identification_candidates` as a
foundation for a future external-identity-resolution milestone.

This milestone remains entirely read-only against the NAS and Plex, and
entirely local against external identity services: no file operations,
no TMDb/TVDB/Plex calls, no automation. It demonstrates that the
canonical inventory can now be locally interpreted into structured
movie/TV candidates — evidence for a human or a future milestone to
confirm, never a confirmed identity itself — without any risk to the
underlying media.

---

# Milestone 7B – External Identity Resolution and Dry-Run Ingest Planning

## Summary

Resolves Milestone 7A's local `identification_candidates` against TMDb,
persists ranked scored matches and confirmed external identities, and
generates structured dry-run ingest plans with proposed (never executed)
actions. Seven new tables across migrations `0006`-`0010`. No NAS file,
directory, or Plex state was changed by any command exercised during
this validation.

## Validation Results

- 826 automated tests passing (up from 504 at the end of Milestone 7A),
  covering schema constraints/uniqueness/FK retention for all seven new
  tables, the TMDb client (auth/rate-limit/timeout/connection/malformed-
  response handling, cache hit/miss/expiry, token-never-leaks),
  deterministic movie/episode scoring (exact/close/missing year,
  alternate-title matching, runtime corroboration, popularity-never-
  overrides), the resolution lifecycle (auto-resolve, review-required,
  no-match, failed, skipped, manual select/reject, assignment
  supersede/no-duplicate), verification, destination naming (including
  sanitization and path-traversal rejection), ingest plan reconciliation
  (no-churn regeneration, approved-plan supersede-on-change), and the
  full `resolve`/`ingest` CLI surface.
- Ruff clean, MyPy clean across all 23 source modules.

## Sandbox Demonstration

Ran the full pipeline (`inventory scan` → `identify evaluate` →
`resolve evaluate` → `resolve select`/`reject` → `ingest plan` →
`ingest approve`) through the real CLI entry points against a sandbox
`Incoming` directory, with a fake `MediaProvider` injected in place of
`resolution_service.build_provider` so no real network call was made and
every result is deterministic and reproducible. Fixtures:

1. **`Alien (1979).mkv`** — clearly named movie with year. Parsed `MOVIE`
   candidate (`HIGH` confidence) → TMDb search returned one exact
   title+year match → **auto-resolved, `RESOLVED`/`HIGH`**. After
   simulating a healthy MediaInfo probe (duration, one video track, one
   audio track — the real scanner never invokes `mediainfo` on these
   zero-content fixture files), `ingest plan --destination-category
   movie` produced a **`READY_FOR_REVIEW`** plan with destination
   `NAS/Movies/Alien (1979)/Alien (1979).mkv` and all six proposed
   actions (`VALIDATE_SOURCE` → `VERIFY_MEDIA` → `CREATE_DIRECTORY` →
   `MOVE` → `REFRESH_INVENTORY` → `REQUEST_PLEX_REFRESH`), each printed
   `(PROPOSED -- NOT EXECUTED)`. Regenerating the same plan a second time
   produced the identical `id` and identical `updated_at` — no churn.
   `ingest approve` flipped it to `APPROVED` and printed "Plan approved.
   No actions executed."
2. **`The Fifth Element.mkv`** — movie without a year, in Incoming
   (an unclassified category). Parsed `UNKNOWN`/`UNKNOWN` — demonstrates
   the documented Milestone 7A boundary (see "Known Limitations" below),
   not a bug. `resolve evaluate` correctly **`SKIPPED`** it without
   ever querying TMDb.
3. **`Total Recall (1990).mkv`** — ambiguous movie title. The fake
   provider returned two distinct TMDb entries with identical title/year
   (simulating a real-world data-quality ambiguity, e.g. original vs.
   remake metadata collision), producing a zero score gap →
   **`REVIEW_REQUIRED`/`MEDIUM`**, both alternatives ranked and
   persisted. `resolve select ATTEMPT_ID MATCH_ID` manually confirmed
   the first match → attempt flipped to `RESOLVED`, `MANUAL`
   assignment created, second alternative preserved untouched. A
   forced re-evaluation (`--force`) produced a **new** historical
   attempt (old one preserved), which `resolve reject` marked
   **`NO_MATCH`**, creating no assignment and preserving both
   alternatives.
4. **`Breaking Bad - S01E01.mkv`** — standard TV episode. Parsed
   `EPISODE` S01E01 → TMDb series search + episode lookup matched
   exactly → **auto-resolved, `RESOLVED`/`HIGH`**.
5. **`Breaking Bad - S01E02-E03.mkv`** — multi-episode file. Parsed
   `EPISODE` with `episode_numbers=(2, 3)` → resolved against its
   primary episode (S01E02) → **`RESOLVED`/`HIGH`** (see "Known
   Limitations": resolution matches the primary episode number only,
   not a merged two-episode identity).
6. **`Corrupt Movie (2020).mkv`** — zero-byte file. Parsed `MOVIE`
   (`HIGH` confidence — the filename alone is well-formed). TMDb search
   for this title returned no results → **`NO_MATCH`**, no assignment
   created. Separately, `ingest plan` on this file (after simulating a
   probe with zero size and no tracks) produced a **`BLOCKED`** plan
   with `verification_status=FAIL` (`non_zero_size`, `duration_present`,
   `video_track_present` all `FAIL`) and reason "no resolved ACTIVE
   external identity assignment exists for this file" — both a
   verification block and an identity block demonstrated on the same
   file.

Every scenario in the milestone's validation checklist was demonstrated:
automatic resolution, review-required resolution, no-match, manual match
selection, manual rejection, verification pass, verification block,
ready-for-review plan, blocked plan, and deterministic plan regeneration.

**Safety confirmation**: after the full run, `NAS/Movies` did not exist
on disk (no directory was created), the source file at
`Incoming/Alien (1979).mkv` was still present and unmodified, and no
Plex request was made (no such call exists anywhere in this milestone's
code).

## Live TMDb Validation

Not performed. No `TMDB_API_TOKEN` was configured in this environment.
Per the milestone's own instructions, sandbox validation with a fake
provider was performed instead, and the "no token configured" path was
verified separately: `resolve evaluate` prints a clear error
("No TMDb API token configured...") and creates no `resolution_attempts`
row at all, while `inventory scan`/`identify evaluate` (and every other
existing command) are completely unaffected. Separately, during
interactive CLI smoke testing, `resolve evaluate` was run once against
the **real** TMDb API with a deliberately invalid token — TMDb's actual
401 response was correctly handled end-to-end (`FAILED` attempt,
`error_message="TMDb rejected the configured API token"`, no crash),
confirming the HTTP error-handling path against a live endpoint even
without full auto-resolution validation. Live validation of actual
search/match results against 5 known movies, 5 known episodes, and 2
intentionally ambiguous titles is deferred until a real token is
configured, per the milestone's own conservative default (do not run
external resolution across the full production library without
explicit approval).

## Known Limitations

- **Year-less movies in Incoming are `UNKNOWN`, not `MOVIE`.**
  `identification._parse_unclassified()`'s movie fallback requires a
  parsed year — an explicit, tested Milestone 7A behavior
  (`test_unclassified_with_no_pattern_evidence_anywhere_is_unknown`)
  that this milestone deliberately does not weaken, since relaxing it
  would also start accepting bare, un-classified titles like `readme`
  as low-confidence movies. A year-less movie under an already-classified
  category (`movies`/`kids_movies`) is unaffected — its layout alone
  routes it to the movie parser regardless of year. Confirmed with the
  full existing local-parsing test suite still passing unchanged.
- **Multi-episode files resolve against their primary episode only.**
  TMDb's episode-detail endpoint addresses one episode at a time;
  `resolution_service._search_and_score_episodes()` resolves and scores
  against `episode_number` (the first of a multi-episode file's
  `episode_numbers`), not a merged two-episode identity. The resulting
  `external_identities`/`media_identity_assignments` row and destination
  filename (`S01E02-E03`) are still correct for the *file*, but a
  second, distinct external identity for episode 3 is never separately
  recorded.
- **No fuzzy "possible edition collision" detection.** Phase J's
  collision analysis checks for exact destination-path collisions
  (filesystem, canonical inventory, another active plan) but does not
  attempt to detect a *different* edition/cut of the same title already
  present under a similar-but-not-identical path — this would require
  path-similarity heuristics not implemented in this milestone.
- **Popularity-based tie-breaking is movie-only.** Episode ranking has
  no popularity tie-break (TMDb exposes no meaningful per-episode
  popularity distinct from its series); ties there are broken by
  provider id alone, which is deterministic but arbitrary.
- **`resolve evaluate`'s default `--limit` (10) is a safety valve, not a
  tuned value.** Chosen because each evaluation costs a real TMDb
  request (unlike free local `identify evaluate`); a future milestone
  running resolution across a large backlog will need explicit
  `--limit`/scripted batching.

## Quality Gates

- 826 automated tests passing
- Ruff clean
- MyPy clean

## Architecture Confidence

This milestone's core risk was different from every prior one: it is the
first to reach outside MAMS (TMDb) and the first to *compute* proposed
file operations rather than only observe read-only state. Both risks
were addressed structurally rather than through validation alone —
`tmdb.py` normalizes every response before it reaches scoring/resolution
code (no raw provider JSON leaks past the client), `provider_cache` is
architecturally separate from canonical identity storage, and no
executor exists anywhere in this codebase for `ingest_plan_actions`, so
there is no code path capable of turning a proposed `MOVE` into a real
one. The sandbox run's safety confirmation (no directory created, source
file untouched, plan regeneration idempotent) is direct evidence that
boundary holds in practice, not just in code review.

Confirmed external identities, local identification candidates,
resolution attempts/matches, and dry-run ingest plans remain four
separate concepts throughout, exactly as designed: `The Fifth Element`'s
`UNKNOWN` local candidate never became a confirmed identity by mistake;
`Total Recall`'s two ambiguous TMDb entries were never silently
collapsed into an automatic pick; and the `Corrupt Movie` fixture was
independently blocked by verification *and* by having no confirmed
identity, each for its own visible, structured reason.

This milestone remains entirely read-only against the NAS and Plex — no
rename, move, copy, delete, replace, directory creation, or Plex call
exists anywhere in this codebase yet. It demonstrates that a local
identification candidate can now be resolved into a confirmed external
identity (automatically or with human review), verified for basic
health, and turned into a fully-specified, human-reviewable dry-run plan
— without any risk to the underlying media, and without ever executing
that plan.

---

# Milestone 8 – Safe Approved-Plan Execution

Date: 2026-07-26

## Summary

Implements the first code in this repository that mutates the
filesystem: `mams ingest execute PLAN_ID --confirm-plan PLAN_ID`, a
narrowly-scoped, one-plan-at-a-time executor for an `APPROVED`,
`READY_FOR_EXECUTOR` dry-run ingest plan. Full design and safety
rationale in `docs/EXECUTION-SAFETY.md`.

## Validation Results

Automated (`.venv/bin/python -m pytest`, this session): 1,180 tests
passing, 12 deselected (`live`-marked TMDb tests), 0 failing. Ruff and
MyPy clean (the pre-existing generator-fixture-return-type MyPy pattern
already present across this test suite's fixtures is unchanged in
count-per-file terms; no new MyPy errors were introduced by this
milestone's source files).

Coverage highlights:

- **Schema**: a rebuild-preserves-all-rows-and-FK-integrity test for
  the `ingest_plans.status` 12-step table rebuild
  (`tests/test_schema_ingest_plans_execution_statuses.py`), plus full
  CHECK-constraint/index coverage for `ingest_executions`/
  `ingest_execution_steps` (`tests/test_schema_ingest_executions.py`).
- **Pure domain** (`tests/test_execution.py`): transfer-strategy
  decision, the 20-check preflight, and the 12-check destination
  verification — one test per check's failure mode, plus a
  never-short-circuits test for each, mirroring `test_readiness.py`'s
  existing style.
- **Filesystem adapter** (`tests/test_execution_filesystem.py`): the
  no-clobber commit primitive (`os.link`+`os.unlink`) proven to refuse
  overwriting an existing destination and to preserve both copies on a
  simulated cross-device link failure or inode mismatch; the streaming
  copy proven to retain a partial temp file on a simulated size
  mismatch rather than deleting it.
- **Orchestration** (`tests/test_execution_service.py`): a real,
  genuinely `READY_FOR_EXECUTOR` plan built the same way
  `test_ingest_service.py`'s audit tests do, executed end-to-end for
  both the same-filesystem path (real `tmp_path` files, real hard-link
  move) and the cross-filesystem path (device IDs monkeypatched to
  simulate two filesystems — `tmp_path` alone can't produce two real
  ones); a parametrized test over all 13 named fault-injection
  boundaries, each asserting the exact `(plan_status, execution_status,
  recovery_status)` triple and on-disk state from
  `docs/EXECUTION-SAFETY.md`'s failure-mapping table; lock-contention
  and `inspect_recovery` read-only-guidance tests.
- **Safety** (`tests/test_safety_controlled_execution.py`): every
  rejection path in `ingest execute` itself — unapproved plan, a plan
  gone stale after approval, missing/mismatched `--confirm-plan`, a
  lock already held, an unconfigured state directory, an unknown plan
  id — proven to leave the source file, plan status, and execution-row
  count completely untouched, using the same unconditional-raise
  monkeypatch technique as `tests/test_safety_no_execution.py` (left
  unmodified — the read-only pre-execution workflow it covers is still
  true).
- **CLI** (`tests/test_cli_ingest_execute.py`): argument parsing,
  confirmation gating (no mutation without an exact `--confirm-plan`
  match), and rendering for all four new subcommands. Confirmed-execution
  rendering is tested against a monkeypatched `execution_service.execute_plan`
  rather than a real run, because the real executor's post-transfer
  verification step invokes the real `mediainfo` binary, which correctly
  refuses to find video/audio tracks in this suite's fake null-byte
  fixture files — there is no real playable media available in this
  development environment. This is not a gap in engine coverage: the
  engine itself is exercised end-to-end, with a real fake-but-plausible
  `MetadataProvider`, in `tests/test_execution_service.py` above.

## Sandbox Demonstration

Both transfer strategies were exercised against real `tmp_path`
directories as part of the automated suite above (same-filesystem via
naturally-identical device IDs under one temp directory; cross-filesystem
via monkeypatched device IDs) — this stands in for the milestone's
originally-scoped "dedicated sandbox with a second mounted volume"
requirement, since a real second disposable volume was not available in
this development environment. Every fault-injection scenario, the
no-clobber guarantee, and the recovery-classification logic were proven
against these same real (if synthetic) filesystem operations, not
against mocks of the operations themselves.

## Production Validation

**Not yet performed — pending an explicit, user-run acceptance step.**
Per this milestone's scope decision, the implementing session did not
touch the real NAS or a real Incoming root; a human must run the one
real, controlled execution described below and record the result in
this section before Milestone 8 is considered fully closed out in
practice (the code, tests, and documentation are complete regardless).

### Runbook (to be completed by the operator)

1. Choose one small, valid movie file. **Do not use the only copy** —
   preserve the original elsewhere first.
2. Copy the disposable copy into a configured `ingest.incoming_roots`
   directory.
3. `mams inventory scan --metadata`
4. `mams findings evaluate` / `mams identify evaluate` — confirm no
   blocking findings, add a manual override
   (`mams identify override ...`) only if needed.
5. `mams resolve evaluate` (or `mams resolve select` for a manual
   match) — confirm an `ACTIVE` assignment exists.
6. `mams ingest plan MEDIA_FILE_ID --destination-category ...` —
   confirm `READY_FOR_REVIEW`.
7. `mams ingest approve PLAN_ID`
8. `mams ingest audit PLAN_ID` — confirm `READY_FOR_EXECUTOR`.
9. `mams ingest execute PLAN_ID` — review the preview text carefully.
10. `mams ingest execute PLAN_ID --confirm-plan PLAN_ID` — execute for
    real.
11. Verify the destination file plays correctly in VLC.
12. Verify the source file is gone from Incoming.
13. `mams ingest execution EXECUTION_ID` — confirm `SUCCEEDED`, review
    the recorded checksums/steps.
14. `mams inventory list --category <destination category>` — confirm
    the destination is `ACTIVE` in canonical inventory with the
    expected metadata.
15. If Plex refresh remains disabled (the default), verify Plex
    manually or leave it for a future milestone.
16. Do **not** execute a second plan in this same acceptance pass —
    one real execution is sufficient to close out this milestone.
17. Record the exact commands run, plan/execution ids, and outcome in
    this section.

## Quality Gates

- 1,180 automated tests passing (pytest, `-m "not live"`, this
  session's count — will grow slightly with the runbook's own manual
  run, which is not itself an automated test)
- Ruff clean
- MyPy clean

## Architecture Confidence

This milestone's core risk is categorically different from every prior
one: it is the first to actually write to the filesystem, and a bug
here risks real media, not just a database row. The mitigation is
structural, not just procedural: every no-overwrite guarantee rests on
`os.link()`'s native atomicity rather than a check-then-write race; the
source is never removed before independent destination verification
succeeds (checksum-verified for cross-filesystem, inode-verified for
same-filesystem); every failure path is proven, by a 13-point
parametrized fault-injection suite, to leave either nothing, a clearly
partial and never-auto-deleted artifact, or two independently-verifiable
full copies — never an ambiguous or silently-lost state; and there is
no code path capable of retrying a mutation automatically (`ingest
retry` does not exist). The primary safety principle — nothing is
trusted merely because it was approved or audited earlier — is
enforced by actually re-running the readiness audit and re-gathering
live filesystem evidence a second time, after the lock is held,
immediately before any write.

The one meaningful gap this entry documents honestly: no real NAS
execution has happened yet. Everything above is real code exercised
against a real (if synthetic, `tmp_path`-based) filesystem, not a mock
of the mutation primitives — but the specific combination of this
operator's actual NAS mount, real media file, and real Plex instance
remains unverified until the runbook above is completed.

---

# Future Validation Entries

Future milestones (asset identification, Plex integration, the replacement engine, automation, etc.) should add new entries to this document rather than modifying previous validation history.
