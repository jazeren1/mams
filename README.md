# Media Archive Management System (MAMS)

A personal media preservation and Plex library automation project.

## Mission

Build a verified, maintainable archive of physical media while preserving source quality, standardizing naming, safely replacing older encodes, and reducing manual work over time.

## Current scope

- Movies
- Kids Movies
- TV
- Kids Shows
- Fitness

Music exists on the NAS but is out of scope for the first version.

## Core principles

1. Preserve source quality during ingestion.
2. Prove workflows manually before automating them.
3. Keep every destructive operation reversible until verification succeeds.
4. Treat the catalog/database as the system of record.
5. Treat Plex as a consumer of the archive, not the archive itself.
6. Keep filenames simple and Plex-friendly; store technical detail in the database.

## Validated workflows

- Blu-ray movie ripping with MakeMKV
- DVD movie ripping with MakeMKV
- DVD TV episode ripping with MakeMKV
- MediaInfo verification
- VLC playback spot checks
- Plex discovery of replacement files
- Plex removal of deleted source files after a library scan

## Quick start

1. Read `PROJECT-INSTRUCTIONS.md`.
2. Read `docs/CURRENT-STATE.md`.
3. Review `docs/PLAYBOOK.md`.
4. Copy `config/config.example.yaml` to `config/config.yaml`.
5. Update NAS paths and Plex library names.
6. Initialize the database using `python scripts/init_db.py`.
7. Begin with manual logging before enabling any file-moving automation.

## Inventory scanner

`mams inventory scan` is a read-only scanner that recursively discovers
media files under the NAS category paths configured in `config/config.yaml`
(Movies, Kids Movies, TV, Kids Shows, Fitness). It never renames, moves,
deletes, checksums, or otherwise modifies scanned media — it only reads
directory entries and file sizes.

```text
mams inventory scan
mams inventory scan --json
mams inventory scan --output reports/library.json
```

Each run writes a JSON report and a human-readable summary under
`reports/` (default: `reports/library.json` and
`reports/library-summary.txt`). Each discovered file records its category,
absolute and relative path, filename, extension, parent directory, size,
and detected layout (`movie_flat`, `movie_folder`, `movie_collection_folder`,
`tv_series_folder`, `tv_season_folder`, or `unknown`). A movie category file
two directory levels below its category root — collection/franchise folder,
then per-movie folder, e.g. `Movies/Star Wars/A New Hope/A New Hope_001.mp4`
— is reported as `movie_collection_folder`; anything nested deeper remains
`unknown`.

## Media metadata (MediaInfo)

`mams inventory scan --metadata` additionally enriches each discovered file
with technical metadata extracted from the `mediainfo` CLI tool: container
format, duration, overall bitrate; per video track codec, resolution,
aspect ratio, frame rate, HDR format, bit depth, and scan type; per audio
track codec, language, channel count, bitrate, and default flag; and per
subtitle track language, default flag, and forced flag. This is read-only —
it only invokes `mediainfo` and reads its JSON output; it never modifies
scanned media. Without `--metadata`, the scanner behaves exactly as
before. If `mediainfo` is not installed, or fails on a particular file, the
error is recorded on that file (`media_info_error`) and the scan continues.

```text
mams inventory scan --metadata
```

`mams mediainfo <path>` is a developer diagnostic command that runs the
same MediaInfo parser against a single file and prints the parsed result,
without rescanning the whole library. It is read-only and never modifies
the target file.

```text
mams mediainfo "/path/to/movie.mkv"
mams mediainfo "/path/to/movie.mkv" --json
```
