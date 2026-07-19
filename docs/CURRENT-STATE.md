# Current State

## Collection

- Approximately 800 movies
- About 20% Blu-ray and 80% DVD
- Dozens of complete TV series
- Fitness content, Kids Movies, and Kids Shows
- Music exists but is out of scope

## Existing NAS roots

```text
Fitness/
Kids Movies/
Kids Shows/
Movies/
Music/
TV/
```

Movies and Kids Movies contain a mix of flat files and one-folder-per-movie layouts. TV-like categories use a series folder, sometimes with season folders and sometimes with all seasons mixed together.

## Target layout for new content

Movies may remain flat:

```text
Movies/District 9 (2009).mkv
```

TV-like categories use season folders:

```text
TV/Carnivàle/Season 01/Carnivàle - S01E01 - Milfay.mkv
```

No mass migration is required for version 1.

## Hardware

MacBook Pro; OWC optical enclosure; LG BH16NS40 Blu-ray drive; QNAP NAS; Plex Media Server.

## Software

Installed: MakeMKV, MediaInfo, VLC.

Planned: FileBot, Python, SQLite, Tautulli, Bazarr.

## Validated examples

The Dark Knight Rises, Lucky Number Slevin, Carnivàle Season 1 Disc 1, and District 9.

## Storage outlook

The finished archive is likely to require roughly 11–14 TB depending on TV volume and future upgrades. Compression is not part of the archive-master strategy.

## Implemented tooling

- `mams inventory scan`: a read-only scanner that recursively discovers
  media files under the NAS category paths in `config/config.yaml`,
  detects flat/folder-per-movie and series/season TV layouts, and writes a
  JSON report and a human-readable summary under `reports/`. It never
  renames, moves, deletes, checksums, or otherwise modifies scanned media.
