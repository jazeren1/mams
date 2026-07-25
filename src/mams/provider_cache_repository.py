"""SQL-backed HTTP response cache for external metadata providers (TMDb).

Owns all `provider_cache` SQL. See docs/DATABASE.md ("provider_cache") for
retention/invalidation rationale. This is deliberately the *only* place a
provider's raw response is ever persisted -- every other table this
milestone adds stores only the specific normalized fields matching,
scoring, and planning actually need (see docs/DATABASE.md,
"external_identities").

Cache writes commit themselves immediately, independent of any caller's
surrounding transaction: a cache entry is disposable infrastructure, not
domain data, so its persistence is never tied to the correctness of a
resolution/ingest reconciliation.

`SqliteCacheStore` adapts this module's functions to `mams.tmdb.CacheStore`
(a structural `Protocol` -- no import of `mams.tmdb` is needed here).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class CacheEntry:
    response_json: str
    status_code: int | None
    fetched_at: str
    expires_at: str


def get_entry(connection: sqlite3.Connection, *, provider: str, request_key: str) -> CacheEntry | None:
    """Look up an unexpired cache entry. A row whose `expires_at` has
    passed is treated as a cache miss (not deleted -- expired rows are
    simply overwritten by the next `put_entry` for the same key)."""
    row = connection.execute(
        """
        SELECT response_json, status_code, fetched_at, expires_at
        FROM provider_cache
        WHERE provider = ? AND request_key = ? AND expires_at > CURRENT_TIMESTAMP
        """,
        (provider, request_key),
    ).fetchone()
    if row is None or row["response_json"] is None:
        return None
    return CacheEntry(
        response_json=row["response_json"],
        status_code=row["status_code"],
        fetched_at=row["fetched_at"],
        expires_at=row["expires_at"],
    )


def put_entry(
    connection: sqlite3.Connection,
    *,
    provider: str,
    request_key: str,
    endpoint: str,
    response_json: str,
    status_code: int | None,
    ttl_seconds: int,
) -> None:
    """Upsert one cache row, keyed by (provider, request_key). Overwrites
    any prior response for the same key -- a cache holds only the latest
    fetch, never history."""
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    connection.execute(
        """
        INSERT INTO provider_cache (provider, request_key, endpoint, response_json, status_code, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (provider, request_key) DO UPDATE SET
            endpoint = excluded.endpoint,
            response_json = excluded.response_json,
            status_code = excluded.status_code,
            fetched_at = CURRENT_TIMESTAMP,
            expires_at = excluded.expires_at,
            error_message = NULL
        """,
        (provider, request_key, endpoint, response_json, status_code, expires_at),
    )
    connection.commit()


class SqliteCacheStore:
    """Adapts this module's functions to `mams.tmdb.CacheStore` for one
    fixed provider. Structural match only -- `mams.tmdb` is never
    imported here, so this module stays free of any HTTP dependency."""

    def __init__(self, connection: sqlite3.Connection, *, provider: str, ttl_seconds: int) -> None:
        self._connection = connection
        self._provider = provider
        self._ttl_seconds = ttl_seconds

    def get(self, *, request_key: str) -> str | None:
        entry = get_entry(self._connection, provider=self._provider, request_key=request_key)
        return entry.response_json if entry is not None else None

    def put(self, *, request_key: str, endpoint: str, response_json: str, status_code: int) -> None:
        put_entry(
            self._connection,
            provider=self._provider,
            request_key=request_key,
            endpoint=endpoint,
            response_json=response_json,
            status_code=status_code,
            ttl_seconds=self._ttl_seconds,
        )
