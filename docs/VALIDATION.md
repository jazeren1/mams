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

# Future Validation Entries

Future milestones (asset identification, Plex integration, the replacement engine, automation, etc.) should add new entries to this document rather than modifying previous validation history.
