from __future__ import annotations

from pathlib import Path

import pytest

from mams.db import connect, current_schema_version, migrate


def _write_migration(migrations_dir: Path, version: int, sql: str) -> None:
    migrations_dir.mkdir(parents=True, exist_ok=True)
    (migrations_dir / f"{version:04d}_test.sql").write_text(sql, encoding="utf-8")


def test_migrate_applies_pending_migrations_in_order(tmp_path: Path) -> None:
    db_path = tmp_path / "mams.db"
    migrations_dir = tmp_path / "migrations"
    _write_migration(migrations_dir, 1, "CREATE TABLE IF NOT EXISTS a (id INTEGER PRIMARY KEY);")
    _write_migration(migrations_dir, 2, "CREATE TABLE IF NOT EXISTS b (id INTEGER PRIMARY KEY);")

    applied = migrate(db_path, migrations_dir)

    assert applied == [1, 2]
    with connect(db_path) as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"a", "b", "schema_version"} <= tables
        assert current_schema_version(connection) == 2


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "mams.db"
    migrations_dir = tmp_path / "migrations"
    _write_migration(migrations_dir, 1, "CREATE TABLE IF NOT EXISTS a (id INTEGER PRIMARY KEY);")

    first = migrate(db_path, migrations_dir)
    second = migrate(db_path, migrations_dir)

    assert first == [1]
    assert second == []


def test_migrate_only_applies_versions_newer_than_current(tmp_path: Path) -> None:
    db_path = tmp_path / "mams.db"
    migrations_dir = tmp_path / "migrations"
    _write_migration(migrations_dir, 1, "CREATE TABLE IF NOT EXISTS a (id INTEGER PRIMARY KEY);")
    migrate(db_path, migrations_dir)

    _write_migration(migrations_dir, 2, "CREATE TABLE IF NOT EXISTS b (id INTEGER PRIMARY KEY);")
    applied = migrate(db_path, migrations_dir)

    assert applied == [2]


def test_migrate_records_schema_version_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "mams.db"
    migrations_dir = tmp_path / "migrations"
    _write_migration(migrations_dir, 1, "CREATE TABLE IF NOT EXISTS a (id INTEGER PRIMARY KEY);")
    _write_migration(migrations_dir, 2, "CREATE TABLE IF NOT EXISTS b (id INTEGER PRIMARY KEY);")

    migrate(db_path, migrations_dir)

    with connect(db_path) as connection:
        versions = [
            row["version"]
            for row in connection.execute("SELECT version FROM schema_version ORDER BY version")
        ]
        assert versions == [1, 2]


def test_migrate_against_real_initial_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "mams.db"
    repo_migrations_dir = Path(__file__).resolve().parents[1] / "database" / "migrations"

    applied = migrate(db_path, repo_migrations_dir)

    assert applied == [1]
    with connect(db_path) as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"discs", "assets", "files", "jobs", "replacements", "events"} <= tables


def test_connect_enables_foreign_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "mams.db"

    with connect(db_path) as connection:
        row = connection.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1


def test_connect_creates_parent_directory(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dir" / "mams.db"

    connect(db_path).close()

    assert db_path.parent.is_dir()


@pytest.mark.parametrize("missing_dir", ["does-not-exist"])
def test_migrate_with_no_migration_files_is_a_noop(tmp_path: Path, missing_dir: str) -> None:
    db_path = tmp_path / "mams.db"
    migrations_dir = tmp_path / missing_dir

    applied = migrate(db_path, migrations_dir)

    assert applied == []
