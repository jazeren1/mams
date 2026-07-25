"""Tests for provider_cache_repository.py: get_entry/put_entry SQL and the
SqliteCacheStore adapter."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mams.db import connect, migrate
from mams.provider_cache_repository import SqliteCacheStore, get_entry, put_entry

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"


@pytest.fixture()
def connection(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "mams.db"
    migrate(db_path, REPO_MIGRATIONS_DIR)
    conn = connect(db_path)
    yield conn
    conn.close()


def test_get_entry_returns_none_when_missing(connection: sqlite3.Connection) -> None:
    assert get_entry(connection, provider="TMDB", request_key="k") is None


def test_put_then_get_round_trips(connection: sqlite3.Connection) -> None:
    put_entry(
        connection,
        provider="TMDB",
        request_key="k",
        endpoint="/search/movie",
        response_json='{"results": []}',
        status_code=200,
        ttl_seconds=3600,
    )
    entry = get_entry(connection, provider="TMDB", request_key="k")
    assert entry is not None
    assert entry.response_json == '{"results": []}'
    assert entry.status_code == 200


def test_put_overwrites_existing_entry_in_place(connection: sqlite3.Connection) -> None:
    put_entry(
        connection, provider="TMDB", request_key="k", endpoint="/x", response_json="a", status_code=200, ttl_seconds=3600
    )
    put_entry(
        connection, provider="TMDB", request_key="k", endpoint="/x", response_json="b", status_code=200, ttl_seconds=3600
    )
    count = connection.execute("SELECT COUNT(*) FROM provider_cache").fetchone()[0]
    assert count == 1
    entry = get_entry(connection, provider="TMDB", request_key="k")
    assert entry is not None
    assert entry.response_json == "b"


def test_expired_entry_is_treated_as_a_miss(connection: sqlite3.Connection) -> None:
    put_entry(
        connection,
        provider="TMDB",
        request_key="k",
        endpoint="/x",
        response_json="a",
        status_code=200,
        ttl_seconds=-1,  # already expired
    )
    assert get_entry(connection, provider="TMDB", request_key="k") is None


def test_different_request_keys_do_not_collide(connection: sqlite3.Connection) -> None:
    put_entry(
        connection, provider="TMDB", request_key="a", endpoint="/x", response_json="1", status_code=200, ttl_seconds=3600
    )
    put_entry(
        connection, provider="TMDB", request_key="b", endpoint="/x", response_json="2", status_code=200, ttl_seconds=3600
    )
    assert get_entry(connection, provider="TMDB", request_key="a") is not None
    assert get_entry(connection, provider="TMDB", request_key="b") is not None
    count = connection.execute("SELECT COUNT(*) FROM provider_cache").fetchone()[0]
    assert count == 2


def test_sqlite_cache_store_get_put(connection: sqlite3.Connection) -> None:
    store = SqliteCacheStore(connection, provider="TMDB", ttl_seconds=3600)
    assert store.get(request_key="k") is None
    store.put(request_key="k", endpoint="/search/movie", response_json='{"a": 1}', status_code=200)
    assert store.get(request_key="k") == '{"a": 1}'


def test_sqlite_cache_store_scopes_to_its_configured_provider(connection: sqlite3.Connection) -> None:
    store = SqliteCacheStore(connection, provider="TMDB", ttl_seconds=3600)
    store.put(request_key="k", endpoint="/x", response_json="a", status_code=200)
    row = connection.execute("SELECT provider FROM provider_cache WHERE request_key = 'k'").fetchone()
    assert row["provider"] == "TMDB"
