"""Deterministic, explainable match scoring between a local
`identification.IdentificationCandidate` (a parsed, unconfirmed local
interpretation of a file) and a normalized TMDb search/detail result
(`tmdb.MovieResult` / `tmdb.EpisodeResult`).

Pure and independently testable: no SQL, no HTTP, no filesystem access.
`resolution_service.py` is the only module that wires this scoring to a
live `MediaProvider` and to persistence.

Every score is a `MatchScore` carrying `components` (the same evidence
used to compute `total_score`, keyed by name, `None` when a component
could not be evaluated) and `reasons` (a human-readable audit trail) --
together these become `resolution_matches.scoring_json`
(see docs/DATABASE.md).

Provider popularity is deliberately never a scoring input: the milestone
requires popularity to be, at most, "a final weak tie-breaker" that "must
be visible in scoring evidence if used" -- `resolution_service.py` is
where any such tie-breaking on `MovieResult.popularity`/rank assignment
happens (documented there), not here.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from .identification import IdentificationCandidate
from .tmdb import EpisodeResult, MovieResult

# Movie component weights. Weights for components that could not be
# evaluated (e.g. no local year) are dropped and the remainder
# renormalized -- see `_weighted_average`.
MOVIE_TITLE_WEIGHT = 0.5
MOVIE_YEAR_WEIGHT = 0.35
MOVIE_RUNTIME_WEIGHT = 0.15

# Episode component weights.
EPISODE_SERIES_TITLE_WEIGHT = 0.35
EPISODE_SEASON_WEIGHT = 0.2
EPISODE_NUMBER_WEIGHT = 0.2
EPISODE_TITLE_WEIGHT = 0.15
EPISODE_RUNTIME_WEIGHT = 0.1

_EXACT_YEAR_SCORE = 1.0
_CLOSE_YEAR_SCORE = 0.6
_UNKNOWN_CANDIDATE_YEAR_SCORE = 0.5
_MISMATCHED_YEAR_SCORE = 0.0

_RUNTIME_CLOSE_RATIO = 0.05
_RUNTIME_ROUGH_RATIO = 0.15
_RUNTIME_CLOSE_SCORE = 1.0
_RUNTIME_ROUGH_SCORE = 0.6
_RUNTIME_CONFLICT_SCORE = 0.0

_TYPE_MATCH_SCORE = 1.0
_TYPE_MISMATCH_SCORE = 0.3


@dataclass(frozen=True)
class MatchScore:
    """A scored candidate match, explainable via `components`/`reasons`.

    `components` holds every score that was actually evaluated (`None`
    values are never included; a component is entirely absent, not
    present-with-None) so `to_scoring_dict()` matches the milestone's
    documented shape:
    `{"title_score": 0.95, "year_score": 1.0, "total_score": 0.94,
    "reasons": [...]}`.
    """

    total_score: float
    components: dict[str, float]
    reasons: tuple[str, ...]

    def to_scoring_dict(self) -> dict[str, object]:
        data: dict[str, object] = {key: round(value, 4) for key, value in self.components.items()}
        data["total_score"] = round(self.total_score, 4)
        data["reasons"] = list(self.reasons)
        return data


def _normalize_title(title: str) -> str:
    """Lowercase, accent-folded, punctuation-collapsed, single-leading-
    article-stripped normalization for similarity comparison only --
    deliberately more aggressive than `identification._titles_similar`,
    which exists for a different job (conservative conflict detection
    between two local evidence sources, not continuous similarity)."""
    text = unicodedata.normalize("NFKD", title)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for article in ("the ", "a ", "an "):
        if text.startswith(article):
            text = text[len(article) :]
            break
    return text


def _title_similarity(a: str | None, b: str | None) -> float:
    """1.0 for an exact normalized match, 0.0 if either title is missing,
    otherwise `difflib.SequenceMatcher`'s ratio on normalized text --
    deterministic and dependency-free."""
    if not a or not b:
        return 0.0
    norm_a, norm_b = _normalize_title(a), _normalize_title(b)
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0
    return SequenceMatcher(None, norm_a, norm_b).ratio()


def _year_score(local_year: int | None, candidate_year: int | None) -> tuple[float | None, str]:
    if local_year is None:
        return None, "local year unknown"
    if candidate_year is None:
        return _UNKNOWN_CANDIDATE_YEAR_SCORE, "candidate release year unknown"
    diff = abs(local_year - candidate_year)
    if diff == 0:
        return _EXACT_YEAR_SCORE, "exact release year match"
    if diff == 1:
        return _CLOSE_YEAR_SCORE, f"release year off by {diff} year"
    return _MISMATCHED_YEAR_SCORE, f"release year mismatch ({local_year} vs {candidate_year})"


def _runtime_score(local_seconds: float | None, candidate_seconds: int | None) -> tuple[float | None, str | None]:
    if local_seconds is None or not candidate_seconds:
        return None, None
    ratio = abs(local_seconds - candidate_seconds) / candidate_seconds
    if ratio <= _RUNTIME_CLOSE_RATIO:
        return _RUNTIME_CLOSE_SCORE, "runtime closely matches"
    if ratio <= _RUNTIME_ROUGH_RATIO:
        return _RUNTIME_ROUGH_SCORE, "runtime roughly matches"
    return _RUNTIME_CONFLICT_SCORE, f"runtime disagrees ({local_seconds:.0f}s vs {candidate_seconds}s)"


def _weighted_average(components: list[tuple[float, float]]) -> float:
    """Weighted mean over components that were actually evaluated. A
    component with no evaluated value contributes no weight at all,
    rather than being scored as 0 -- "missing evidence" and "contradicting
    evidence" must never look the same."""
    total_weight = sum(weight for _, weight in components)
    if total_weight == 0:
        return 0.0
    return sum(score * weight for score, weight in components) / total_weight


def _title_reason(label: str, score: float) -> str:
    if score >= 0.999:
        return f"exact normalized {label} match"
    if score >= 0.8:
        return f"close {label} similarity"
    if score > 0:
        return f"weak {label} similarity"
    return f"no meaningful {label} similarity"


def score_movie_match(
    candidate: IdentificationCandidate,
    result: MovieResult,
    *,
    local_runtime_seconds: float | None = None,
) -> MatchScore:
    """Score one TMDb movie result against a local movie candidate.
    Considers normalized title equality/similarity (against both the
    provider's title and original title), exact/close/missing year, and
    runtime corroboration when both sides have a runtime. Local category
    consistency is captured by `type_score`: a candidate whose
    `candidate_type` is not `MOVIE` is heavily discounted, never zeroed --
    the mismatch must remain visible to a human reviewer, not silently
    hide the rest of the evidence."""
    reasons: list[str] = []
    components: dict[str, float] = {}

    title_score = max(
        _title_similarity(candidate.parsed_title, result.title),
        _title_similarity(candidate.parsed_title, result.original_title),
    )
    components["title_score"] = title_score
    reasons.append(_title_reason("title", title_score))

    year_score, year_reason = _year_score(candidate.parsed_year, result.release_year)
    reasons.append(year_reason)
    weighted_components = [(title_score, MOVIE_TITLE_WEIGHT)]
    if year_score is not None:
        components["year_score"] = year_score
        weighted_components.append((year_score, MOVIE_YEAR_WEIGHT))

    runtime_score, runtime_reason = _runtime_score(local_runtime_seconds, result.runtime_seconds)
    if runtime_reason is not None:
        reasons.append(runtime_reason)
    if runtime_score is not None:
        components["runtime_score"] = runtime_score
        weighted_components.append((runtime_score, MOVIE_RUNTIME_WEIGHT))

    type_score = _TYPE_MATCH_SCORE if candidate.candidate_type.value == "MOVIE" else _TYPE_MISMATCH_SCORE
    components["type_score"] = type_score
    if type_score < _TYPE_MATCH_SCORE:
        reasons.append(f"local candidate type {candidate.candidate_type.value} does not match provider type MOVIE")

    total_score = round(_weighted_average(weighted_components) * type_score, 4)
    return MatchScore(total_score=total_score, components=components, reasons=tuple(reasons))


def score_episode_match(
    candidate: IdentificationCandidate,
    result: EpisodeResult,
    *,
    series_title: str | None,
    local_runtime_seconds: float | None = None,
) -> MatchScore:
    """Score one TMDb episode result against a local episode candidate.
    Considers normalized series-title similarity, exact season/episode
    number match, episode-title corroboration when both sides have one,
    and runtime corroboration. `series_title` is the provider's series
    title (from the paired `SeriesResult`/`get_tv_series` lookup) -- kept
    as an explicit parameter rather than a field on `EpisodeResult` so
    this function never needs to know how the caller obtained it."""
    reasons: list[str] = []
    components: dict[str, float] = {}
    weighted_components: list[tuple[float, float]] = []

    series_title_score = _title_similarity(candidate.parsed_series_title, series_title)
    components["title_score"] = series_title_score
    reasons.append(_title_reason("series title", series_title_score))
    weighted_components.append((series_title_score, EPISODE_SERIES_TITLE_WEIGHT))

    if candidate.season_number is not None:
        season_score = _TYPE_MATCH_SCORE if candidate.season_number == result.season_number else 0.0
        components["season_score"] = season_score
        reasons.append(
            "exact season match"
            if season_score == _TYPE_MATCH_SCORE
            else f"season mismatch ({candidate.season_number} vs {result.season_number})"
        )
        weighted_components.append((season_score, EPISODE_SEASON_WEIGHT))
    else:
        reasons.append("local season number unknown")

    if candidate.episode_number is not None:
        episode_score = _TYPE_MATCH_SCORE if candidate.episode_number == result.episode_number else 0.0
        components["episode_score"] = episode_score
        reasons.append(
            "exact episode match"
            if episode_score == _TYPE_MATCH_SCORE
            else f"episode mismatch ({candidate.episode_number} vs {result.episode_number})"
        )
        weighted_components.append((episode_score, EPISODE_NUMBER_WEIGHT))
    else:
        reasons.append("local episode number unknown")

    if candidate.episode_title and result.episode_title:
        episode_title_score = _title_similarity(candidate.episode_title, result.episode_title)
        components["episode_title_score"] = episode_title_score
        reasons.append(
            "episode title corroborates" if episode_title_score >= 0.8 else "episode title does not corroborate"
        )
        weighted_components.append((episode_title_score, EPISODE_TITLE_WEIGHT))

    runtime_score, runtime_reason = _runtime_score(local_runtime_seconds, result.runtime_seconds)
    if runtime_reason is not None:
        reasons.append(runtime_reason)
    if runtime_score is not None:
        components["runtime_score"] = runtime_score
        weighted_components.append((runtime_score, EPISODE_RUNTIME_WEIGHT))

    type_score = _TYPE_MATCH_SCORE if candidate.candidate_type.value in ("EPISODE", "SPECIAL") else _TYPE_MISMATCH_SCORE
    components["type_score"] = type_score
    if type_score < _TYPE_MATCH_SCORE:
        reasons.append(f"local candidate type {candidate.candidate_type.value} does not match provider type EPISODE")

    total_score = round(_weighted_average(weighted_components) * type_score, 4)
    return MatchScore(total_score=total_score, components=components, reasons=tuple(reasons))
