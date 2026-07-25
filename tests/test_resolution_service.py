"""Tests for resolution_service.py: search -> score -> decide -> persist
orchestration, threshold/gap boundaries, and manual review."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mams.config import AppConfig
from mams.db import connect, migrate
from mams.identification import CandidateType, Confidence, IdentificationCandidate
from mams.identification_repository import CandidateRecord
from mams.resolution_repository import get_active_assignment, list_attempts
from mams.resolution_service import (
    AUTO_RESOLVE_MIN_GAP,
    AUTO_RESOLVE_MIN_SCORE,
    MIN_PLAUSIBLE_SCORE,
    Decision,
    TMDbNotConfiguredError,
    _decide_episode_outcome,
    _decide_movie_outcome,
    build_provider,
    evaluate_candidate,
    reject_match_manually,
    select_match_manually,
)
from mams.scoring import MatchScore
from mams.tmdb import EpisodeResult, MovieResult, SeriesResult, TMDbClient, TMDbTimeoutError

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"


@pytest.fixture()
def connection(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "mams.db"
    migrate(db_path, REPO_MIGRATIONS_DIR)
    conn = connect(db_path)
    yield conn
    conn.close()


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _seed_media_file(connection: sqlite3.Connection, *, name: str = "Alien.mkv") -> int:
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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (library_id, f"/Volumes/movies/{name}", name, name, ".mkv", "/Volumes/movies", "movie_flat", 1234, scan_id, scan_id),
        )
    )


def _seed_candidate(connection: sqlite3.Connection, *, media_file_id: int, candidate_type: str = "MOVIE") -> int:
    return _lastrowid(
        connection.execute(
            "INSERT INTO identification_candidates (media_file_id, candidate_type, confidence, parser_version) "
            "VALUES (?, ?, 'HIGH', 1)",
            (media_file_id, candidate_type),
        )
    )


def _seed_movie(connection: sqlite3.Connection, *, name: str = "Alien.mkv") -> tuple[int, int]:
    media_file_id = _seed_media_file(connection, name=name)
    candidate_id = _seed_candidate(connection, media_file_id=media_file_id, candidate_type="MOVIE")
    return media_file_id, candidate_id


def _seed_episode(connection: sqlite3.Connection, *, name: str = "BB.mkv", candidate_type: str = "EPISODE") -> tuple[int, int]:
    media_file_id = _seed_media_file(connection, name=name)
    candidate_id = _seed_candidate(connection, media_file_id=media_file_id, candidate_type=candidate_type)
    return media_file_id, candidate_id


def _movie_candidate_record(media_file_id: int, *, candidate_id: int, title: str = "Alien", year: int | None = 1979) -> CandidateRecord:
    return CandidateRecord(
        id=candidate_id,
        media_file_id=media_file_id,
        category="movies",
        absolute_path=f"/Volumes/movies/x{media_file_id}.mkv",
        relative_path=f"x{media_file_id}.mkv",
        candidate_type="MOVIE",
        confidence="HIGH",
        parser_version=1,
        parsed_title=title,
        parsed_year=year,
        parsed_series_title=None,
        season_number=None,
        episode_number=None,
        episode_numbers=(),
        episode_title=None,
        edition=None,
        part_number=None,
        special_type=None,
        evidence=None,
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
    )


def _episode_candidate_record(
    media_file_id: int, *, candidate_id: int, series_title: str = "Breaking Bad", season: int = 1, episode: int = 1, candidate_type: str = "EPISODE"
) -> CandidateRecord:
    return CandidateRecord(
        id=candidate_id,
        media_file_id=media_file_id,
        category="tv",
        absolute_path=f"/Volumes/tv/x{media_file_id}.mkv",
        relative_path=f"x{media_file_id}.mkv",
        candidate_type=candidate_type,
        confidence="HIGH",
        parser_version=1,
        parsed_title=None,
        parsed_year=None,
        parsed_series_title=series_title,
        season_number=season,
        episode_number=episode,
        episode_numbers=(episode,),
        episode_title=None,
        edition=None,
        part_number=None,
        special_type=None,
        evidence=None,
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
    )


class FakeProvider:
    def __init__(
        self,
        *,
        movie_results: list[MovieResult] | None = None,
        movie_details: dict[int, MovieResult] | None = None,
        tv_results: list[SeriesResult] | None = None,
        episode_details: dict[tuple[int, int, int], EpisodeResult] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.movie_results = movie_results or []
        self.movie_details = movie_details or {}
        self.tv_results = tv_results or []
        self.episode_details = episode_details or {}
        self.error = error
        self.search_movie_calls = 0
        self.search_tv_calls = 0

    def search_movie(self, title: str, year: int | None = None) -> list[MovieResult]:
        self.search_movie_calls += 1
        if self.error is not None:
            raise self.error
        return self.movie_results

    def get_movie(self, provider_id: int) -> MovieResult | None:
        return self.movie_details.get(provider_id)

    def search_tv(self, series_title: str) -> list[SeriesResult]:
        self.search_tv_calls += 1
        if self.error is not None:
            raise self.error
        return self.tv_results

    def get_tv_series(self, provider_id: int) -> SeriesResult | None:
        return None

    def get_tv_episode(self, series_id: int, season_number: int, episode_number: int) -> EpisodeResult | None:
        return self.episode_details.get((series_id, season_number, episode_number))


def _movie_result(provider_id: int = 348, title: str = "Alien", year: int | None = 1979) -> MovieResult:
    return MovieResult(
        provider_id=provider_id, title=title, original_title=title, release_year=year,
        original_language="en", popularity=50.0,
    )


# --- integration: evaluate_candidate ---------------------------------------------


def test_auto_resolve_high_confidence_movie(connection: sqlite3.Connection) -> None:
    media_file_id, candidate_id = _seed_movie(connection)
    candidate = _movie_candidate_record(media_file_id, candidate_id=candidate_id)
    provider = FakeProvider(movie_results=[_movie_result()])

    attempt = evaluate_candidate(connection, provider, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.status == "RESOLVED"
    assignment = get_active_assignment(connection, media_file_id)
    assert assignment is not None
    assert assignment.assignment_method == "AUTO"
    assert assignment.confidence == "HIGH"


def test_review_required_ambiguous_movie(connection: sqlite3.Connection) -> None:
    media_file_id, candidate_id = _seed_movie(connection)
    candidate = _movie_candidate_record(media_file_id, candidate_id=candidate_id)
    # Two equally-good results (same title/year, different provider ids) --
    # zero score gap forces REVIEW_REQUIRED even though both score high.
    provider = FakeProvider(movie_results=[_movie_result(provider_id=1), _movie_result(provider_id=2)])

    attempt = evaluate_candidate(connection, provider, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.status == "REVIEW_REQUIRED"
    assert get_active_assignment(connection, media_file_id) is None


def test_no_match_movie(connection: sqlite3.Connection) -> None:
    media_file_id, candidate_id = _seed_movie(connection)
    candidate = _movie_candidate_record(media_file_id, candidate_id=candidate_id)
    provider = FakeProvider(movie_results=[])

    attempt = evaluate_candidate(connection, provider, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.status == "NO_MATCH"
    assert get_active_assignment(connection, media_file_id) is None
    count = connection.execute("SELECT COUNT(*) FROM external_identities").fetchone()[0]
    assert count == 0


def test_provider_failure_marks_attempt_failed(connection: sqlite3.Connection) -> None:
    media_file_id, candidate_id = _seed_movie(connection)
    candidate = _movie_candidate_record(media_file_id, candidate_id=candidate_id)
    provider = FakeProvider(error=TMDbTimeoutError("TMDb timed out"))

    attempt = evaluate_candidate(connection, provider, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.status == "FAILED"
    assert attempt.error_message == "TMDb timed out"
    assert get_active_assignment(connection, media_file_id) is None


def test_auto_resolve_episode(connection: sqlite3.Connection) -> None:
    media_file_id, candidate_id = _seed_episode(connection, name="BB.mkv")
    candidate = _episode_candidate_record(media_file_id, candidate_id=candidate_id)
    provider = FakeProvider(
        tv_results=[SeriesResult(provider_id=1396, title="Breaking Bad", original_title="Breaking Bad", first_air_year=2008, original_language="en", popularity=300.0)],
        episode_details={(1396, 1, 1): EpisodeResult(provider_id=62085, series_provider_id=1396, season_number=1, episode_number=1, episode_title="Pilot", air_date="2008-01-20", runtime_seconds=None)},
    )

    attempt = evaluate_candidate(connection, provider, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.status == "RESOLVED"
    assignment = get_active_assignment(connection, media_file_id)
    assert assignment is not None
    assert assignment.confidence == "HIGH"


def test_review_required_conflicting_series_result(connection: sqlite3.Connection) -> None:
    media_file_id, candidate_id = _seed_episode(connection, name="Office.mkv")
    candidate = _episode_candidate_record(media_file_id, candidate_id=candidate_id, series_title="The Office")
    provider = FakeProvider(
        tv_results=[
            SeriesResult(provider_id=1, title="The Office", original_title="The Office", first_air_year=2005, original_language="en", popularity=200.0),
            SeriesResult(provider_id=2, title="The Office", original_title="The Office", first_air_year=2001, original_language="en", popularity=150.0),
        ],
        episode_details={
            (1, 1, 1): EpisodeResult(provider_id=101, series_provider_id=1, season_number=1, episode_number=1, episode_title="Pilot", air_date="2005-03-24", runtime_seconds=None),
            (2, 1, 1): EpisodeResult(provider_id=201, series_provider_id=2, season_number=1, episode_number=1, episode_title="Downsize", air_date="2001-07-09", runtime_seconds=None),
        },
    )

    attempt = evaluate_candidate(connection, provider, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.status == "REVIEW_REQUIRED"
    assert len(attempt.matches) == 2
    assert get_active_assignment(connection, media_file_id) is None


def test_skipped_for_unknown_candidate_never_queries_provider(connection: sqlite3.Connection) -> None:
    media_file_id, candidate_id = _seed_movie(connection)
    candidate = _movie_candidate_record(media_file_id, candidate_id=candidate_id)
    candidate = CandidateRecord(**{**candidate.__dict__, "candidate_type": "UNKNOWN"})
    provider = FakeProvider(movie_results=[_movie_result()])

    attempt = evaluate_candidate(connection, provider, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.status == "SKIPPED"
    assert provider.search_movie_calls == 0
    assert provider.search_tv_calls == 0


def test_skipped_for_extra_candidate_never_queries_provider(connection: sqlite3.Connection) -> None:
    media_file_id, candidate_id = _seed_episode(connection, candidate_type="EXTRA")
    candidate = _episode_candidate_record(media_file_id, candidate_id=candidate_id, candidate_type="EXTRA")
    provider = FakeProvider(tv_results=[SeriesResult(provider_id=1, title="X", original_title="X", first_air_year=2000, original_language="en", popularity=1.0)])

    attempt = evaluate_candidate(connection, provider, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.status == "SKIPPED"
    assert provider.search_tv_calls == 0


def test_special_candidate_never_auto_resolves(connection: sqlite3.Connection) -> None:
    media_file_id, candidate_id = _seed_episode(connection, name="Special.mkv", candidate_type="SPECIAL")
    candidate = _episode_candidate_record(media_file_id, candidate_id=candidate_id, series_title="Breaking Bad", season=0, episode=1, candidate_type="SPECIAL")
    provider = FakeProvider(
        tv_results=[SeriesResult(provider_id=1396, title="Breaking Bad", original_title="Breaking Bad", first_air_year=2008, original_language="en", popularity=300.0)],
        episode_details={(1396, 0, 1): EpisodeResult(provider_id=99999, series_provider_id=1396, season_number=0, episode_number=1, episode_title="Special", air_date="2008-01-20", runtime_seconds=None)},
    )

    attempt = evaluate_candidate(connection, provider, candidate_record=candidate, local_runtime_seconds=None)

    assert attempt.status == "REVIEW_REQUIRED"
    assert get_active_assignment(connection, media_file_id) is None


def test_repeated_evaluation_does_not_duplicate_assignments(connection: sqlite3.Connection) -> None:
    media_file_id, candidate_id = _seed_movie(connection)
    candidate = _movie_candidate_record(media_file_id, candidate_id=candidate_id)
    provider = FakeProvider(movie_results=[_movie_result()])

    evaluate_candidate(connection, provider, candidate_record=candidate, local_runtime_seconds=None)
    evaluate_candidate(connection, provider, candidate_record=candidate, local_runtime_seconds=None)

    assignments = connection.execute("SELECT COUNT(*) FROM media_identity_assignments").fetchone()[0]
    assert assignments == 1
    attempts = list_attempts(connection, media_file_id=media_file_id)
    assert len(attempts) == 2  # attempt history still accumulates


def test_new_selected_identity_supersedes_prior_assignment(connection: sqlite3.Connection) -> None:
    media_file_id, candidate_id = _seed_movie(connection)
    candidate = _movie_candidate_record(media_file_id, candidate_id=candidate_id)

    first_provider = FakeProvider(movie_results=[_movie_result(provider_id=348, title="Alien", year=1979)])
    evaluate_candidate(connection, first_provider, candidate_record=candidate, local_runtime_seconds=None)
    first_assignment = get_active_assignment(connection, media_file_id)
    assert first_assignment is not None

    second_provider = FakeProvider(movie_results=[_movie_result(provider_id=679, title="Aliens", year=1986)])
    candidate_2 = _movie_candidate_record(media_file_id, candidate_id=candidate_id, title="Aliens", year=1986)
    evaluate_candidate(connection, second_provider, candidate_record=candidate_2, local_runtime_seconds=None)
    second_assignment = get_active_assignment(connection, media_file_id)

    assert second_assignment is not None
    assert second_assignment.id != first_assignment.id
    count = connection.execute("SELECT COUNT(*) FROM media_identity_assignments").fetchone()[0]
    assert count == 2


def test_historical_attempts_and_matches_are_retained(connection: sqlite3.Connection) -> None:
    media_file_id, candidate_id = _seed_movie(connection)
    candidate = _movie_candidate_record(media_file_id, candidate_id=candidate_id)

    evaluate_candidate(connection, FakeProvider(movie_results=[]), candidate_record=candidate, local_runtime_seconds=None)
    evaluate_candidate(connection, FakeProvider(movie_results=[_movie_result()]), candidate_record=candidate, local_runtime_seconds=None)

    attempts = list_attempts(connection, media_file_id=media_file_id)
    assert len(attempts) == 2
    assert {a.status for a in attempts} == {"NO_MATCH", "RESOLVED"}
    resolved = next(a for a in attempts if a.status == "RESOLVED")
    assert len(resolved.matches) == 1


# --- manual selection / rejection ------------------------------------------------


def test_select_match_manually_confirms_a_non_top_match(connection: sqlite3.Connection) -> None:
    media_file_id, candidate_id = _seed_movie(connection)
    candidate = _movie_candidate_record(media_file_id, candidate_id=candidate_id)
    provider = FakeProvider(movie_results=[_movie_result(provider_id=1), _movie_result(provider_id=2)])

    attempt = evaluate_candidate(connection, provider, candidate_record=candidate, local_runtime_seconds=None)
    assert attempt.status == "REVIEW_REQUIRED"
    second_match = attempt.matches[1]

    updated = select_match_manually(connection, attempt_id=attempt.id, match_id=second_match.id)

    assert updated.status == "RESOLVED"
    assignment = get_active_assignment(connection, media_file_id)
    assert assignment is not None
    assert assignment.assignment_method == "MANUAL"
    assert assignment.external_identity_id is not None


def test_reject_match_manually(connection: sqlite3.Connection) -> None:
    media_file_id, candidate_id = _seed_movie(connection)
    candidate = _movie_candidate_record(media_file_id, candidate_id=candidate_id)
    provider = FakeProvider(movie_results=[_movie_result(provider_id=1), _movie_result(provider_id=2)])
    attempt = evaluate_candidate(connection, provider, candidate_record=candidate, local_runtime_seconds=None)

    updated = reject_match_manually(connection, attempt_id=attempt.id)

    assert updated.status == "NO_MATCH"
    assert get_active_assignment(connection, media_file_id) is None
    assert len(updated.matches) == 2


# --- threshold / gap boundaries (direct, precise) --------------------------------


def _candidate(year: int | None = 1979) -> IdentificationCandidate:
    return IdentificationCandidate(
        media_file_id=1, candidate_type=CandidateType.MOVIE, confidence=Confidence.HIGH, parser_version=1,
        evidence={}, parsed_title="Alien", parsed_year=year,
    )


def _score(total: float, *, type_score: float = 1.0) -> MatchScore:
    return MatchScore(total_score=total, components={"type_score": type_score}, reasons=())


def test_movie_auto_resolve_at_exact_threshold_boundary() -> None:
    ranked = [(_movie_result(), _score(AUTO_RESOLVE_MIN_SCORE)), (_movie_result(provider_id=2), _score(AUTO_RESOLVE_MIN_SCORE - AUTO_RESOLVE_MIN_GAP))]
    decision = _decide_movie_outcome(_candidate(), ranked)
    assert decision.status == "RESOLVED"


def test_movie_just_below_score_threshold_requires_review() -> None:
    ranked = [(_movie_result(), _score(AUTO_RESOLVE_MIN_SCORE - 0.01)), (_movie_result(provider_id=2), _score(0.0))]
    decision = _decide_movie_outcome(_candidate(), ranked)
    assert decision.status == "REVIEW_REQUIRED"


def test_movie_just_below_gap_threshold_requires_review() -> None:
    ranked = [(_movie_result(), _score(0.95)), (_movie_result(provider_id=2), _score(0.95 - (AUTO_RESOLVE_MIN_GAP - 0.01)))]
    decision = _decide_movie_outcome(_candidate(), ranked)
    assert decision.status == "REVIEW_REQUIRED"


def test_movie_below_no_match_threshold() -> None:
    ranked = [(_movie_result(), _score(MIN_PLAUSIBLE_SCORE - 0.01))]
    decision = _decide_movie_outcome(_candidate(), ranked)
    assert decision.status == "NO_MATCH"


def test_movie_at_no_match_threshold_boundary_is_plausible() -> None:
    ranked = [(_movie_result(), _score(MIN_PLAUSIBLE_SCORE))]
    decision = _decide_movie_outcome(_candidate(), ranked)
    assert decision.status != "NO_MATCH"


def test_movie_without_local_year_never_auto_resolves() -> None:
    ranked = [(_movie_result(), _score(1.0)), (_movie_result(provider_id=2), _score(0.0))]
    decision = _decide_movie_outcome(_candidate(year=None), ranked)
    assert decision.status == "REVIEW_REQUIRED"


def test_movie_type_conflict_never_auto_resolves() -> None:
    ranked = [(_movie_result(), _score(1.0, type_score=0.3)), (_movie_result(provider_id=2), _score(0.0))]
    decision = _decide_movie_outcome(_candidate(), ranked)
    assert decision.status == "REVIEW_REQUIRED"


def _episode_score(total: float, *, season_score: float = 1.0, episode_score: float = 1.0, type_score: float = 1.0) -> MatchScore:
    return MatchScore(total_score=total, components={"season_score": season_score, "episode_score": episode_score, "type_score": type_score}, reasons=())


def _episode_candidate() -> IdentificationCandidate:
    return IdentificationCandidate(
        media_file_id=1, candidate_type=CandidateType.EPISODE, confidence=Confidence.HIGH, parser_version=1,
        evidence={}, parsed_series_title="Breaking Bad", season_number=1, episode_number=1,
    )


def _episode_pair(provider_id: int = 1396) -> tuple[SeriesResult, EpisodeResult]:
    series = SeriesResult(provider_id=provider_id, title="Breaking Bad", original_title="Breaking Bad", first_air_year=2008, original_language="en", popularity=300.0)
    episode = EpisodeResult(provider_id=provider_id * 10, series_provider_id=provider_id, season_number=1, episode_number=1, episode_title="Pilot", air_date="2008-01-20", runtime_seconds=None)
    return series, episode


def test_episode_auto_resolve_requires_exact_season_and_episode() -> None:
    series, episode = _episode_pair()
    ranked = [(series, episode, _episode_score(1.0, season_score=0.0))]
    decision = _decide_episode_outcome(_episode_candidate(), ranked)
    assert decision.status == "REVIEW_REQUIRED"


def test_episode_auto_resolve_at_boundary() -> None:
    series, episode = _episode_pair()
    other_series, other_episode = _episode_pair(provider_id=2)
    ranked = [(series, episode, _episode_score(AUTO_RESOLVE_MIN_SCORE)), (other_series, other_episode, _episode_score(AUTO_RESOLVE_MIN_SCORE - AUTO_RESOLVE_MIN_GAP))]
    decision = _decide_episode_outcome(_episode_candidate(), ranked)
    assert decision.status == "RESOLVED"


def test_decision_is_a_frozen_dataclass_with_reasons() -> None:
    decision = Decision(status="NO_MATCH", confidence=None, reasons=("x",))
    assert decision.reasons == ("x",)


# --- build_provider ---------------------------------------------------------------


def test_build_provider_raises_when_no_token_configured(connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMDB_API_TOKEN", raising=False)
    config = AppConfig(raw={"tmdb": {"token_env_var": "TMDB_API_TOKEN"}})
    with pytest.raises(TMDbNotConfiguredError):
        build_provider(config, connection)


def test_build_provider_returns_a_client_when_token_configured(connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMDB_API_TOKEN", "test-token")
    config = AppConfig(raw={"tmdb": {"token_env_var": "TMDB_API_TOKEN"}})
    provider = build_provider(config, connection)
    assert isinstance(provider, TMDbClient)
