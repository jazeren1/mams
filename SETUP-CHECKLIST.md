# Setup Checklist

## ChatGPT Project

- [ ] Create a new ChatGPT Project named `Media Archive Management System`.
- [ ] Paste `PROJECT-INSTRUCTIONS.md` into the Project Instructions field.
- [ ] Upload the project ZIP.
- [ ] Upload the key Markdown files individually if ZIP contents are not indexed reliably.
- [ ] Start a new project chat and ask ChatGPT to confirm the architecture and immediate priorities.

## Local repository

- [ ] Unzip the bundle.
- [ ] Move it to a permanent location on the Mac.
- [ ] Initialize Git.
- [ ] Create a private GitHub repository.
- [ ] Commit version 0.1.0.
- [ ] Create a Python 3.11+ virtual environment.
- [ ] Install the package in editable mode.
- [ ] Copy `config/config.example.yaml` to `config/config.yaml`.
- [ ] Update local staging and NAS paths.
- [ ] Leave `dry_run: true`.
- [ ] Initialize SQLite.
- [ ] Run tests.

## NAS

- [ ] Confirm all Plex root paths.
- [ ] Create `MAMS-Backup` outside Plex roots.
- [ ] Create `replacements`, `failed`, and `quarantine` under it.
- [ ] Confirm the Mac can mount the NAS consistently.
- [ ] Record the final mounted paths in `config/config.yaml`.

## First implementation milestone

- [ ] Import completed examples: The Dark Knight Rises, Lucky Number Slevin, Carnivàle S01E01, Carnivàle S01E02, District 9.
- [ ] Log source type, current path, runtime, and status.
- [ ] Build read-only inventory commands before copy or delete commands.
