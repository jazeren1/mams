"""SQLite connection handling and schema migrations.

The database schema is defined entirely by numbered, idempotent SQL files
under `database/migrations/` (e.g. `0001_initial.sql`). `migrate()` applies
whichever files are newer than the database's recorded `schema_version` and
records each one as it lands. Migration files are never edited after being
committed; a schema change is always a new, higher-numbered file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_MIGRATIONS_DIR = "database/migrations"


def connect(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection


def _discover_migrations(migrations_dir: str | Path) -> list[tuple[int, Path]]:
    """Return (version, path) pairs for migration files, sorted by version.

    Files must be named `<int>_<description>.sql`, e.g. `0001_initial.sql`.
    """
    directory = Path(migrations_dir)
    migrations = [(int(path.name.split("_", 1)[0]), path) for path in directory.glob("*.sql")]
    migrations.sort(key=lambda item: item[0])
    return migrations


def current_schema_version(connection: sqlite3.Connection) -> int:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, "
        "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    row = connection.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
    return row["version"] or 0


def migrate(database_path: str | Path, migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR) -> list[int]:
    """Apply any migrations newer than the database's current schema version.

    Applied in ascending version order. Migration files are expected to use
    `CREATE TABLE/INDEX IF NOT EXISTS`, so a crash between a migration's
    script and its `schema_version` record is safe to retry. Returns the
    versions applied, oldest first; an empty list means the database was
    already current.
    """
    with connect(database_path) as connection:
        current = current_schema_version(connection)
        applied: list[int] = []
        for version, path in _discover_migrations(migrations_dir):
            if version <= current:
                continue
            connection.executescript(path.read_text(encoding="utf-8"))
            connection.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (version,))
            applied.append(version)
        return applied
