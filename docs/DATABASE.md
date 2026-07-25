# Database Design

SQLite is the version 1 database (`config.database_path`), one file, three
layers of tables:

- **Cataloging** (`discs`, `assets`, `files`, `jobs`, `replacements`,
  `events`): the future rip → identify → replace → Plex pipeline described
  in `ARCHITECTURE.md`. Defined but not yet wired to any code.
- **Inventory** (`libraries`, `scan_runs`, `media_files`, `video_tracks`,
  `audio_tracks`, `subtitle_tracks`, `scan_changes`): what `mams inventory
  scan` discovers on the NAS today, plus an immutable log of what changed
  on each scan. This is what most of this document covers.
- **Findings** (`findings`): deterministic conditions detected by
  evaluating the canonical inventory (missing files, metadata errors,
  zero-byte files, and similar), persisted and reconciled by `mams
  findings evaluate`. See "`findings`" below.
- **Identification candidates** (`identification_candidates`): local,
  deterministic parses of each file's path/filename into a structured but
  unconfirmed movie or TV interpretation, persisted and reconciled by
  `mams identify evaluate`. Never calls TMDb/TVDB/Plex or any external
  service. See "`identification_candidates`" below.

Schema changes are versioned migrations under `database/migrations/`, never
applied by hand to a deployed database. See "Migration strategy" below.

## Principle: the database is canonical, the filesystem is discovery

**The database is the canonical inventory. The filesystem is the source of
discovery, not the source of truth.**

A scan reads the filesystem to find out what changed, then reconciles that
into the database. Once a `media_files` row exists, the database's record
of it (including a soft `MISSING` state) persists even after the file is no
longer visible on a scan — nothing is inferred fresh from a directory
listing on every read. Reports, diffs, and queries are all generated from
the database, never from a live filesystem walk. This is what makes
`mams inventory diff` and incremental scanning possible: the database
remembers what the filesystem looked like last time, the filesystem only
ever tells it what's different *now*.

This does not weaken the project's read-only guarantee: scans still only
ever read directory entries, file metadata, and (optionally) `mediainfo`
output. The database is written to; the NAS never is.

## ER diagram (text)

```text
libraries (1) ──────< media_files (N)          [library_id]
scan_runs (1) ──────< media_files (N)          [first_seen_scan_id, last_seen_scan_id, missing_since_scan_id]
media_files (1) ──< video_tracks (N)           [media_file_id]
media_files (1) ──< audio_tracks (N)           [media_file_id]
media_files (1) ──< subtitle_tracks (N)        [media_file_id]

scan_runs (1) ───────< scan_changes (N)        [scan_run_id, no ON DELETE]
media_files (0..1) ──< scan_changes (N)        [media_file_id, ON DELETE SET NULL]
libraries (0..1) ─────< scan_changes (N)       [library_id, ON DELETE SET NULL]

media_files (0..1) ──< findings (N)            [media_file_id, ON DELETE SET NULL]
libraries (0..1) ─────< findings (N)           [library_id, ON DELETE SET NULL]

media_files (1) ──< identification_candidates (0..1)  [media_file_id, ON DELETE CASCADE]

[future, not this milestone] media_files (N) >─── (1) assets
```

## Table definitions

### `libraries`

One row per configured NAS category, synced from `config.yaml` at the
start of every scan. `config.yaml` is authoritative; this table mirrors
it — never edited independently. If `root_path` changes in config, the
next scan overwrites it here.

```sql
CREATE TABLE libraries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL UNIQUE,
    root_path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

No `CHECK` on `category` — it's user-editable config, not a code-owned
enum.

### `scan_runs`

One row per `mams inventory scan` invocation. Append-only audit log of
scan history.

```sql
CREATE TABLE scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('RUNNING','COMPLETE','FAILED')) DEFAULT 'RUNNING',
    metadata_enabled INTEGER NOT NULL DEFAULT 0,
    mediainfo_version TEXT,
    file_count INTEGER,
    total_size_bytes INTEGER,
    error_message TEXT,
    -- added in 0003_scan_changes.sql:
    added_count INTEGER,
    updated_count INTEGER,
    missing_count INTEGER,
    restored_count INTEGER
);
```

`mediainfo_version` records the `mediainfo` CLI tool's version (only
meaningful when `metadata_enabled`), captured once per run via
`mediainfo --Version`. MediaInfo's own field set and detection behavior
changes across tool versions; this gives an audit trail when explaining
why two scans of the same file disagree.

Scan duration is **not** a stored column — it's `completed_at -
started_at`, and storing it as well would duplicate a value already fully
derivable from other columns on the same row. Same reasoning as dropping
`media_info_json` below: one representation, not two that can drift.

`added_count`/`updated_count`/`missing_count`/`restored_count` are a
deliberate exception to that rule: they're a rollup of `scan_changes` rows
for this `scan_run_id` (by `change_type`), computed once via a `GROUP BY`
query when the scan completes. They *are* derivable from `scan_changes`,
but the derivation requires a join/aggregate over a different table, not
just reading other columns on the same row — the same justification
`file_count`/`total_size_bytes` already had before this milestone.
Populated only on `COMPLETE`, `NULL` otherwise, exactly like those two
columns. This lets `mams inventory stats`' "most recent scan" summary stay
a single-row read with no `scan_changes` query at all.

### `media_files`

One row per discovered file, upserted by `absolute_path` (its identity
key). This is the canonical inventory record — see the principle above.

```sql
CREATE TABLE media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id INTEGER NOT NULL REFERENCES libraries(id),
    absolute_path TEXT NOT NULL UNIQUE,
    relative_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    extension TEXT NOT NULL,
    parent_directory TEXT NOT NULL,
    layout TEXT NOT NULL CHECK (layout IN
        ('movie_flat','movie_folder','movie_collection_folder',
         'tv_series_folder','tv_season_folder','unknown')),
    size_bytes INTEGER NOT NULL,
    mtime REAL,                  -- st_mtime, epoch seconds; for future incremental scans
    state TEXT NOT NULL CHECK (state IN ('ACTIVE','MISSING')) DEFAULT 'ACTIVE',

    -- General-track MediaInfo fields only (inherently 1:1 with the file,
    -- unlike video/audio/subtitle tracks — no normalization needed here)
    container TEXT,
    duration_seconds REAL,
    overall_bitrate INTEGER,
    media_info_error TEXT,
    media_info_probed_at TEXT,

    first_seen_scan_id INTEGER NOT NULL REFERENCES scan_runs(id),
    last_seen_scan_id INTEGER NOT NULL REFERENCES scan_runs(id),
    missing_since_scan_id INTEGER REFERENCES scan_runs(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

`category` is not a column here — it's replaced by `library_id`, avoiding
repetition of category/root metadata on every file row.

Only `mtime` is captured, not `ctime`. `ctime` semantics over a
network-mounted NAS (QNAP over SMB/AFP/NFS) are inconsistent and not
reliably an inode metadata-change time the way local POSIX `ctime` is;
`mtime` is the reliable signal and the only one a future incremental-scan
milestone should build on.

A file going `MISSING` on a rescan is a `state` flip, never a `DELETE` —
consistent with "never destroy data." Its row, and its track rows, are
left intact.

### `video_tracks`, `audio_tracks`, `subtitle_tracks`

The canonical — and only — representation of per-track MediaInfo detail.
There is no `media_info_json` blob: normalized tables are the single
source of truth, so there's nothing that can drift out of sync with a
duplicate representation.

```sql
CREATE TABLE video_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    track_index INTEGER NOT NULL,      -- ordinal position among this file's video tracks
    codec TEXT,
    width INTEGER,
    height INTEGER,
    aspect_ratio TEXT,
    frame_rate REAL,
    hdr_format TEXT,
    bit_depth INTEGER,
    scan_type TEXT
);

CREATE TABLE audio_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    track_index INTEGER NOT NULL,
    codec TEXT,
    language TEXT,
    channels INTEGER,
    bitrate INTEGER,
    is_default INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE subtitle_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    track_index INTEGER NOT NULL,
    language TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    is_forced INTEGER NOT NULL DEFAULT 0
);
```

`container`/`duration_seconds`/`overall_bitrate` stay as scalar columns on
`media_files` deliberately — MediaInfo's `General` track is always exactly
one per file, so there's no multiplicity to normalize away. Video, audio,
and subtitle tracks are 1:N, which is what actually warrants child tables.

### `scan_changes`

One row per `ADDED`/`UPDATED`/`MISSING`/`RESTORED` event, written in the
same transaction as the `media_files`/track writes it describes. Immutable
once written — nothing ever updates a `scan_changes` row after insert;
`mams inventory diff` reads these rows directly rather than reconstructing
history from `media_files`' current state.

```sql
CREATE TABLE scan_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_run_id INTEGER NOT NULL REFERENCES scan_runs(id),
    media_file_id INTEGER REFERENCES media_files(id) ON DELETE SET NULL,
    library_id INTEGER REFERENCES libraries(id) ON DELETE SET NULL,
    change_type TEXT NOT NULL CHECK (change_type IN ('ADDED','UPDATED','MISSING','RESTORED')),
    absolute_path TEXT NOT NULL,
    previous_absolute_path TEXT,
    details_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

**Why `media_file_id`/`library_id` are nullable with `ON DELETE SET NULL`,
not `CASCADE`, and why `absolute_path` is denormalized onto the row:** no
code path deletes `media_files` or `libraries` rows today — `media_files`
only ever soft-transitions to `MISSING` (see above). But `scan_changes` is
meant to be historical evidence, and evidence that silently disappears the
moment its subject is removed isn't evidence. `CASCADE` would delete the
event along with the file it describes, which is exactly backwards for an
audit log — the event is often the last remaining record of what happened
*before* something was deleted. `SET NULL` keeps the row (with its
`change_type`, `absolute_path`, `details_json`, and timestamp intact) and
only detaches the now-meaningless foreign key. `absolute_path` is written
directly onto every `scan_changes` row (not read through the
`media_files` join) for the same reason: an event stays fully
interpretable — "what happened, to which path, when" — even if its
`media_file_id`/`library_id` are later nulled out. `scan_run_id`, by
contrast, has no `ON DELETE` clause at all (SQLite's default, which blocks
the delete): `scan_runs` is already an append-only log nothing deletes, so
there's no legitimate scenario to accommodate — a delete attempt should
fail loudly, not silently detach.

`previous_absolute_path` exists for a future rename/move-detection
heuristic; nothing populates it yet. The current write path detects a file
by `absolute_path` (its identity key — see `media_files` above), so a path
change is indistinguishable from one file going `MISSING` and a different
one being `ADDED`. The column is reserved now so that a future heuristic
doesn't need a schema change to record what it detects.

**`details_json` shape.** `NULL` for `ADDED`/`MISSING` (the `change_type`
is fully self-explanatory). For `UPDATED`/`RESTORED`, a compact, flat
structure:

```json
{"changes": [{"field": "size_bytes", "old": 1234, "new": 5678}]}
```

Serialized with `json.dumps(sort_keys=True, separators=(",", ":"))` for
byte-for-byte determinism — verified in tests across independent runs, not
just structural equality. Track-field changes report counts, never the
track payload:

```json
{"field": "video_tracks", "old_count": 1, "new_count": 2}
```

This is a deliberate line: `details_json` is *about* the change (which
fields, roughly how), never a second copy of the data itself. Dumping full
before/after track objects here would make `scan_changes` an alternative,
un-normalized MediaInfo store that could drift from `video_tracks`/
`audio_tracks`/`subtitle_tracks` — precisely the problem normalizing those
tables in the first place was meant to avoid (see above). If a future
feature needs the full before/after track detail, it belongs in a
purpose-built structure, not smuggled into this column.

### `findings`

One row per (rule, media file) condition detected by evaluating the
canonical inventory, reconciled — not reinserted — by every `mams findings
evaluate` run. This is a read-only engine: evaluation only reads
`media_files`/track tables and writes `findings`; it never touches the NAS,
Plex, or any other table.

```sql
CREATE TABLE findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_code TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('INFO','WARNING','ERROR','CRITICAL')),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','RESOLVED','IGNORED')) DEFAULT 'ACTIVE',
    media_file_id INTEGER REFERENCES media_files(id) ON DELETE SET NULL,
    library_id INTEGER REFERENCES libraries(id) ON DELETE SET NULL,
    summary TEXT NOT NULL,
    evidence_json TEXT,
    recommendation TEXT,
    first_detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

**Identity/uniqueness.** `UNIQUE(rule_code, media_file_id)`, enforced by
`idx_findings_rule_media_file`. This is the initial, and so far sufficient,
finding-key design: every rule in `src/mams/findings.py` evaluates a single
`media_files` row and needs no other dimension (no per-track findings, no
cross-file findings) to stay unambiguous. A future rule needing a finer key
(e.g. a per-track finding) would need a documented change to this
uniqueness strategy, not a workaround bolted onto this one.

**Why no `absolute_path`/`relative_path`/`category` column.** Unlike
`scan_changes` (which denormalizes `absolute_path` onto every row so an
event stays interpretable after its file is gone), `findings` deliberately
resolves path and category via a `LEFT JOIN` to `media_files`/`libraries`
at read time (see `findings_repository.list_findings`/`get_finding`). Two
reasons this is the right tradeoff here, unlike for `scan_changes`:
`findings` rows are reconciled in place and re-read constantly (`mams
findings list`/`show`), so a join per read is cheap and keeps exactly one
place path/category can drift from; and nothing deletes `media_files` rows
today, so the "evidence must outlive its subject" scenario `scan_changes`
guards against is not yet a live concern for findings either. If a future
milestone starts deleting `media_files` rows, a finding whose
`media_file_id` gets set `NULL` loses its resolvable path — acceptable for
now, revisit if that changes.

**Why no `scan_run_id` (or an `evaluation_runs` table).** `scan_changes` is
intrinsically per-scan — every row is generated by exactly one
`persist_scan()` call and makes no sense outside that context.
`findings evaluate` is different: it re-evaluates the *current* canonical
inventory state on demand, independent of when the last `mams inventory
scan` ran or how many scans have happened since the last evaluation.
Tying a finding to "the scan that produced it" would be meaningless (a
finding can persist, unchanged, across many scans and many evaluations).
No evaluation-run table is introduced either — `reconcile_findings()`
always evaluates the full, fixed rule set (`findings.ALL_RULES`) against
the full current inventory in one call, so "what was evaluated this run"
never needs to be recorded separately from "what the findings table
contains right now."

**`evidence_json`.** `NULL` when a rule has nothing beyond its `rule_code`
to say (e.g. `metadata_not_probed`); otherwise a compact, deterministic
encoding of the specific facts that rule based its judgment on —
`json.dumps(..., sort_keys=True, separators=(",", ":"))`, the same
convention as `scan_changes.details_json`. Contains only evidence (a byte
count, an error string, a threshold, a track count) — never a duplicate of
the `media_files` row itself, and never enough fields reassembled together
to reconstruct one either.

**FK retention behavior.** `media_file_id`/`library_id` are nullable with
`ON DELETE SET NULL`, the same retention rationale as `scan_changes` (see
above): a finding is evidence a condition existed, and that evidence must
outlive the row it describes if `media_files`/`libraries` rows are ever
deleted in the future (nothing deletes them today). `CASCADE` would erase
findings history the moment its subject disappeared — exactly backwards
for a record whose entire purpose is surfacing problems for a human to
review.

### `identification_candidates`

One row per `ACTIVE` media file, holding the current deterministic parse of
its path/filename into a structured movie or TV interpretation.
Reconciled — not reinserted — by every `mams identify evaluate` run,
exactly like `findings`. This is a read-only-against-the-NAS engine: it
only reads `media_files` and writes `identification_candidates`; it never
calls TMDb, TVDB, Plex, or any other external service. **A candidate is a
local interpretation of evidence, never a confirmed media identity** — the
schema and every CLI surface deliberately avoid language ("identified as",
"matched") that would suggest otherwise.

```sql
CREATE TABLE identification_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id INTEGER NOT NULL UNIQUE REFERENCES media_files(id) ON DELETE CASCADE,
    candidate_type TEXT NOT NULL CHECK (candidate_type IN ('MOVIE','EPISODE','SPECIAL','EXTRA','UNKNOWN')),
    parsed_title TEXT,
    parsed_year INTEGER,
    parsed_series_title TEXT,
    season_number INTEGER,
    episode_number INTEGER,
    episode_numbers_json TEXT,
    episode_title TEXT,
    edition TEXT,
    part_number INTEGER,
    special_type TEXT,
    confidence TEXT NOT NULL CHECK (confidence IN ('HIGH','MEDIUM','LOW','UNKNOWN')),
    parser_version INTEGER NOT NULL,
    evidence_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

**Identity/uniqueness.** `UNIQUE(media_file_id)`, enforced by
`idx_identification_candidates_media_file_id`. Unlike `findings`
(`UNIQUE(rule_code, media_file_id)` — many rules can each hold a row per
file), a file has exactly one *current* candidate. A re-parse (the file's
path, filename, or layout changed since the last evaluation, or
`parser_version` advanced) replaces that row's content in place; it never
accumulates a second row for the same file.

**FK retention: `ON DELETE CASCADE`, deliberately not `SET NULL`.** This is
the one place this schema's FK convention differs from `scan_changes` and
`findings`. Those two exist specifically to be *historical evidence that
outlives its subject* — a `scan_changes` row documents "this happened," a
`findings` row documents "this condition was observed" — so both use
`ON DELETE SET NULL` to survive a future `media_files` delete. A candidate
is the opposite kind of thing: it is not a record that something happened,
it is *the current interpretation of a file that still exists*, discarded
and reparsed the instant that file's path changes. An interpretation of a
file that has been deleted describes nothing, so `ON DELETE CASCADE`
removes the candidate along with the file it interprets. (Nothing deletes
`media_files` rows today — a missing file is a `state` flip, not a
delete — so this is a documented future-proofing choice, not a live
behavior difference; see the "identification_candidates lifecycle"
section below for what actually happens on the state flip that *does*
happen today.)

**Column notes:**

- `candidate_type` — `MOVIE`, `EPISODE`, `SPECIAL` (season 0 / explicit
  special evidence), `EXTRA` (bonus/deleted-scene/featurette content, not
  a numbered episode), or `UNKNOWN` (insufficient evidence to classify).
- `parsed_title` / `parsed_year` — movie fields; `NULL` for
  `EPISODE`/`SPECIAL` candidates.
- `parsed_series_title` / `season_number` / `episode_number` /
  `episode_numbers_json` / `episode_title` — TV fields; `NULL` for `MOVIE`
  candidates. `episode_number` holds the first/primary episode number for
  simple filtering and display; `episode_numbers_json` holds the full
  ordered list (as a compact JSON array, e.g. `[2,3]`) for multi-episode
  files (`S01E02E03`, `S01E02-E03`). For a single-episode file the two are
  redundant by construction (`episode_numbers_json` is always `[episode_number]`)
  — accepted duplication, not drift, because "the primary episode number"
  needs to be a plain queryable/sortable column (see the query layer
  below) and "all episode numbers" needs to preserve the full list; there
  is no third representation to keep in sync.
- `edition` / `part_number` — movie fields for conservatively recognized
  edition labels (e.g. `"Director's Cut"`) and part/disc markers.
- `special_type` — set only for `SPECIAL`/`EXTRA` candidates when the
  filename gives a specific classification (e.g. `"behind the scenes"`,
  `"deleted scene"`); `NULL` when the type is known but the specific kind
  isn't.
- `confidence` — see "Confidence rubric" in `src/mams/identification.py`;
  never `HIGH` merely because a file lives in a movie/TV category
  directory — it reflects the strength of the *parsed* evidence.
- `parser_version` — an integer bumped whenever parsing logic changes
  meaningfully, so a future `mams identify evaluate` run can detect and
  re-evaluate candidates parsed by an older version. Not currently read by
  reconciliation to force a re-parse (every `evaluate` run re-parses every
  `ACTIVE` file unconditionally, so a version bump takes effect on the very
  next run) — it exists purely as an audit trail on each row: "which
  parser produced this."
- `evidence_json`. `NULL` only if no candidate was produced (never — a
  candidate row is always written, at minimum `UNKNOWN`); otherwise a
  compact, deterministic encoding (`json.dumps(..., sort_keys=True,
  separators=(",", ":"))`, same convention as `findings.evidence_json`)
  recording *how* the parse was derived: which evidence source each field
  came from (`"title_source": "filename"` vs `"parent_directory"`), the
  specific substrings matched (e.g. the raw `"(1979)"` or `"S01E02"`
  match), and the technical tokens stripped from the raw name (e.g.
  `["1080p", "BluRay", "x264"]`). Deliberately **not** a duplicate of the
  `media_files` row — it never repeats the full filename/path already on
  that row, only the parsing-specific facts a human would need to audit
  *why* this candidate looks the way it does.

## `identification_candidates` lifecycle

`identification_service.evaluate_candidates()` loads every `ACTIVE`
`media_files` row (via `inventory_repository.list_media_files(state="ACTIVE")`),
parses each one exactly once (`identification.evaluate_file()` — pure, no
SQL), and reconciles the results into `identification_candidates`, keyed
by `media_file_id`:

- **`ACTIVE` file with no existing candidate row** → INSERT. `created_at`
  and `updated_at` both start at `CURRENT_TIMESTAMP`.
- **`ACTIVE` file with an existing row whose parsed content is
  unchanged** → left untouched entirely (no `UPDATE` statement at all), so
  `id`, `created_at`, and `updated_at` are all stable and a repeated
  evaluation against an unchanged inventory produces zero writes for that
  file.
- **`ACTIVE` file with an existing row whose parsed content differs**
  (the file's path/filename/layout changed since the last evaluation, or
  the parser now produces a different result) → UPDATE every content
  column plus `updated_at`, preserving `id` and `created_at`.
- **`MISSING` file** → never visited by `evaluate_candidates()` at all
  (it only queries `state='ACTIVE'`). Any existing candidate for that file
  is left exactly as it was — this is the simplest of the three lifecycle
  options considered (remove / mark inactive / retain untouched) and
  requires no special-case code: retaining the last interpretation is
  also the most useful default for a future external-identity-resolution
  milestone, since a file that goes `MISSING` and is later `RESTORED`
  keeps its candidate the whole time.

Reconciliation runs inside one transaction
(`with connection:` in `identification_service.evaluate_candidates`, the
same pattern as `findings_service.evaluate_findings`); an exception
partway through rolls back every write made so far.

## Findings lifecycle

`findings_repository.reconcile_findings()` is given every `FindingCandidate`
produced by evaluating `findings.ALL_RULES` against every current
`media_files` row (both `ACTIVE` and `MISSING` — see `findings.py`'s rule
scoping), and reconciles them into `findings`, keyed by
`(rule_code, media_file_id)`:

- **No existing row for this key** → INSERT a new `ACTIVE` finding.
  `first_detected_at` and `last_detected_at` both start at
  `CURRENT_TIMESTAMP`.
- **Existing `ACTIVE` row** → id and `first_detected_at` are preserved.
  `last_detected_at` always advances (the condition was reconfirmed this
  run). `updated_at` — and the content columns
  (`severity`/`summary`/`evidence_json`/`recommendation`/`library_id`) —
  are touched *only* if that content actually changed since last time, so
  a repeated evaluation against unchanged inventory produces no
  unnecessary `updated_at` churn.
- **Existing `RESOLVED` row** → reactivated: `status` back to `ACTIVE`,
  `resolved_at` cleared, content refreshed to the current candidate,
  `last_detected_at`/`updated_at` advanced. `first_detected_at` is
  deliberately left untouched, so a finding's original detection time
  survives a resolve → reactivate cycle rather than resetting.
- **Existing `IGNORED` row** → status and `resolved_at` are never touched
  automatically (see below); only `last_detected_at` advances, recording
  that the condition is still present without disturbing the ignore.
- **Existing `ACTIVE` row whose key is absent from this run's
  candidates** (the condition is no longer present) → marked `RESOLVED`
  with `resolved_at` set. An already-`RESOLVED` row absent from the
  candidates is left alone (it's already resolved); an `IGNORED` row
  absent from the candidates is also left alone — see below.

Reconciliation runs inside one transaction (`with connection:` in
`findings_service.evaluate_findings`); an exception partway through rolls
back every write made so far, so a failed evaluation never leaves a
partially updated findings table.

**IGNORED findings.** No CLI command in this milestone sets a finding's
status to `IGNORED` — `mams findings` exposes `evaluate`/`list`/`stats`/
`show` only. The schema and reconciliation logic fully support it anyway
(deferred CLI exposure, not deferred design), so a future milestone can add
an "ignore" command without a schema or lifecycle change. Whichever way a
row becomes `IGNORED`, reconciliation never automatically flips it back to
`ACTIVE` or forward to `RESOLVED` in either direction — that would silently
erase a user's explicit decision to suppress a finding they've already
reviewed.

## Primary keys

`INTEGER AUTOINCREMENT` surrogates on all seven inventory tables. This
diverges from the cataloging tables' `TEXT` (UUID-style) primary keys —
deliberately: `discs`/`assets`/`files` anticipate external reference and
eventual multi-source merge, while inventory tables are single-writer,
single-machine scan bookkeeping with no merge scenario. Plain autoincrement
ints are simpler and smaller here.

`absolute_path` (on `media_files`) and `category` (on `libraries`) are the
natural/business keys, enforced via `UNIQUE`.

## Foreign keys

- `media_files.library_id → libraries.id`
- `media_files.first_seen_scan_id / last_seen_scan_id / missing_since_scan_id → scan_runs.id`
- `video_tracks.media_file_id`, `audio_tracks.media_file_id`,
  `subtitle_tracks.media_file_id → media_files.id` (`ON DELETE CASCADE` —
  track rows are wholly owned/derived data of a file, never independently
  meaningful)
- `scan_changes.scan_run_id → scan_runs.id` (no `ON DELETE` clause —
  `scan_runs` rows are never deleted; a delete attempt should fail, not
  cascade or detach)
- `scan_changes.media_file_id → media_files.id`,
  `scan_changes.library_id → libraries.id` (`ON DELETE SET NULL` — the
  opposite reasoning from the track tables: a `scan_changes` row is
  historical evidence, not owned/derived data, so it must outlive the row
  it describes. See "`scan_changes`" above for the full rationale.)
- `findings.media_file_id → media_files.id`,
  `findings.library_id → libraries.id` (`ON DELETE SET NULL` — same
  "evidence must outlive its subject" reasoning as `scan_changes`. See
  "`findings`" above.)
- `identification_candidates.media_file_id → media_files.id`
  (`ON DELETE CASCADE` — the opposite reasoning from `scan_changes`/
  `findings`: a candidate is a live interpretation of a still-existing
  file, not evidence that must outlive it. See "`identification_candidates`"
  above.)

Nothing yet references `discs`/`assets`/`files`/`jobs`/`replacements` —
that linkage (a nullable `asset_id` on `media_files`, or a join table for
multi-file assets) is deferred to a future migration once an
identification milestone exists.

## Required indexes

```sql
CREATE UNIQUE INDEX idx_libraries_category ON libraries(category);

CREATE UNIQUE INDEX idx_media_files_absolute_path ON media_files(absolute_path);
CREATE INDEX idx_media_files_library_id ON media_files(library_id);
CREATE INDEX idx_media_files_state ON media_files(state);
CREATE INDEX idx_media_files_library_layout ON media_files(library_id, layout);
CREATE INDEX idx_media_files_last_seen ON media_files(last_seen_scan_id);

CREATE INDEX idx_video_tracks_media_file_id ON video_tracks(media_file_id);
CREATE INDEX idx_video_tracks_hdr ON video_tracks(hdr_format) WHERE hdr_format IS NOT NULL;

CREATE INDEX idx_audio_tracks_media_file_id ON audio_tracks(media_file_id);
CREATE INDEX idx_audio_tracks_language ON audio_tracks(language);

CREATE INDEX idx_subtitle_tracks_media_file_id ON subtitle_tracks(media_file_id);
CREATE INDEX idx_subtitle_tracks_language ON subtitle_tracks(language);

CREATE INDEX idx_scan_changes_scan_run_id ON scan_changes(scan_run_id);
CREATE INDEX idx_scan_changes_media_file_id ON scan_changes(media_file_id);
CREATE INDEX idx_scan_changes_library_id ON scan_changes(library_id);
CREATE INDEX idx_scan_changes_change_type ON scan_changes(change_type);
CREATE INDEX idx_scan_changes_absolute_path ON scan_changes(absolute_path);

CREATE UNIQUE INDEX idx_findings_rule_media_file ON findings(rule_code, media_file_id);
CREATE INDEX idx_findings_status ON findings(status);
CREATE INDEX idx_findings_severity ON findings(severity);
CREATE INDEX idx_findings_rule_code ON findings(rule_code);
CREATE INDEX idx_findings_media_file_id ON findings(media_file_id);
CREATE INDEX idx_findings_library_id ON findings(library_id);

CREATE UNIQUE INDEX idx_identification_candidates_media_file_id ON identification_candidates(media_file_id);
CREATE INDEX idx_identification_candidates_type ON identification_candidates(candidate_type);
CREATE INDEX idx_identification_candidates_confidence ON identification_candidates(confidence);
CREATE INDEX idx_identification_candidates_season_number ON identification_candidates(season_number);
CREATE INDEX idx_identification_candidates_parsed_year ON identification_candidates(parsed_year);
```

## Migration strategy

Numbered, forward-only SQL files under `database/migrations/`:

- `0001_initial.sql` — the cataloging schema (`discs`, `assets`, `files`,
  `jobs`, `replacements`, `events`, `schema_version`), unchanged from the
  original bootstrap.
- `0002_inventory.sql` — `libraries`, `scan_runs`, `media_files`,
  `video_tracks`, `audio_tracks`, `subtitle_tracks`, and the indexes above.
- `0003_scan_changes.sql` — `scan_changes` and its indexes, plus the four
  `ADD COLUMN` statements on `scan_runs`.
- `0004_findings.sql` — `findings` and its indexes. Unlike `0003`, every
  statement is `CREATE TABLE/INDEX IF NOT EXISTS`, so this migration is
  fully retry-safe with no narrow-window caveat.
- `0005_identification_candidates.sql` — `identification_candidates` and
  its indexes. Also fully `IF NOT EXISTS`, retry-safe like `0004`.

`schema_version` tracks the highest applied migration number. A migration
runner applies any file numbered above the current version, in order,
inside one transaction, then records the new version. No down-migrations
in v1 — a bad migration is fixed by writing a new forward migration, not
reverting. An already-applied migration file is never edited; a new one is
always added instead.

**Caveat introduced by `0003`:** every `CREATE TABLE`/`CREATE INDEX` in
this project uses `IF NOT EXISTS`, which is what makes a migration safe to
blindly retry after a crash. SQLite's `ALTER TABLE ... ADD COLUMN` has no
`IF NOT EXISTS` form, so `0003` isn't retry-safe in the narrow window
between the script finishing and its `schema_version` row being recorded —
a retry there would fail with "duplicate column name." That window
requires a process crash at that exact instant; the fix if it ever happens
is a one-time manual correction, not a recurring risk. A bespoke
idempotency mechanism for this one edge case was judged not worth adding.

## Import/update strategy

Each `mams inventory scan` run, in one transaction:

1. **Sync `libraries` from `config.yaml`**: upsert by `category` — insert
   new categories, update `root_path` if changed, bump `updated_at`.
2. Insert a `scan_runs` row (`status='RUNNING'`), recording
   `mediainfo_version` up front if `--metadata` is set.
3. Walk the NAS exactly as today — unchanged, read-only. Capture
   `st_mtime` from the same `stat()` call already used for `size_bytes`.
4. Upsert `media_files` by `absolute_path`
   (`INSERT ... ON CONFLICT DO UPDATE`) — `size_bytes`, `mtime`, `layout`,
   `last_seen_scan_id`, `state='ACTIVE'`. General-track fields
   (`container`/`duration_seconds`/`overall_bitrate`) update only on a
   `--metadata` scan; a scan without `--metadata` must never null out
   metadata a previous scan collected.
5. **Track replacement, not merge**: when a file is probed with
   `--metadata`, delete its existing `video_tracks`/`audio_tracks`/
   `subtitle_tracks` rows and reinsert fresh ones from the current probe,
   in the same transaction as the `media_files` update. Track identity
   (`track_index`) isn't stable across probes — a re-rip can add, drop, or
   reorder tracks — so full replace-per-probe is deterministic and leaves
   no orphaned stale rows. A scan without `--metadata` must not touch
   track tables at all.
6. Any `ACTIVE` row in a scanned library with `last_seen_scan_id` less than
   this run's id becomes `MISSING` + `missing_since_scan_id`. Its track
   rows are left as-is — `ON DELETE CASCADE` only fires on an actual
   `media_files` delete, which a scan never performs.
7. `scan_runs` finalized to `COMPLETE` (with `file_count`/
   `total_size_bytes` computed from the database) or `FAILED` +
   `error_message`. The NAS is never touched regardless of outcome.
8. JSON/summary export reassembles the original `MediaInfo` nested shape
   via joins from `media_files` to the three track tables — the external
   report format is unchanged even though storage is normalized, not a
   blob.

### Change-event generation

Steps 4–6 above also produce `scan_changes` rows, in the same transaction
as the writes they describe — a rollback discards both together. Each
file's transition during one scan produces **at most one** event:

- No prior `media_files` row for this `absolute_path` → `ADDED`.
- Prior row was `MISSING` → `RESTORED`. Any other field changes that
  happened at the same time (the file was also resized while gone, say)
  ride along in the same event's `details_json` rather than generating a
  separate `UPDATED` — a file can only leave `MISSING` by being
  rediscovered.
- Prior row was `ACTIVE` and a comparable field changed → `UPDATED`, with
  changed field names and old/new values in `details_json`.
- Prior row was `ACTIVE` and nothing comparable changed → no event.
  "Comparable" deliberately excludes bookkeeping/timestamp columns
  (`updated_at`, `last_seen_scan_id`, `media_info_probed_at`, ...), so an
  identical repeat scan is silent by construction, not a special case. A
  scan without `--metadata` never touches metadata columns or track
  tables, so it has nothing to diff there either — it cannot produce a
  false metadata-change event.
- `MISSING` events come from step 6: the about-to-flip rows are selected
  *before* the bulk `UPDATE`, so each gets its own event with its own
  `absolute_path`.

Track content is compared **by value**
(`VideoTrack`/`AudioTrack`/`SubtitleTrack` equality), not by whether rows
were touched — delete+reinsert (step 5) always touches rows on a
successful probe, so a re-probe that finds byte-identical tracks must not
produce a false `UPDATED`.

At scan completion, `scan_runs.added_count`/`updated_count`/
`missing_count`/`restored_count` are set from one `GROUP BY change_type
... WHERE scan_run_id = ?` query — see the `scan_runs` table definition
above.

## CLI additions

- `mams init-db` — applies pending migrations from `database/migrations/`.
- `mams inventory scan` — unchanged flags; now also persists to the
  database. `--no-db` remains available as an escape hatch for
  debugging without touching the database.
- `mams inventory list [--category] [--state] [--layout] [--metadata-error]
  [--limit] [--json]` — filtered browsing over `media_files`, joined with
  track counts.
- `mams inventory stats [--json]` — per-library counts/sizes, layout/
  extension counts, metadata success/error/not-probed counts, track
  totals, and the most recent scan's summary.
- `mams inventory find QUERY [--category] [--state] [--limit] [--json]` —
  case-insensitive substring search over filename/relative_path/
  absolute_path.
- `mams inventory diff [--scan ID | --from-scan ID --to-scan ID] [--type]
  [--category] [--json]` — recorded `scan_changes` events. Defaults to the
  most recent `COMPLETE` scan. A `--from-scan`/`--to-scan` pair is an
  **event-range view** (`from_scan_id < scan_run_id <= to_scan_id`),
  explicitly not a reconstructed point-in-time snapshot comparison — see
  "`scan_changes`" above.
- `mams status` — currently reports only the database path and `dry_run`.
  The "schema version and last scan summary" this bullet used to promise
  is now `mams inventory stats`' job instead; `status` hasn't been
  revisited since.
- `mams findings evaluate [--json]` — evaluates every rule in
  `findings.ALL_RULES` against the current canonical inventory and
  reconciles the results into `findings` (see "Findings lifecycle" above).
  Writes only to the `findings` table; never touches the NAS, Plex, or any
  other table.
- `mams findings list [--status] [--severity] [--rule] [--category]
  [--limit] [--json]` — filtered browsing over `findings`, grouped by file
  then severity then rule declaration order (see `findings_repository.
  list_findings`).
- `mams findings stats [--json]` — totals by status, plus severity/rule
  breakdowns over `ACTIVE` findings.
- `mams findings show FINDING_ID [--json]` — one finding's full detail
  (status, severity, rule, resolved path, summary, evidence, recommendation,
  timestamps). A non-existent id prints a clear error and mutates nothing.
- `mams identify evaluate [--json]` — parses every `ACTIVE` media file's
  path/filename into a local `IdentificationCandidate` (see
  `identification.py`) and reconciles the results into
  `identification_candidates` (see "`identification_candidates` lifecycle"
  above). Writes only to that table; never calls TMDb, TVDB, Plex, or any
  other external service, and never touches the NAS.
- `mams identify list [--type] [--confidence] [--category] [--limit]
  [--json]` — filtered browsing over `identification_candidates`, joined
  with `media_files`/`libraries` for path/category. Every text and JSON
  surface labels this as a parsed local interpretation, never a confirmed
  identity.
- `mams identify stats [--json]` — totals by `candidate_type` and
  `confidence`, plus a with/without-`parsed_year` split.
- `mams identify show CANDIDATE_ID [--json]` — one candidate's full detail
  (type, confidence, parsed fields, evidence, parser version, timestamps).
  A non-existent id prints a clear error and mutates nothing.

## Risks and tradeoffs

1. **`absolute_path` as identity is mount-path-dependent.** A different
   mount point (different machine) makes every file look "new." Not a
   concern for the current single-machine setup; `(library_id,
   relative_path)` is the fallback stable key if this changes.
2. **Join cost on every per-file read.** Reassembling a file's full
   MediaInfo takes three joins instead of one blob read. Trivial at
   ~3,500 files with a handful of tracks each; revisit if the library
   grows an order of magnitude.
3. **Track row churn.** Delete+reinsert on every `--metadata` rescan means
   `video_tracks.id` etc. don't stay stable across probes. Fine as long as
   nothing outside this schema holds a track row id long-term.
4. **Two places libraries' root paths live** (`config.yaml` and the
   `libraries` table). Mitigated by the one-directional sync rule above —
   `config.yaml` always wins on the next scan.
5. **No `assets` linkage yet.** Deferred to a future migration; flagged
   now (nullable FK vs. join table) so that migration isn't a redesign.
6. **Primary-key style inconsistency** between inventory tables (int
   autoincrement) and cataloging tables (TEXT/UUID) — a deliberate,
   documented choice, not drift.
7. **`scan_changes` grows without bound.** Every `ADDED`/`UPDATED`/
   `MISSING`/`RESTORED` event is kept forever — there is no pruning or
   retention window. Reasonable at library scale and scan cadence (a few
   thousand files, occasional scans); revisit with a retention policy if
   scan frequency or library size grows enough for this to matter.
8. **No rename/move detection.** A file's identity is its `absolute_path`
   (see risk 1); a genuine rename or move is indistinguishable from one
   file going `MISSING` and an unrelated file being `ADDED` at a new path.
   `previous_absolute_path` is reserved on `scan_changes` for a future
   heuristic to populate; nothing does today, so `mams inventory diff`
   currently reports renames as an unrelated MISSING/ADDED pair, not as a
   single move event.
9. **`0003`'s `ALTER TABLE ADD COLUMN` statements aren't idempotent** the
   way this project's `CREATE TABLE/INDEX IF NOT EXISTS` migrations are —
   see "Migration strategy" above for the accepted, narrow-window risk.
10. **`findings` rows are never deleted, only resolved.** A condition that
    comes and goes repeatedly doesn't accumulate duplicate rows (the
    `(rule_code, media_file_id)` key reconciles in place), but a `RESOLVED`
    row itself is permanent history, same tradeoff as `scan_changes`.
    Reasonable at current library scale; revisit with a retention policy if
    that changes.
11. **`findings` path/category resolution depends on `media_files`/
    `libraries` rows still existing** (see "`findings`" above) — nothing
    deletes those today, so this is a documented future consideration, not
    a live gap.
12. **`identification_candidates`' "update in place" path is not reachable
    through today's real scan pipeline for a genuine file rename**, for
    the same reason as risk 8 above: a rename is indistinguishable from
    one file going `MISSING` and a different one being `ADDED` at the
    inventory layer, so `mams identify evaluate` only ever sees this as
    "retain the old file's candidate untouched (it's now `MISSING`) and
    create a fresh candidate for the new path" — never an `UPDATE` of the
    same row. The `UPDATE` path itself is real and tested (a `media_files`
    row whose `filename`/`layout` changes while its `id` stays the same,
    e.g. from a future rename-detection heuristic reusing
    `scan_changes.previous_absolute_path`), just not exercised by today's
    scanner. Verified in production: renaming a sandbox file end-to-end
    through `mams inventory scan` → `mams identify evaluate` produced
    exactly this MISSING-retained + ADDED-created pair, not an update.
13. **Known parser limitations, observed against the real 3,513-file
    production library** (none of these are bugs — each is the parser
    correctly, conservatively declining to guess beyond what this
    milestone's brief scoped in):
    - MakeMKV disc/title/segment numeric suffixes baked into ripped
      filenames (e.g. `..._DISC17_1.mp4`, `..._3_1.mp4`) are not
      recognized tokens, so they survive in `parsed_title` and can be
      misread as a `part_number` when they happen to follow the literal
      word "Disc" (see `docs/VALIDATION.md`'s Milestone 7A entry for
      examples). Distinguishing a real multi-disc "Part 2 of 2" from an
      arbitrary MakeMKV title-track index would require ripping-tool-
      specific knowledge out of this milestone's scope.
    - `_titles_similar()`'s substring check does not account for a
      leading article ("A Christmas Story" vs. "Christmas Story"), so
      that specific case reads as conflicting evidence (`LOW`) rather
      than a match.
    - A filename with the season/episode pattern repeated twice (e.g. a
      renaming tool applied twice) only has its *first* occurrence
      stripped; the second occurrence survives in `episode_title`.
    - Series titles are parsed independently per file with no cross-file
      canonicalization, so the same real show can appear under multiple
      `parsed_series_title` casings/spacings across different files (e.g.
      one series ripped across multiple seasons with inconsistent
      filename conventions). Reconciling these into one canonical series
      identity is exactly the kind of work deferred to a future external-
      identity-resolution milestone, not this one.

## Recommended implementation order

1. `database/migrations/` convention; move `schema.sql` →
   `0001_initial.sql` verbatim.
2. `0002_inventory.sql`: all six inventory tables and indexes. Migration
   runner + tests.
3. `libraries` sync from `config.yaml` on scan start.
4. Database write path for `media_files` (upsert, `mtime` capture,
   missing-flip) — no track writes yet, tested against a temp database and
   fixture directories.
5. Track replace-on-probe logic for `--metadata` scans, in the same
   transaction as step 4.
6. Switch JSON/summary generation to read from the database via joins;
   verify existing report-shape tests pass unchanged.
7. `mams inventory diff` / `mams inventory list`.
8. Docs and changelog updates, one small commit per step above.
