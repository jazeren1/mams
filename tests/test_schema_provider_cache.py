"""Tests for the provider_cache schema (database/migrations/0006_provider_cache.sql).

Exercises the migration file directly against a temp database, same
approach as test_schema_identification_candidates.py, so a constraint
typo in the .sql file itself gets caught.
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
    migrate(path, _migrations_dir_through(tmp_path, "0001", "0002", "0003", "0004", "0005", "0006"))
    return path


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _insert_cache_row(
    connection: sqlite3.Connection,
    *,
    provider: str = "TMDB",
    request_key: str = "movie_search:alien:1979",
    endpoint: str = "/search/movie",
    response_json: str | None = '{"results": []}',
    status_code: int | None = 200,
    expires_at: str = "2099-01-01T00:00:00",
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO provider_cache (provider, request_key, endpoint, response_json, status_code, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (provider, request_key, endpoint, response_json, status_code, expires_at),
    )
    return _lastrowid(cursor)


def test_migrate_applies_provider_cache_schema(db_path: Path) -> None:
    with connect(db_path) as connection:
        assert current_schema_version(connection) == 6
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "provider_cache" in tables


def test_cache_row_defaults(db_path: Path) -> None:
    with connect(db_path) as connection:
        row_id = _insert_cache_row(connection)
        row = connection.execute("SELECT * FROM provider_cache WHERE id = ?", (row_id,)).fetchone()
        assert row["fetched_at"] is not None
        assert row["error_message"] is None


def test_rejects_invalid_provider(db_path: Path) -> None:
    with connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_cache_row(connection, provider="TVDB")


def test_requires_request_key_endpoint_expires_at(db_path: Path) -> None:
    with connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO provider_cache (provider, endpoint, expires_at) VALUES ('TMDB', '/x', '2099')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO provider_cache (provider, request_key, expires_at) VALUES ('TMDB', 'k', '2099')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO provider_cache (provider, request_key, endpoint) VALUES ('TMDB', 'k', '/x')")


def test_uniqueness_rejects_duplicate_provider_request_key(db_path: Path) -> None:
    with connect(db_path) as connection:
        _insert_cache_row(connection, request_key="dup")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_cache_row(connection, request_key="dup")


def test_uniqueness_allows_same_request_key_for_different_provider(db_path: Path) -> None:
    with connect(db_path) as connection:
        _insert_cache_row(connection, provider="TMDB", request_key="shared")
        # No other provider is valid yet, but the composite key still
        # governs uniqueness -- a different request_key never collides.
        _insert_cache_row(connection, provider="TMDB", request_key="shared-2")
        count = connection.execute("SELECT COUNT(*) FROM provider_cache").fetchone()[0]
        assert count == 2


def test_cache_row_can_be_overwritten_in_place(db_path: Path) -> None:
    """A fresh fetch updates the existing row rather than accumulating
    history -- this is a cache, not an audit log."""
    with connect(db_path) as connection:
        row_id = _insert_cache_row(connection, request_key="k", response_json='{"a": 1}')
        connection.execute(
            "UPDATE provider_cache SET response_json = ?, fetched_at = CURRENT_TIMESTAMP WHERE id = ?",
            ('{"a": 2}', row_id),
        )
        count = connection.execute("SELECT COUNT(*) FROM provider_cache").fetchone()[0]
        assert count == 1
        row = connection.execute("SELECT response_json FROM provider_cache WHERE id = ?", (row_id,)).fetchone()
        assert row["response_json"] == '{"a": 2}'


@pytest.mark.parametrize(
    "index_name",
    ["idx_provider_cache_provider_request_key", "idx_provider_cache_expires_at"],
)
def test_required_index_exists(db_path: Path, index_name: str) -> None:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name = ?", (index_name,)
        ).fetchone()
        assert row is not None, f"missing index {index_name}"


def test_migrate_from_scratch_applies_at_least_through_0006(tmp_path: Path) -> None:
    path = tmp_path / "mams.db"
    applied = migrate(path, REPO_MIGRATIONS_DIR)
    assert applied[:6] == [1, 2, 3, 4, 5, 6]


def test_migrate_is_idempotent_after_0006(tmp_path: Path, db_path: Path) -> None:
    migrations_dir = _migrations_dir_through(tmp_path, "0001", "0002", "0003", "0004", "0005", "0006")
    applied_again = migrate(db_path, migrations_dir)
    assert applied_again == []
