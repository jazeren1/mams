"""TMDb (The Movie Database) client: a `MediaProvider` implementation that
searches and looks up movies, TV series, and TV episodes.

Normalizes TMDb's raw JSON responses into small, stable dataclasses before
they reach any other module -- scoring, resolution, and CLI code never see
a raw TMDb response (see docs/DATABASE.md, "external_identities", for why
that boundary matters). HTTP concerns (auth, timeouts, retries, rate
limits, caching) live entirely here; this module contains no SQL and no
ingest-planning logic.

The cache itself is a separate concern, injected via the `CacheStore`
protocol -- `provider_cache_repository.SqliteCacheStore` is the concrete,
SQL-backed implementation used in production; tests inject an in-memory
fake so no test in this codebase needs a real network call.

Authentication uses a TMDb v4 "Read Access Token" as a bearer token (see
`AppConfig.tmdb_token` / `config.yaml`'s `tmdb.token_env_var`), which also
works against the v3 endpoints used here. The token is passed only in the
`Authorization` request header -- it is never logged, never included in
an exception message, and never written to the cache (TMDb's response
bodies do not echo the caller's credentials).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import requests

DEFAULT_BASE_URL = "https://api.themoviedb.org/3"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_TIMEOUT_ATTEMPTS = 2


class TMDbError(Exception):
    """Base class for all TMDb client errors."""


class TMDbAuthenticationError(TMDbError):
    """The configured token was rejected (HTTP 401/403)."""


class TMDbRateLimitError(TMDbError):
    """TMDb responded with HTTP 429."""


class TMDbTimeoutError(TMDbError):
    """The request did not complete within the configured timeout, after
    the retry policy (`max_timeout_attempts`) was exhausted."""


class TMDbConnectionError(TMDbError):
    """A network-level failure prevented the request from completing."""


class TMDbResponseError(TMDbError):
    """TMDb returned an unexpected status code or a malformed body."""


@dataclass(frozen=True)
class MovieResult:
    provider_id: int
    title: str
    original_title: str | None
    release_year: int | None
    original_language: str | None
    popularity: float | None
    runtime_seconds: int | None = None


@dataclass(frozen=True)
class SeriesResult:
    provider_id: int
    title: str
    original_title: str | None
    first_air_year: int | None
    original_language: str | None
    popularity: float | None


@dataclass(frozen=True)
class EpisodeResult:
    provider_id: int
    series_provider_id: int
    season_number: int
    episode_number: int
    episode_title: str | None
    air_date: str | None
    runtime_seconds: int | None


class MediaProvider(Protocol):
    """Provider abstraction `resolution_service` depends on. A fake
    implementing this protocol is used throughout the test suite so
    scoring/resolution tests never make a real network call, mirroring
    `mediainfo.MetadataProvider`."""

    def search_movie(self, title: str, year: int | None = None) -> list[MovieResult]: ...

    def get_movie(self, provider_id: int) -> MovieResult | None: ...

    def search_tv(self, series_title: str) -> list[SeriesResult]: ...

    def get_tv_series(self, provider_id: int) -> SeriesResult | None: ...

    def get_tv_episode(self, series_id: int, season_number: int, episode_number: int) -> EpisodeResult | None: ...


class CacheStore(Protocol):
    """Cache abstraction `TMDbClient` depends on.
    `provider_cache_repository.SqliteCacheStore` is the concrete,
    SQL-backed implementation; this module never imports it or performs
    SQL directly."""

    def get(self, *, request_key: str) -> str | None: ...

    def put(self, *, request_key: str, endpoint: str, response_json: str, status_code: int) -> None: ...


def build_cache_key(endpoint: str, params: dict[str, str | int | None]) -> str:
    """A deterministic cache key for one logical request: the endpoint
    plus its parameters, `None`-filtered and sorted, so the same logical
    request always maps to the same key regardless of parameter order."""
    normalized = {k: v for k, v in params.items() if v is not None}
    return f"{endpoint}?{json.dumps(normalized, sort_keys=True, separators=(',', ':'))}"


def _year_from_date(date_str: object) -> int | None:
    if not isinstance(date_str, str) or len(date_str) < 4:
        return None
    try:
        return int(date_str[:4])
    except ValueError:
        return None


def _runtime_seconds(minutes: object) -> int | None:
    if not isinstance(minutes, (int, float)) or minutes <= 0:
        return None
    return int(minutes * 60)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


class TMDbClient:
    """`MediaProvider` implementation backed by the TMDb v3 HTTP API,
    authenticated with a v4 bearer read-access token."""

    def __init__(
        self,
        *,
        token: str,
        cache: CacheStore,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_timeout_attempts: int = DEFAULT_MAX_TIMEOUT_ATTEMPTS,
        session: requests.Session | None = None,
    ) -> None:
        self._token = token
        self._cache = cache
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_timeout_attempts = max(1, max_timeout_attempts)
        self._session = session or requests.Session()

    def _get(self, endpoint: str, params: dict[str, str | int | None]) -> dict[str, Any] | None:
        """GET one TMDb endpoint, serving from cache when possible.
        Returns `None` for a 404 (the requested id genuinely doesn't
        exist); raises a `TMDbError` subclass for every other failure."""
        request_key = build_cache_key(endpoint, params)
        cached = self._cache.get(request_key=request_key)
        if cached is not None:
            payload: Any = json.loads(cached)
            return payload if isinstance(payload, dict) else None

        response: requests.Response | None = None
        last_timeout: requests.Timeout | None = None
        for _ in range(self._max_timeout_attempts):
            try:
                response = self._session.get(
                    f"{self._base_url}{endpoint}",
                    params={k: v for k, v in params.items() if v is not None},
                    headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
                    timeout=self._timeout_seconds,
                )
                break
            except requests.Timeout as exc:
                last_timeout = exc
                response = None
                continue
            except requests.RequestException as exc:
                raise TMDbConnectionError(f"TMDb request to {endpoint} failed") from exc
        if response is None:
            raise TMDbTimeoutError(
                f"TMDb request to {endpoint} timed out after {self._max_timeout_attempts} attempt(s)"
            ) from last_timeout

        if response.status_code == 404:
            return None
        if response.status_code in (401, 403):
            raise TMDbAuthenticationError("TMDb rejected the configured API token")
        if response.status_code == 429:
            raise TMDbRateLimitError("TMDb rate limit exceeded")
        if response.status_code != 200:
            raise TMDbResponseError(f"TMDb returned unexpected status {response.status_code} for {endpoint}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise TMDbResponseError(f"TMDb returned a malformed response body for {endpoint}") from exc
        if not isinstance(payload, dict):
            raise TMDbResponseError(f"TMDb returned an unexpected response shape for {endpoint}")

        self._cache.put(
            request_key=request_key,
            endpoint=endpoint,
            response_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            status_code=response.status_code,
        )
        return payload

    def verify_credentials(self) -> None:
        """Validate the configured token with one minimal, cached, harmless
        request against TMDb's `/authentication` endpoint -- it takes no
        parameters and returns no meaningful data, existing solely to
        confirm a token is accepted. Reuses `_get`'s existing error
        handling: raises `TMDbAuthenticationError`/`TMDbRateLimitError`/
        `TMDbTimeoutError`/`TMDbConnectionError`/`TMDbResponseError` for
        the corresponding failure, or returns `None` on success. Subject to
        the same cache as every other request (see `build_cache_key`), so
        repeated diagnostic runs within the TTL cost no extra network call."""
        self._get("/authentication", {})

    def search_movie(self, title: str, year: int | None = None) -> list[MovieResult]:
        payload = self._get("/search/movie", {"query": title, "year": year})
        results = (payload or {}).get("results")
        if not isinstance(results, list):
            raise TMDbResponseError("TMDb movie search response missing 'results' list")
        try:
            return [self._normalize_movie_search_item(item) for item in results if isinstance(item, dict)]
        except (KeyError, TypeError, ValueError) as exc:
            raise TMDbResponseError("TMDb movie search response contained a malformed result") from exc

    def get_movie(self, provider_id: int) -> MovieResult | None:
        payload = self._get(f"/movie/{provider_id}", {})
        if payload is None:
            return None
        try:
            return self._normalize_movie_detail(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise TMDbResponseError(f"TMDb movie detail response for {provider_id} was malformed") from exc

    def search_tv(self, series_title: str) -> list[SeriesResult]:
        payload = self._get("/search/tv", {"query": series_title})
        results = (payload or {}).get("results")
        if not isinstance(results, list):
            raise TMDbResponseError("TMDb TV search response missing 'results' list")
        try:
            return [self._normalize_series_search_item(item) for item in results if isinstance(item, dict)]
        except (KeyError, TypeError, ValueError) as exc:
            raise TMDbResponseError("TMDb TV search response contained a malformed result") from exc

    def get_tv_series(self, provider_id: int) -> SeriesResult | None:
        payload = self._get(f"/tv/{provider_id}", {})
        if payload is None:
            return None
        try:
            return self._normalize_series_search_item(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise TMDbResponseError(f"TMDb series detail response for {provider_id} was malformed") from exc

    def get_tv_episode(self, series_id: int, season_number: int, episode_number: int) -> EpisodeResult | None:
        payload = self._get(f"/tv/{series_id}/season/{season_number}/episode/{episode_number}", {})
        if payload is None:
            return None
        try:
            return self._normalize_episode_detail(series_id, payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise TMDbResponseError(
                f"TMDb episode detail response for series {series_id} S{season_number:02d}E{episode_number:02d} "
                "was malformed"
            ) from exc

    @staticmethod
    def _normalize_movie_search_item(item: dict[str, Any]) -> MovieResult:
        return MovieResult(
            provider_id=int(item["id"]),
            title=str(item.get("title") or ""),
            original_title=_optional_str(item.get("original_title")),
            release_year=_year_from_date(item.get("release_date")),
            original_language=_optional_str(item.get("original_language")),
            popularity=_optional_float(item.get("popularity")),
        )

    def _normalize_movie_detail(self, payload: dict[str, Any]) -> MovieResult:
        base = self._normalize_movie_search_item(payload)
        return MovieResult(
            provider_id=base.provider_id,
            title=base.title,
            original_title=base.original_title,
            release_year=base.release_year,
            original_language=base.original_language,
            popularity=base.popularity,
            runtime_seconds=_runtime_seconds(payload.get("runtime")),
        )

    @staticmethod
    def _normalize_series_search_item(item: dict[str, Any]) -> SeriesResult:
        return SeriesResult(
            provider_id=int(item["id"]),
            title=str(item.get("name") or ""),
            original_title=_optional_str(item.get("original_name")),
            first_air_year=_year_from_date(item.get("first_air_date")),
            original_language=_optional_str(item.get("original_language")),
            popularity=_optional_float(item.get("popularity")),
        )

    @staticmethod
    def _normalize_episode_detail(series_id: int, payload: dict[str, Any]) -> EpisodeResult:
        return EpisodeResult(
            provider_id=int(payload["id"]),
            series_provider_id=series_id,
            season_number=int(payload["season_number"]),
            episode_number=int(payload["episode_number"]),
            episode_title=_optional_str(payload.get("name")),
            air_date=_optional_str(payload.get("air_date")),
            runtime_seconds=_runtime_seconds(payload.get("runtime")),
        )
