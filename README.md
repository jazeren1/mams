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
