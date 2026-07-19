# MAMS Engineering Guidelines

## Philosophy

MAMS is an archive-first media management system.

Preserve original quality.
Never destroy data.
Prefer verification over automation.

## Priorities

1. Safety
2. Determinism
3. Simplicity
4. Testability

## Rules

Never delete user media without explicit verification.

Never silently rename files.

Never modify the NAS unless the feature explicitly requires it.

SQLite is the system of record.

Plex is a consumer, not the source of truth.

Every feature must include:

- tests
- documentation updates
- changelog update

Prefer many small commits over one large commit.
