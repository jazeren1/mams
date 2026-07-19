# Changelog

## 0.2.0

Added a read-only library inventory scanner (`mams inventory scan`). Recursively
discovers media files under the NAS category paths in `config/config.yaml`,
detects flat/folder-per-movie and series/season TV layouts, and writes a
JSON report and a human-readable summary under `reports/`. The scanner never
renames, moves, deletes, checksums, or otherwise modifies scanned media.

## 0.1.0

Initial project foundation: instructions, current state, playbook, architecture, naming, NAS policy, verification, Plex strategy, schema, roadmap, decision log, Python scaffolding, and inventory templates.
