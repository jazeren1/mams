"""Tests for the TMDb client (src/mams/tmdb.py).

Uses a fake `requests.Session`-shaped object and an in-memory `CacheStore`
so no test in this file makes a real network call, per the project's
`MetadataProvider`-style fake-injection convention (see mediainfo.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
import requests

from mams.tmdb import (
    DEFAULT_MAX_TIMEOUT_ATTEMPTS,
    TMDbAuthenticationError,
    TMDbClient,
    TMDbConnectionError,
    TMDbRateLimitError,
    TMDbResponseError,
    TMDbTimeoutError,
    build_cache_key,
)

FAKE_TOKEN = "SUPER-SECRET-TMDB-TOKEN-XYZ"


@dataclass
class FakeResponse:
    status_code: int
    _json: Any = None
    _malformed: bool = False

    def json(self) -> Any:
        if self._malformed:
            raise ValueError("not valid json")
        return self._json


@dataclass
class FakeSession:
    responses: list[Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def get(self, url: str, *, params: dict[str, Any], headers: dict[str, str], timeout: float) -> FakeResponse:
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, FakeResponse)
        return item


class InMemoryCache:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.put_calls: list[dict[str, Any]] = []

    def get(self, *, request_key: str) -> str | None:
        return self.store.get(request_key)

    def put(self, *, request_key: str, endpoint: str, response_json: str, status_code: int) -> None:
        self.put_calls.append(
            {"request_key": request_key, "endpoint": endpoint, "response_json": response_json, "status_code": status_code}
        )
        self.store[request_key] = response_json


def _client(session: FakeSession, cache: InMemoryCache | None = None) -> tuple[TMDbClient, InMemoryCache]:
    cache = cache or InMemoryCache()
    client = TMDbClient(token=FAKE_TOKEN, cache=cache, session=session)  # type: ignore[arg-type]
    return client, cache


# --- normalization ------------------------------------------------------------


def test_search_movie_normalizes_results() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 348,
                            "title": "Alien",
                            "original_title": "Alien",
                            "release_date": "1979-05-25",
                            "original_language": "en",
                            "popularity": 51.2,
                        }
                    ]
                },
            )
        ]
    )
    client, _ = _client(session)
    results = client.search_movie("Alien", year=1979)
    assert len(results) == 1
    result = results[0]
    assert result.provider_id == 348
    assert result.title == "Alien"
    assert result.original_title == "Alien"
    assert result.release_year == 1979
    assert result.original_language == "en"
    assert result.popularity == 51.2
    assert result.runtime_seconds is None


def test_get_movie_normalizes_detail_with_runtime() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "id": 348,
                    "title": "Alien",
                    "original_title": "Alien",
                    "release_date": "1979-05-25",
                    "original_language": "en",
                    "popularity": 51.2,
                    "runtime": 117,
                },
            )
        ]
    )
    client, _ = _client(session)
    result = client.get_movie(348)
    assert result is not None
    assert result.runtime_seconds == 117 * 60


def test_get_movie_returns_none_for_404() -> None:
    session = FakeSession([FakeResponse(404)])
    client, _ = _client(session)
    assert client.get_movie(999999999) is None


def test_search_tv_normalizes_results() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 1396,
                            "name": "Breaking Bad",
                            "original_name": "Breaking Bad",
                            "first_air_date": "2008-01-20",
                            "original_language": "en",
                            "popularity": 300.0,
                        }
                    ]
                },
            )
        ]
    )
    client, _ = _client(session)
    results = client.search_tv("Breaking Bad")
    assert len(results) == 1
    result = results[0]
    assert result.provider_id == 1396
    assert result.title == "Breaking Bad"
    assert result.first_air_year == 2008


def test_get_tv_series_normalizes_detail() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "id": 1396,
                    "name": "Breaking Bad",
                    "original_name": "Breaking Bad",
                    "first_air_date": "2008-01-20",
                    "original_language": "en",
                    "popularity": 300.0,
                },
            )
        ]
    )
    client, _ = _client(session)
    result = client.get_tv_series(1396)
    assert result is not None
    assert result.title == "Breaking Bad"
    assert result.first_air_year == 2008


def test_get_tv_episode_normalizes_detail() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {"id": 62085, "season_number": 1, "episode_number": 1, "name": "Pilot", "air_date": "2008-01-20", "runtime": 58},
            )
        ]
    )
    client, _ = _client(session)
    result = client.get_tv_episode(1396, 1, 1)
    assert result is not None
    assert result.provider_id == 62085
    assert result.series_provider_id == 1396
    assert result.season_number == 1
    assert result.episode_number == 1
    assert result.episode_title == "Pilot"
    assert result.runtime_seconds == 58 * 60


def test_get_tv_episode_returns_none_for_404() -> None:
    session = FakeSession([FakeResponse(404)])
    client, _ = _client(session)
    assert client.get_tv_episode(1396, 99, 99) is None


# --- error handling -----------------------------------------------------------


@pytest.mark.parametrize("status_code", [401, 403])
def test_authentication_failure_raises(status_code: int) -> None:
    session = FakeSession([FakeResponse(status_code)])
    client, _ = _client(session)
    with pytest.raises(TMDbAuthenticationError):
        client.search_movie("Alien")


def test_rate_limit_raises() -> None:
    session = FakeSession([FakeResponse(429)])
    client, _ = _client(session)
    with pytest.raises(TMDbRateLimitError):
        client.search_movie("Alien")


def test_unexpected_status_raises_response_error() -> None:
    session = FakeSession([FakeResponse(500)])
    client, _ = _client(session)
    with pytest.raises(TMDbResponseError):
        client.search_movie("Alien")


def test_malformed_json_body_raises_response_error() -> None:
    session = FakeSession([FakeResponse(200, _malformed=True)])
    client, _ = _client(session)
    with pytest.raises(TMDbResponseError):
        client.search_movie("Alien")


def test_missing_results_key_raises_response_error() -> None:
    session = FakeSession([FakeResponse(200, {"unexpected": "shape"})])
    client, _ = _client(session)
    with pytest.raises(TMDbResponseError):
        client.search_movie("Alien")


def test_malformed_result_item_raises_response_error() -> None:
    session = FakeSession([FakeResponse(200, {"results": [{"title": "Missing id field"}]})])
    client, _ = _client(session)
    with pytest.raises(TMDbResponseError):
        client.search_movie("Alien")


def test_connection_error_raises() -> None:
    session = FakeSession([requests.ConnectionError("boom")])
    client, _ = _client(session)
    with pytest.raises(TMDbConnectionError):
        client.search_movie("Alien")


def test_timeout_retries_then_succeeds() -> None:
    session = FakeSession([requests.Timeout(), FakeResponse(200, {"results": []})])
    client, _ = _client(session)
    assert client.search_movie("Alien") == []
    assert len(session.calls) == 2


def test_timeout_raises_after_retries_exhausted() -> None:
    session = FakeSession([requests.Timeout(), requests.Timeout()])
    client, _ = _client(session)
    with pytest.raises(TMDbTimeoutError):
        client.search_movie("Alien")
    assert len(session.calls) == DEFAULT_MAX_TIMEOUT_ATTEMPTS


# --- caching --------------------------------------------------------------


def test_cache_miss_performs_request_and_populates_cache() -> None:
    session = FakeSession([FakeResponse(200, {"results": []})])
    client, cache = _client(session)
    client.search_movie("Alien")
    assert len(session.calls) == 1
    assert len(cache.put_calls) == 1


def test_cache_hit_skips_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession([])  # would raise IndexError if a request were attempted
    cache = InMemoryCache()
    key = build_cache_key("/search/movie", {"query": "Alien", "year": None})
    cache.store[key] = '{"results": []}'
    client, _ = _client(session, cache)
    results = client.search_movie("Alien")
    assert results == []
    assert session.calls == []
    assert cache.put_calls == []


def test_build_cache_key_is_order_independent_and_drops_none() -> None:
    key_a = build_cache_key("/search/movie", {"query": "Alien", "year": 1979})
    key_b = build_cache_key("/search/movie", {"year": 1979, "query": "Alien"})
    assert key_a == key_b
    key_with_none = build_cache_key("/search/movie", {"query": "Alien", "year": None})
    key_without = build_cache_key("/search/movie", {"query": "Alien"})
    assert key_with_none == key_without


# --- provider-status diagnostic (verify_credentials) --------------------------


def test_verify_credentials_success() -> None:
    session = FakeSession([FakeResponse(200, {"success": True, "status_code": 1, "status_message": "OK"})])
    client, _ = _client(session)
    assert client.verify_credentials() is None
    assert session.calls[0]["url"].endswith("/authentication")


@pytest.mark.parametrize("status_code", [401, 403])
def test_verify_credentials_invalid_token_raises(status_code: int) -> None:
    session = FakeSession([FakeResponse(status_code)])
    client, _ = _client(session)
    with pytest.raises(TMDbAuthenticationError):
        client.verify_credentials()


def test_verify_credentials_rate_limited_raises() -> None:
    session = FakeSession([FakeResponse(429)])
    client, _ = _client(session)
    with pytest.raises(TMDbRateLimitError):
        client.verify_credentials()


def test_verify_credentials_network_failure_raises() -> None:
    session = FakeSession([requests.ConnectionError("boom")])
    client, _ = _client(session)
    with pytest.raises(TMDbConnectionError):
        client.verify_credentials()


def test_verify_credentials_timeout_raises() -> None:
    session = FakeSession([requests.Timeout(), requests.Timeout()])
    client, _ = _client(session)
    with pytest.raises(TMDbTimeoutError):
        client.verify_credentials()


def test_verify_credentials_malformed_response_raises() -> None:
    session = FakeSession([FakeResponse(200, _malformed=True)])
    client, _ = _client(session)
    with pytest.raises(TMDbResponseError):
        client.verify_credentials()


def test_verify_credentials_is_cached_on_repeat() -> None:
    session = FakeSession([FakeResponse(200, {"success": True})])
    client, cache = _client(session)
    client.verify_credentials()
    client.verify_credentials()
    assert len(session.calls) == 1
    assert len(cache.put_calls) == 1


def test_verify_credentials_never_leaks_token() -> None:
    session = FakeSession([FakeResponse(401)])
    client, _ = _client(session)
    with pytest.raises(TMDbAuthenticationError) as excinfo:
        client.verify_credentials()
    assert FAKE_TOKEN not in str(excinfo.value)


# --- token never leaks ---------------------------------------------------------


def test_token_never_appears_in_exception_messages() -> None:
    session = FakeSession([FakeResponse(401)])
    client, _ = _client(session)
    with pytest.raises(TMDbAuthenticationError) as excinfo:
        client.search_movie("Alien")
    assert FAKE_TOKEN not in str(excinfo.value)


def test_token_never_appears_in_cached_response_or_request_key() -> None:
    session = FakeSession([FakeResponse(200, {"results": []})])
    client, cache = _client(session)
    client.search_movie("Alien")
    assert FAKE_TOKEN not in cache.put_calls[0]["response_json"]
    assert FAKE_TOKEN not in cache.put_calls[0]["request_key"]


def test_token_is_sent_only_in_the_authorization_header() -> None:
    session = FakeSession([FakeResponse(200, {"results": []})])
    client, _ = _client(session)
    client.search_movie("Alien")
    assert session.calls[0]["headers"]["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert FAKE_TOKEN not in session.calls[0]["url"]
    assert FAKE_TOKEN not in str(session.calls[0]["params"])
