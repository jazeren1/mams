"""Tests for scoring.py: deterministic movie/episode match scoring."""

from __future__ import annotations

from mams.identification import CandidateType, Confidence, IdentificationCandidate
from mams.scoring import score_episode_match, score_movie_match
from mams.tmdb import EpisodeResult, MovieResult


def _movie_candidate(
    *,
    title: str | None = "Alien",
    year: int | None = 1979,
    candidate_type: CandidateType = CandidateType.MOVIE,
) -> IdentificationCandidate:
    return IdentificationCandidate(
        media_file_id=1,
        candidate_type=candidate_type,
        confidence=Confidence.HIGH,
        parser_version=1,
        evidence={},
        parsed_title=title,
        parsed_year=year,
    )


def _movie_result(
    *,
    title: str = "Alien",
    original_title: str | None = "unset",
    release_year: int | None = 1979,
    runtime_seconds: int | None = None,
) -> MovieResult:
    return MovieResult(
        provider_id=348,
        title=title,
        original_title=title if original_title == "unset" else original_title,
        release_year=release_year,
        original_language="en",
        popularity=50.0,
        runtime_seconds=runtime_seconds,
    )


def _episode_candidate(
    *,
    series_title: str | None = "Breaking Bad",
    season: int | None = 1,
    episode: int | None = 1,
    episode_title: str | None = None,
    candidate_type: CandidateType = CandidateType.EPISODE,
) -> IdentificationCandidate:
    return IdentificationCandidate(
        media_file_id=1,
        candidate_type=candidate_type,
        confidence=Confidence.HIGH,
        parser_version=1,
        evidence={},
        parsed_series_title=series_title,
        season_number=season,
        episode_number=episode,
        episode_title=episode_title,
    )


def _episode_result(
    *,
    season: int = 1,
    episode: int = 1,
    episode_title: str | None = "Pilot",
    runtime_seconds: int | None = None,
) -> EpisodeResult:
    return EpisodeResult(
        provider_id=62085,
        series_provider_id=1396,
        season_number=season,
        episode_number=episode,
        episode_title=episode_title,
        air_date="2008-01-20",
        runtime_seconds=runtime_seconds,
    )


# --- movie scoring --------------------------------------------------------------


def test_exact_title_and_year_scores_highest() -> None:
    score = score_movie_match(_movie_candidate(), _movie_result())
    assert score.components["title_score"] == 1.0
    assert score.components["year_score"] == 1.0
    assert score.total_score == 1.0
    assert "exact normalized title match" in score.reasons
    assert "exact release year match" in score.reasons


def test_exact_title_with_no_local_year_drops_year_component() -> None:
    score = score_movie_match(_movie_candidate(year=None), _movie_result())
    assert "year_score" not in score.components
    assert "local year unknown" in score.reasons
    # Total is title-only (no runtime), so it stays at the full title score.
    assert score.total_score == 1.0


def test_same_title_wrong_year_scores_lower_than_exact() -> None:
    exact = score_movie_match(_movie_candidate(year=1979), _movie_result(release_year=1979))
    wrong_year = score_movie_match(_movie_candidate(year=1979), _movie_result(release_year=2015))
    assert wrong_year.components["year_score"] == 0.0
    assert wrong_year.total_score < exact.total_score
    assert any("mismatch" in r for r in wrong_year.reasons)


def test_close_year_scores_between_exact_and_mismatch() -> None:
    close = score_movie_match(_movie_candidate(year=1979), _movie_result(release_year=1980))
    assert close.components["year_score"] == 0.6


def test_alternate_original_title_still_matches() -> None:
    score = score_movie_match(
        _movie_candidate(title="Sacrificio"),
        _movie_result(title="Sacrifice", original_title="Sacrificio"),
    )
    assert score.components["title_score"] == 1.0


def test_close_title_ambiguity_scores_between_zero_and_one() -> None:
    score = score_movie_match(_movie_candidate(title="Alien"), _movie_result(title="Aliens"))
    assert 0.0 < score.components["title_score"] < 1.0


def test_unrelated_titles_score_near_zero() -> None:
    score = score_movie_match(_movie_candidate(title="Alien"), _movie_result(title="The Notebook"))
    assert score.components["title_score"] < 0.3


def test_runtime_corroboration_increases_confidence_signal() -> None:
    score = score_movie_match(
        _movie_candidate(), _movie_result(runtime_seconds=117 * 60), local_runtime_seconds=117 * 60
    )
    assert score.components["runtime_score"] == 1.0
    assert "runtime closely matches" in score.reasons


def test_runtime_conflict_lowers_score() -> None:
    with_conflict = score_movie_match(
        _movie_candidate(), _movie_result(runtime_seconds=117 * 60), local_runtime_seconds=45 * 60
    )
    without_runtime = score_movie_match(_movie_candidate(), _movie_result())
    assert with_conflict.components["runtime_score"] == 0.0
    assert with_conflict.total_score < without_runtime.total_score


def test_missing_runtime_data_omits_runtime_component() -> None:
    score = score_movie_match(_movie_candidate(), _movie_result(runtime_seconds=None))
    assert "runtime_score" not in score.components


def test_type_mismatch_discounts_but_does_not_zero_total() -> None:
    matching_type = score_movie_match(_movie_candidate(candidate_type=CandidateType.MOVIE), _movie_result())
    mismatched_type = score_movie_match(_movie_candidate(candidate_type=CandidateType.EPISODE), _movie_result())
    assert mismatched_type.components["type_score"] < matching_type.components["type_score"]
    assert 0.0 < mismatched_type.total_score < matching_type.total_score
    assert any("does not match provider type MOVIE" in r for r in mismatched_type.reasons)


def test_popularity_never_influences_movie_score() -> None:
    low_popularity = _movie_result()
    high_popularity = MovieResult(
        provider_id=low_popularity.provider_id,
        title=low_popularity.title,
        original_title=low_popularity.original_title,
        release_year=low_popularity.release_year,
        original_language=low_popularity.original_language,
        popularity=99999.0,
        runtime_seconds=low_popularity.runtime_seconds,
    )
    score_low = score_movie_match(_movie_candidate(), low_popularity)
    score_high = score_movie_match(_movie_candidate(), high_popularity)
    assert score_low.total_score == score_high.total_score
    assert score_low.components == score_high.components
    assert "popularity" not in score_low.to_scoring_dict()


def test_scoring_is_deterministic_across_repeated_calls() -> None:
    scores = [score_movie_match(_movie_candidate(), _movie_result()).to_scoring_dict() for _ in range(5)]
    assert all(s == scores[0] for s in scores)


def test_to_scoring_dict_matches_documented_shape() -> None:
    score = score_movie_match(_movie_candidate(), _movie_result(runtime_seconds=117 * 60), local_runtime_seconds=117 * 60)
    data = score.to_scoring_dict()
    assert set(data.keys()) == {"title_score", "year_score", "runtime_score", "type_score", "total_score", "reasons"}
    assert isinstance(data["reasons"], list)


# --- episode scoring --------------------------------------------------------------


def test_exact_series_title_season_and_episode_scores_highest() -> None:
    score = score_episode_match(_episode_candidate(), _episode_result(), series_title="Breaking Bad")
    assert score.components["title_score"] == 1.0
    assert score.components["season_score"] == 1.0
    assert score.components["episode_score"] == 1.0
    assert score.total_score == 1.0


def test_season_mismatch_scores_zero_for_that_component() -> None:
    score = score_episode_match(_episode_candidate(season=2), _episode_result(season=1), series_title="Breaking Bad")
    assert score.components["season_score"] == 0.0
    assert any("season mismatch" in r for r in score.reasons)


def test_episode_mismatch_scores_zero_for_that_component() -> None:
    score = score_episode_match(_episode_candidate(episode=5), _episode_result(episode=1), series_title="Breaking Bad")
    assert score.components["episode_score"] == 0.0


def test_episode_title_corroboration_included_when_locally_available() -> None:
    score = score_episode_match(
        _episode_candidate(episode_title="Pilot"), _episode_result(episode_title="Pilot"), series_title="Breaking Bad"
    )
    assert score.components["episode_title_score"] == 1.0
    assert "episode title corroborates" in score.reasons


def test_episode_title_omitted_when_locally_unavailable() -> None:
    score = score_episode_match(
        _episode_candidate(episode_title=None), _episode_result(episode_title="Pilot"), series_title="Breaking Bad"
    )
    assert "episode_title_score" not in score.components


def test_missing_local_season_or_episode_number_is_recorded_but_not_scored() -> None:
    score = score_episode_match(
        _episode_candidate(season=None, episode=None), _episode_result(), series_title="Breaking Bad"
    )
    assert "season_score" not in score.components
    assert "episode_score" not in score.components
    assert "local season number unknown" in score.reasons
    assert "local episode number unknown" in score.reasons


def test_episode_type_mismatch_discounts_total() -> None:
    matching_type = score_episode_match(
        _episode_candidate(candidate_type=CandidateType.EPISODE), _episode_result(), series_title="Breaking Bad"
    )
    mismatched_type = score_episode_match(
        _episode_candidate(candidate_type=CandidateType.MOVIE), _episode_result(), series_title="Breaking Bad"
    )
    assert mismatched_type.total_score < matching_type.total_score


def test_special_candidate_type_matches_episode_provider_type() -> None:
    score = score_episode_match(
        _episode_candidate(candidate_type=CandidateType.SPECIAL), _episode_result(season=0), series_title="Breaking Bad"
    )
    assert score.components["type_score"] == 1.0


def test_episode_scoring_is_deterministic() -> None:
    scores = [
        score_episode_match(_episode_candidate(), _episode_result(), series_title="Breaking Bad").to_scoring_dict()
        for _ in range(5)
    ]
    assert all(s == scores[0] for s in scores)


def test_total_score_always_within_bounds() -> None:
    for candidate_type in CandidateType:
        movie_score = score_movie_match(_movie_candidate(candidate_type=candidate_type), _movie_result())
        assert 0.0 <= movie_score.total_score <= 1.0
        episode_score = score_episode_match(
            _episode_candidate(candidate_type=candidate_type), _episode_result(), series_title="Breaking Bad"
        )
        assert 0.0 <= episode_score.total_score <= 1.0
