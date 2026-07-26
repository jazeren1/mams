"""Tests for scan_runs.scan_scope/scope_category (database/migrations/0015_scan_scope.sql).

Exercises the migration file directly against a temp database, same
approach as test_schema_scan_changes.py, so a constraint typo in the .sql
file itself gets caught.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mams.db import connect, current_schema_version, migrate

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"


def _migrations_dir_through(tmp_path: Path, *versions: str) -> Path:
    scoped = tmp_path / "scoped_migrations"
    scoped.mkdir(exist_ok=True)
    for version in versions:
        matches = list(REPO_MIGRATIONS_DIR.glob(f"{version}_*.sql"))
        assert len(matches) == 1, f"expected exactly one migration file for {version}"
        (scoped / matches[0].name).write_text(matches[0].read_text(encoding="utf-8"), encoding="utf-8")
    return scoped


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "mams.db"
    versions = [f"{n:04d}" for n in range(1, 16)]
    migrate(path, _migrations_dir_through(tmp_path, *versions))
    return path


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def test_migrate_applies_scan_scope_schema(db_path: Path) -> None:
    with connect(db_path) as connection:
        assert current_schema_version(connection) == 15


def test_scan_runs_defaults_to_full_scope_with_no_category(db_path: Path) -> None:
    with connect(db_path) as connection:
        cursor = connection.execute("INSERT INTO scan_runs DEFAULT VALUES")
        scan_id = _lastrowid(cursor)
        row = connection.execute(
            "SELECT scan_scope, scope_category FROM scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["scan_scope"] == "FULL"
        assert row["scope_category"] is None


def test_scan_runs_accepts_category_scope_with_a_category(db_path: Path) -> None:
    with connect(db_path) as connection:
        cursor = connection.execute(
            "INSERT INTO scan_runs (scan_scope, scope_category) VALUES ('CATEGORY', 'incoming')"
        )
        scan_id = _lastrowid(cursor)
        row = connection.execute(
            "SELECT scan_scope, scope_category FROM scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["scan_scope"] == "CATEGORY"
        assert row["scope_category"] == "incoming"


def test_scan_runs_rejects_invalid_scan_scope(db_path: Path) -> None:
    with connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO scan_runs (scan_scope) VALUES ('PARTIAL')")


def test_migrate_from_scratch_applies_at_least_through_0015(tmp_path: Path) -> None:
    path = tmp_path / "mams.db"
    applied = migrate(path, REPO_MIGRATIONS_DIR)
    assert 15 in applied


def test_migrate_is_idempotent_after_0015(tmp_path: Path, db_path: Path) -> None:
    versions = [f"{n:04d}" for n in range(1, 16)]
    migrations_dir = _migrations_dir_through(tmp_path, *versions)
    applied_again = migrate(db_path, migrations_dir)
    assert applied_again == []
