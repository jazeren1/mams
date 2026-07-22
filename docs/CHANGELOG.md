# Changelog

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
