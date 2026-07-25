"""Tests for AppConfig's TMDb/ingest properties (config.py)."""

from __future__ import annotations

import pytest

from mams.config import AppConfig


def test_tmdb_token_reads_named_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMDB_API_TOKEN", "secret-value")
    config = AppConfig(raw={"tmdb": {"token_env_var": "TMDB_API_TOKEN"}})
    assert config.tmdb_token == "secret-value"


def test_tmdb_token_is_none_when_env_var_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMDB_API_TOKEN", raising=False)
    config = AppConfig(raw={"tmdb": {"token_env_var": "TMDB_API_TOKEN"}})
    assert config.tmdb_token is None


def test_tmdb_token_is_none_when_section_missing() -> None:
    config = AppConfig(raw={})
    assert config.tmdb_token is None


def test_tmdb_cache_ttl_seconds_default() -> None:
    config = AppConfig(raw={})
    assert config.tmdb_cache_ttl_seconds == 7 * 24 * 3600


def test_tmdb_cache_ttl_seconds_configured() -> None:
    config = AppConfig(raw={"tmdb": {"cache_ttl_seconds": 3600}})
    assert config.tmdb_cache_ttl_seconds == 3600


def test_ingest_incoming_roots_empty_by_default() -> None:
    config = AppConfig(raw={})
    assert config.ingest_incoming_roots == []


def test_incoming_categories_single_root_uses_incoming_key() -> None:
    config = AppConfig(raw={"ingest": {"incoming_roots": ["/Media/Incoming"]}})
    assert config.incoming_categories == {"incoming": "/Media/Incoming"}


def test_incoming_categories_multiple_roots_are_indexed() -> None:
    config = AppConfig(raw={"ingest": {"incoming_roots": ["/A", "/B"]}})
    assert config.incoming_categories == {"incoming_1": "/A", "incoming_2": "/B"}


def test_incoming_categories_empty_when_no_roots_configured() -> None:
    config = AppConfig(raw={})
    assert config.incoming_categories == {}


def test_ingest_destination_categories_defaults() -> None:
    config = AppConfig(raw={})
    assert config.ingest_destination_categories == {
        "movie": "movies",
        "tv": "tv",
        "kids_movie": "kids_movies",
        "kids_tv": "kids_shows",
    }


def test_ingest_destination_categories_configured() -> None:
    config = AppConfig(raw={"ingest": {"movie_destination_category": "films"}})
    assert config.ingest_destination_categories["movie"] == "films"
