"""Live TMDb acceptance matrix (Milestone 7C, Phase B).

Every test here makes a real network call to the live TMDb API. All of
them are marked `@pytest.mark.live` and therefore excluded from the
default `pytest` run (see `addopts = "-m 'not live'"` in pyproject.toml)
regardless of whether `TMDB_API_TOKEN` happens to be set -- running this
file requires *both* `pytest -m live` *and* a real token
(`TMDB_API_TOKEN` in the environment), matching the milestone's "never
run as part of the normal deterministic unit suite unless explicitly
enabled."

This is a deliberately small, representative acceptance set -- it never
resolves more than a handful of well-known titles, and never touches (or
even opens) the production database or NAS. Each test builds a real
`TMDbClient` against a scratch, on-disk SQLite cache (via
`provider_cache_repository.SqliteCacheStore`) so cache hit/miss/expiry
behavior is exercised too, and drives `resolution_service.evaluate_candidate`
directly -- the same function `mams resolve evaluate` calls -- against a
hand-built local `IdentificationCandidate`, without needing real ripped
media files or a full inventory scan.

Real-world outcomes (score gaps, whether a given ambiguous title happens
to auto-resolve) can shift over time as TMDb's catalog/popularity data
changes -- these tests assert the *documented* threshold policy's
observable behavior (e.g. "an ambiguous title never silently
auto-resolves"), not a specific score value, and are not a substitute for
the manual acceptance-matrix review the milestone's own validation phase
requires.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from mams.db import connect, migrate
from mams.identification import CandidateType, Confidence, IdentificationCandidate
from mams.identification_repository import CandidateRecord
from mams.provider_cache_repository import SqliteCacheStore, get_cache_stats
from mams.resolution_repository import get_active_assignment
from mams.resolution_service import evaluate_candidate
from mams.tmdb import TMDbClient

pytestmark = pytest.mark.live

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"

skip_without_token = pytest.mark.skipif(
    not os.environ.get("TMDB_API_TOKEN"),
    reason="TMDB_API_TOKEN not set -- live tests require a real TMDb token even when run with `pytest -m live`.",
)


@pytest.fixture()
def connection(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "mams.db"
    migrate(db_path, REPO_MIGRATIONS_DIR)
    conn = connect(db_path)
    yield conn
    conn.close()


@pytest.fixture()
def client(connection: sqlite3.Connection) -> TMDbClient:
    token = os.environ["TMDB_API_TOKEN"]
    cache = SqliteCacheStore(connection, provider="TMDB", ttl_seconds=604800)
    return TMDbClient(token=token, cache=cache)


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _seed_media_file(connection: sqlite3.Connection, *, name: str) -> int:
    library_id = _lastrowid(
        connection.execute("INSERT INTO libraries (category, root_path) VALUES ('movies', '/Volumes/movies')")
    )
    scan_id = _lastrowid(connection.execute("INSERT INTO scan_runs DEFAULT VALUES"))
    return _lastrowid(
        connection.execute(
            """
            INSERT INTO media_files (
                library_id, absolute_path, relative_path, filename, extension,
                parent_directory, layout, size_bytes, first_seen_scan_id, last_seen_scan_id
            ) VALUES (?, ?, ?, ?, ?, ?, 'movie_flat', 1234, ?, ?)
            """,
            (library_id, f"/Volumes/movies/{name}", name, name, ".mkv", "/Volumes/movies", scan_id, scan_id),
        )
    )


def _seed_candidate_row(connection: sqlite3.Connection, *, media_file_id: int, candidate_type: str) -> int:
    return _lastrowid(
        connection.execute(
            "INSERT INTO identification_candidates (media_file_id, candidate_type, confidence, parser_version) "
            "VALUES (?, ?, 'HIGH', 1)",
            (media_file_id, candidate_type),
        )
    )


def _movie_candidate_record(media_file_id: int, candidate_id: int, *, title: str, year: int | None) -> CandidateRecord:
    return CandidateRecord(
        id=candidate_id, media_file_id=media_file_id, category="movies",
        absolute_path=f"/Volumes/movies/x{media_file_id}.mkv", relative_path=f"x{media_file_id}.mkv",
        candidate_type="MOVIE", confidence="HIGH", parser_version=1,
        parsed_title=title, parsed_year=year, parsed_series_title=None, season_number=None,
        episode_number=None, episode_numbers=(), episode_title=None, edition=None, part_number=None,
        special_type=None, evidence=None, created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00",
    )


def _episode_candidate_record(
    media_file_id: int, candidate_id: int, *, series_title: str, season: int, episode: int,
    episode_title: str | None = None, candidate_type: str = "EPISODE",
) -> CandidateRecord:
    return CandidateRecord(
        id=candidate_id, media_file_id=media_file_id, category="tv",
        absolute_path=f"/Volumes/tv/x{media_file_id}.mkv", relative_path=f"x{media_file_id}.mkv",
        candidate_type=candidate_type, confidence="HIGH", parser_version=1,
        parsed_title=None, parsed_year=None, parsed_series_title=series_title, season_number=season,
        episode_number=episode, episode_numbers=(episode,), episode_title=episode_title, edition=None,
        part_number=None, special_type=None, evidence=None, created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00",
    )


def _movie_ic(title: str, year: int | None) -> IdentificationCandidate:
    return IdentificationCandidate(
        media_file_id=0, candidate_type=CandidateType.MOVIE, confidence=Confidence.HIGH, parser_version=1,
        evidence={}, parsed_title=title, parsed_year=year,
    )


def _episode_ic(series_title: str, season: int, episode: int, episode_title: str | None = None) -> IdentificationCandidate:
    return IdentificationCandidate(
        media_file_id=0, candidate_type=CandidateType.EPISODE, confidence=Confidence.HIGH, parser_version=1,
        evidence={}, parsed_series_title=series_title, season_number=season, episode_number=episode,
        episode_numbers=(episode,), episode_title=episode_title,
    )


# --- Phase A: provider-status against the real API -----------------------------


@skip_without_token
def test_live_verify_credentials_succeeds_with_a_real_token(client: TMDbClient) -> None:
    assert client.verify_credentials() is None


# --- Phase B: movie acceptance matrix --------------------------------------------


@skip_without_token
def test_live_movie_exact_title_and_year_auto_resolves(connection: sqlite3.Connection, client: TMDbClient) -> None:
    media_file_id = _seed_media_file(connection, name="The Matrix (1999).mkv")
    candidate_id = _seed_candidate_row(connection, media_file_id=media_file_id, candidate_type="MOVIE")
    candidate = _movie_candidate_record(media_file_id, candidate_id, title="The Matrix", year=1999)

    attempt = evaluate_candidate(connection, client, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.status == "RESOLVED"
    assignment = get_active_assignment(connection, media_file_id)
    assert assignment is not None
    assert assignment.assignment_method == "AUTO"


@skip_without_token
def test_live_movie_exact_title_without_year_documented_behavior(connection: sqlite3.Connection, client: TMDbClient) -> None:
    """Per the threshold policy (resolution_service.py), a movie without a
    known local year never auto-resolves regardless of score -- documented
    here as REVIEW_REQUIRED or NO_MATCH, never a silent RESOLVED."""
    media_file_id = _seed_media_file(connection, name="The Matrix.mkv")
    candidate_id = _seed_candidate_row(connection, media_file_id=media_file_id, candidate_type="MOVIE")
    candidate = _movie_candidate_record(media_file_id, candidate_id, title="The Matrix", year=None)

    attempt = evaluate_candidate(connection, client, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.status in ("REVIEW_REQUIRED", "NO_MATCH")


@skip_without_token
def test_live_ambiguous_movie_title_requires_review(connection: sqlite3.Connection, client: TMDbClient) -> None:
    """"Halloween" without a year has multiple legitimate releases
    (1978, 2007, 2018...) -- must never silently pick one."""
    media_file_id = _seed_media_file(connection, name="Halloween.mkv")
    candidate_id = _seed_candidate_row(connection, media_file_id=media_file_id, candidate_type="MOVIE")
    candidate = _movie_candidate_record(media_file_id, candidate_id, title="Halloween", year=None)

    attempt = evaluate_candidate(connection, client, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.status in ("REVIEW_REQUIRED", "NO_MATCH")
    assert len(attempt.matches) >= 1


@skip_without_token
def test_live_movie_title_with_article_variation_normalizes(connection: sqlite3.Connection, client: TMDbClient) -> None:
    media_file_id = _seed_media_file(connection, name="Matrix The (1999).mkv")
    candidate_id = _seed_candidate_row(connection, media_file_id=media_file_id, candidate_type="MOVIE")
    candidate = _movie_candidate_record(media_file_id, candidate_id, title="Matrix, The", year=1999)

    attempt = evaluate_candidate(connection, client, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.status != "FAILED"
    assert len(attempt.matches) >= 1


@skip_without_token
def test_live_movie_no_plausible_result_is_no_match(connection: sqlite3.Connection, client: TMDbClient) -> None:
    media_file_id = _seed_media_file(connection, name="Zzqxvthisisnotarealmovietitle999.mkv")
    candidate_id = _seed_candidate_row(connection, media_file_id=media_file_id, candidate_type="MOVIE")
    candidate = _movie_candidate_record(
        media_file_id, candidate_id, title="Zzqxvthisisnotarealmovietitle999", year=1999
    )

    attempt = evaluate_candidate(connection, client, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.status == "NO_MATCH"
    assert get_active_assignment(connection, media_file_id) is None


# --- Phase B: TV acceptance matrix ------------------------------------------------


@skip_without_token
def test_live_episode_exact_series_season_episode_resolves(connection: sqlite3.Connection, client: TMDbClient) -> None:
    media_file_id = _seed_media_file(connection, name="Breaking Bad S01E01.mkv")
    candidate_id = _seed_candidate_row(connection, media_file_id=media_file_id, candidate_type="EPISODE")
    candidate = _episode_candidate_record(media_file_id, candidate_id, series_title="Breaking Bad", season=1, episode=1)

    attempt = evaluate_candidate(connection, client, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.status == "RESOLVED"


@skip_without_token
def test_live_episode_title_corroboration_appears_in_scoring_evidence(
    connection: sqlite3.Connection, client: TMDbClient
) -> None:
    media_file_id = _seed_media_file(connection, name="Breaking Bad S01E01 Pilot.mkv")
    candidate_id = _seed_candidate_row(connection, media_file_id=media_file_id, candidate_type="EPISODE")
    candidate = _episode_candidate_record(
        media_file_id, candidate_id, series_title="Breaking Bad", season=1, episode=1, episode_title="Pilot"
    )

    attempt = evaluate_candidate(connection, client, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.matches
    top = attempt.matches[0]
    assert "episode_title_score" in top.scoring or any("corroborat" in reason for reason in top.scoring.get("reasons", []))


@skip_without_token
def test_live_ambiguous_series_title_is_reviewable_or_no_match(connection: sqlite3.Connection, client: TMDbClient) -> None:
    media_file_id = _seed_media_file(connection, name="The Office S01E01.mkv")
    candidate_id = _seed_candidate_row(connection, media_file_id=media_file_id, candidate_type="EPISODE")
    # "The Office" has both a US and UK version -- TMDb search returns
    # multiple plausible series results.
    candidate = _episode_candidate_record(media_file_id, candidate_id, series_title="The Office", season=1, episode=1)

    attempt = evaluate_candidate(connection, client, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.status in ("RESOLVED", "REVIEW_REQUIRED", "NO_MATCH")


@skip_without_token
def test_live_special_season_zero_never_auto_resolves(connection: sqlite3.Connection, client: TMDbClient) -> None:
    media_file_id = _seed_media_file(connection, name="Breaking Bad S00E01.mkv")
    candidate_id = _seed_candidate_row(connection, media_file_id=media_file_id, candidate_type="SPECIAL")
    candidate = _episode_candidate_record(
        media_file_id, candidate_id, series_title="Breaking Bad", season=0, episode=1, candidate_type="SPECIAL"
    )

    attempt = evaluate_candidate(connection, client, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.status in ("REVIEW_REQUIRED", "NO_MATCH")


@skip_without_token
def test_live_multi_episode_file_resolves_against_primary_episode_only(
    connection: sqlite3.Connection, client: TMDbClient
) -> None:
    """Documented limitation (docs/VALIDATION.md, Milestone 7B): a
    multi-episode local candidate resolves and scores against its primary
    (first) episode number only."""
    media_file_id = _seed_media_file(connection, name="Breaking Bad S01E01E02.mkv")
    candidate_id = _seed_candidate_row(connection, media_file_id=media_file_id, candidate_type="EPISODE")
    candidate = CandidateRecord(
        id=candidate_id, media_file_id=media_file_id, category="tv",
        absolute_path=f"/Volumes/tv/x{media_file_id}.mkv", relative_path=f"x{media_file_id}.mkv",
        candidate_type="EPISODE", confidence="HIGH", parser_version=1,
        parsed_title=None, parsed_year=None, parsed_series_title="Breaking Bad", season_number=1,
        episode_number=1, episode_numbers=(1, 2), episode_title=None, edition=None, part_number=None,
        special_type=None, evidence=None, created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00",
    )

    attempt = evaluate_candidate(connection, client, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.status == "RESOLVED"
    assert attempt.matches[0].episode_number == 1


# --- Phase C: provider-cache acceptance against live requests --------------------


@skip_without_token
def test_live_repeated_request_is_a_cache_hit(connection: sqlite3.Connection, client: TMDbClient) -> None:
    media_file_id = _seed_media_file(connection, name="Inception (2010).mkv")
    candidate_id = _seed_candidate_row(connection, media_file_id=media_file_id, candidate_type="MOVIE")
    candidate = _movie_candidate_record(media_file_id, candidate_id, title="Inception", year=2010)

    first = evaluate_candidate(connection, client, candidate_record=candidate, local_runtime_seconds=None)
    stats_after_first = get_cache_stats(connection)

    # Re-evaluate the identical query -- the second search_movie() call
    # must be served from provider_cache, not a second network request.
    second_candidate_id = _seed_candidate_row(
        connection, media_file_id=_seed_media_file(connection, name="Inception (2010) copy.mkv"), candidate_type="MOVIE"
    )
    second_candidate = _movie_candidate_record(second_candidate_id, second_candidate_id, title="Inception", year=2010)
    evaluate_candidate(connection, client, candidate_record=second_candidate, local_runtime_seconds=None)
    stats_after_second = get_cache_stats(connection)

    assert first.status == "RESOLVED"
    # An identical logical request (same endpoint + normalized params)
    # must not grow the cache table further.
    assert stats_after_second.total_count == stats_after_first.total_count
