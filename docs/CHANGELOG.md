# Changelog

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
