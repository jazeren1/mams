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

# Future Validation Entries

Future milestones (asset identification, Plex integration, the replacement engine, automation, etc.) should add new entries to this document rather than modifying previous validation history.
