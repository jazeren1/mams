"""Orchestrates TMDb resolution: search -> score -> decide -> persist.

This is the only module that combines `tmdb.py` (HTTP), `scoring.py`
(pure scoring), and `resolution_repository.py` (SQL) -- none of those
three modules depends on either of the others. Mirrors the role
`identification_service.py` plays for local parsing.

## Thresholds (Phase E)

- **AUTO_RESOLVE / HIGH**: top score >= `AUTO_RESOLVE_MIN_SCORE`, gap from
  the second-best match >= `AUTO_RESOLVE_MIN_GAP`, no provider-type
  conflict, and (movies only) a known local year, or (episodes only) an
  exact season+episode match and a candidate that isn't SPECIAL/EXTRA.
  A movie with no local year, or an episode without an exact season+
  episode match, never auto-resolves regardless of score.
- **REVIEW_REQUIRED / MEDIUM**: top score >= `MIN_PLAUSIBLE_SCORE` but the
  auto-resolve bar above wasn't met.
- **NO_MATCH**: no result reached `MIN_PLAUSIBLE_SCORE`, or TMDb returned
  no results at all. No assignment is created.
- **FAILED**: a `tmdb.TMDbError` was raised (auth, rate limit, timeout,
  connection, malformed response).
- **SKIPPED**: the local candidate's `candidate_type` is `UNKNOWN` (no
  usable query text) or `EXTRA` (bonus/deleted-scene/featurette content
  TMDb does not catalog as a distinct, searchable entity) -- TMDb is never
  queried for these. `SPECIAL` candidates *are* searched (as a season-0
  episode) but are capped at REVIEW_REQUIRED, never auto-resolved.

Every attempt is recorded, including SKIPPED/FAILED/NO_MATCH ones --
resolution_attempts is permanent history (see docs/DATABASE.md).

Provider popularity is used only as a documented, final, weak tie-breaker
when ranking movie search results with an equal `total_score` -- never as
a scoring input (see scoring.py). Episode ranking has no popularity
tie-break (TMDb exposes no meaningful per-episode popularity distinct
from its series); ties are broken by provider id alone.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .config import AppConfig
from .identification import CandidateType, Confidence, IdentificationCandidate
from .identification_repository import CandidateRecord
from .provider_cache_repository import SqliteCacheStore
from .resolution_repository import (
    AssignmentRecord,
    AttemptRecord,
    CandidateMatchInput,
    MatchRecord,
    assign_identity,
    record_attempt,
    reject_attempt as repo_reject_attempt,
    select_match as repo_select_match,
    upsert_external_identity,
)
from .scoring import MatchScore, score_episode_match, score_movie_match, title_similarity
from .tmdb import EpisodeResult, MediaProvider, MovieResult, SeriesResult, TMDbClient, TMDbError

ALGORITHM_VERSION = 1

AUTO_RESOLVE_MIN_SCORE = 0.90
AUTO_RESOLVE_MIN_GAP = 0.10
MIN_PLAUSIBLE_SCORE = 0.60

# Bounds how many TMDb detail lookups one evaluation can trigger, so a
# noisy search result list never turns into an unbounded number of extra
# requests (the provider_cache makes repeats free, but a first pass still
# costs real quota/latency).
MOVIE_DETAIL_LOOKUP_LIMIT = 3
SERIES_CANDIDATE_LOOKUP_LIMIT = 2


class TMDbNotConfiguredError(Exception):
    """Raised when resolution is attempted with no TMDb token configured.
    Callers (the CLI) must check for this *before* calling
    `evaluate_candidate` at all, so that no resolution_attempts row (not
    even a FAILED one) is ever created for "we never had credentials" --
    that is a configuration problem, not a resolution outcome."""


def build_provider(config: AppConfig, connection: sqlite3.Connection) -> TMDbClient:
    """The only place `tmdb.TMDbClient` and `provider_cache_repository`
    are wired together. Raises `TMDbNotConfiguredError` if no token is
    configured -- callers (the CLI) must call this, and handle that
    error, before doing anything else for a `resolve` command; every
    other existing command (inventory/findings/identify) never calls this
    and is completely unaffected by whether TMDb is configured."""
    token = config.tmdb_token
    if not token:
        raise TMDbNotConfiguredError(
            "No TMDb API token configured. Set the environment variable named by "
            "config.yaml's tmdb.token_env_var (default TMDB_API_TOKEN)."
        )
    cache = SqliteCacheStore(connection, provider="TMDB", ttl_seconds=config.tmdb_cache_ttl_seconds)
    return TMDbClient(token=token, cache=cache)


@dataclass(frozen=True)
class Decision:
    status: str
    confidence: str | None
    reasons: tuple[str, ...]


def _to_identification_candidate(record: CandidateRecord) -> IdentificationCandidate:
    """Reconstruct the pure parsing-domain object scoring.py depends on
    from its persisted repository row. `evidence` is intentionally empty
    here -- scoring never reads it, and resolution has no need to
    round-trip the original parse evidence."""
    return IdentificationCandidate(
        media_file_id=record.media_file_id,
        candidate_type=CandidateType(record.candidate_type),
        confidence=Confidence(record.confidence),
        parser_version=record.parser_version,
        evidence={},
        parsed_title=record.parsed_title,
        parsed_year=record.parsed_year,
        parsed_series_title=record.parsed_series_title,
        season_number=record.season_number,
        episode_number=record.episode_number,
        episode_numbers=record.episode_numbers,
        episode_title=record.episode_title,
        edition=record.edition,
        part_number=record.part_number,
        special_type=record.special_type,
    )


def _search_and_score_movies(
    provider: MediaProvider,
    candidate: IdentificationCandidate,
    local_runtime_seconds: float | None,
) -> list[tuple[MovieResult, MatchScore]]:
    results = provider.search_movie(candidate.parsed_title or "", candidate.parsed_year)
    if not results:
        return []

    enriched: dict[int, MovieResult] = {}
    if local_runtime_seconds is not None:
        # Runtime only helps once we know which few results are worth the
        # extra lookup -- rank by a runtime-less preliminary score first.
        preliminary = sorted(results, key=lambda r: -score_movie_match(candidate, r).total_score)
        for result in preliminary[:MOVIE_DETAIL_LOOKUP_LIMIT]:
            detail = provider.get_movie(result.provider_id)
            if detail is not None:
                enriched[result.provider_id] = detail

    scored = [
        (
            enriched.get(result.provider_id, result),
            score_movie_match(
                candidate, enriched.get(result.provider_id, result), local_runtime_seconds=local_runtime_seconds
            ),
        )
        for result in results
    ]
    return sorted(scored, key=lambda pair: (-pair[1].total_score, -(pair[0].popularity or 0.0), pair[0].provider_id))


def _decide_movie_outcome(candidate: IdentificationCandidate, ranked: list[tuple[MovieResult, MatchScore]]) -> Decision:
    if not ranked:
        return Decision(status="NO_MATCH", confidence=None, reasons=("no TMDb search results",))

    _, top_score = ranked[0]
    if top_score.total_score < MIN_PLAUSIBLE_SCORE:
        return Decision(
            status="NO_MATCH",
            confidence=None,
            reasons=(f"top score {top_score.total_score:.4f} below minimum plausible threshold {MIN_PLAUSIBLE_SCORE}",),
        )

    second_score = ranked[1][1].total_score if len(ranked) > 1 else 0.0
    gap = round(top_score.total_score - second_score, 4)
    type_conflict = top_score.components.get("type_score", 1.0) < 1.0
    year_known = candidate.parsed_year is not None

    if top_score.total_score >= AUTO_RESOLVE_MIN_SCORE and gap >= AUTO_RESOLVE_MIN_GAP and not type_conflict and year_known:
        return Decision(
            status="RESOLVED",
            confidence="HIGH",
            reasons=(
                f"top score {top_score.total_score:.4f} >= {AUTO_RESOLVE_MIN_SCORE}",
                f"score gap {gap:.4f} >= {AUTO_RESOLVE_MIN_GAP}",
                "known local year, no provider type conflict",
            ),
        )

    reasons = [f"top score {top_score.total_score:.4f} is plausible but did not meet the auto-resolve bar"]
    if gap < AUTO_RESOLVE_MIN_GAP:
        reasons.append(f"score gap {gap:.4f} below {AUTO_RESOLVE_MIN_GAP} -- competing result too close")
    if not year_known:
        reasons.append("local year is unknown; movies without a year never auto-resolve")
    if type_conflict:
        reasons.append("provider media type conflicts with the local candidate")
    return Decision(status="REVIEW_REQUIRED", confidence="MEDIUM", reasons=tuple(reasons))


def _search_and_score_episodes(
    provider: MediaProvider,
    candidate: IdentificationCandidate,
    local_runtime_seconds: float | None,
) -> list[tuple[SeriesResult, EpisodeResult, MatchScore]]:
    if candidate.parsed_series_title is None or candidate.season_number is None or candidate.episode_number is None:
        return []

    series_results = provider.search_tv(candidate.parsed_series_title)
    if not series_results:
        return []

    ranked_series = sorted(series_results, key=lambda s: -title_similarity(candidate.parsed_series_title, s.title))
    scored: list[tuple[SeriesResult, EpisodeResult, MatchScore]] = []
    for series in ranked_series[:SERIES_CANDIDATE_LOOKUP_LIMIT]:
        episode = provider.get_tv_episode(series.provider_id, candidate.season_number, candidate.episode_number)
        if episode is None:
            continue
        score = score_episode_match(
            candidate, episode, series_title=series.title, local_runtime_seconds=local_runtime_seconds
        )
        scored.append((series, episode, score))

    return sorted(scored, key=lambda triple: (-triple[2].total_score, triple[0].provider_id))


def _decide_episode_outcome(
    candidate: IdentificationCandidate, ranked: list[tuple[SeriesResult, EpisodeResult, MatchScore]]
) -> Decision:
    if not ranked:
        return Decision(status="NO_MATCH", confidence=None, reasons=("no TMDb series/episode results",))

    _, _, top_score = ranked[0]
    if top_score.total_score < MIN_PLAUSIBLE_SCORE:
        return Decision(
            status="NO_MATCH",
            confidence=None,
            reasons=(f"top score {top_score.total_score:.4f} below minimum plausible threshold {MIN_PLAUSIBLE_SCORE}",),
        )

    second_score = ranked[1][2].total_score if len(ranked) > 1 else 0.0
    gap = round(top_score.total_score - second_score, 4)
    type_conflict = top_score.components.get("type_score", 1.0) < 1.0
    season_exact = top_score.components.get("season_score") == 1.0
    episode_exact = top_score.components.get("episode_score") == 1.0
    is_special_or_extra = candidate.candidate_type in (CandidateType.SPECIAL, CandidateType.EXTRA)

    if (
        top_score.total_score >= AUTO_RESOLVE_MIN_SCORE
        and gap >= AUTO_RESOLVE_MIN_GAP
        and not type_conflict
        and season_exact
        and episode_exact
        and not is_special_or_extra
    ):
        return Decision(
            status="RESOLVED",
            confidence="HIGH",
            reasons=(
                f"top score {top_score.total_score:.4f} >= {AUTO_RESOLVE_MIN_SCORE}",
                f"score gap {gap:.4f} >= {AUTO_RESOLVE_MIN_GAP}",
                "exact season/episode match, no provider type conflict",
            ),
        )

    reasons = [f"top score {top_score.total_score:.4f} is plausible but did not meet the auto-resolve bar"]
    if gap < AUTO_RESOLVE_MIN_GAP:
        reasons.append(f"score gap {gap:.4f} below {AUTO_RESOLVE_MIN_GAP} -- competing series/episode result")
    if not season_exact or not episode_exact:
        reasons.append("season/episode number does not exactly match")
    if type_conflict:
        reasons.append("provider media type conflicts with the local candidate")
    if is_special_or_extra:
        reasons.append("local candidate is SPECIAL/EXTRA; provider mapping requires review")
    return Decision(status="REVIEW_REQUIRED", confidence="MEDIUM", reasons=tuple(reasons))


def _record_terminal(
    connection: sqlite3.Connection,
    candidate_record: CandidateRecord,
    *,
    status: str,
    query_text: str | None,
    query_year: int | None,
    error_message: str | None = None,
    matches: list[CandidateMatchInput] | None = None,
    override_id: int | None = None,
) -> AttemptRecord:
    with connection:
        return record_attempt(
            connection,
            identification_candidate_id=candidate_record.id,
            media_file_id=candidate_record.media_file_id,
            status=status,
            query_text=query_text,
            query_year=query_year,
            error_message=error_message,
            matches=matches or [],
            algorithm_version=ALGORITHM_VERSION,
            identification_override_id=override_id,
        )


def evaluate_candidate(
    connection: sqlite3.Connection,
    provider: MediaProvider,
    *,
    candidate_record: CandidateRecord,
    local_runtime_seconds: float | None,
    effective_candidate: IdentificationCandidate | None = None,
    override_id: int | None = None,
) -> AttemptRecord:
    """Resolve one identification_candidates row against TMDb: search,
    score, decide, and persist one resolution_attempts row (plus its
    ranked resolution_matches and, if RESOLVED, an upserted
    external_identities row and a media_identity_assignments row) -- all
    inside one transaction, so a failure partway through never leaves a
    partial write. Never raises for a provider failure (records status
    FAILED instead); raises nothing else.

    `effective_candidate`/`override_id` (Milestone 7C, Phase D) let a
    caller resolve against a manual override instead of the plain parsed
    candidate: when `effective_candidate` is given, it is used in place of
    `candidate_record`'s own parsed fields, and `override_id` is recorded
    on the persisted attempt. Both default to `None`, reproducing this
    function's original (pre-override) behavior exactly -- every existing
    caller that omits them is unaffected. `candidate_record` is still
    required in both cases: it supplies `id`/`media_file_id` for the
    attempt's FK columns, which an override never replaces (overrides
    don't get their own identification_candidates row)."""
    candidate = effective_candidate if effective_candidate is not None else _to_identification_candidate(candidate_record)

    if candidate.candidate_type in (CandidateType.UNKNOWN, CandidateType.EXTRA):
        return _record_terminal(
            connection, candidate_record, status="SKIPPED", query_text=None, query_year=None, override_id=override_id
        )

    is_movie = candidate.candidate_type == CandidateType.MOVIE
    query_text = candidate.parsed_title if is_movie else candidate.parsed_series_title
    query_year = candidate.parsed_year if is_movie else None

    try:
        if is_movie:
            ranked_movies = _search_and_score_movies(provider, candidate, local_runtime_seconds)
            decision = _decide_movie_outcome(candidate, ranked_movies)
        else:
            ranked_episodes = _search_and_score_episodes(provider, candidate, local_runtime_seconds)
            decision = _decide_episode_outcome(candidate, ranked_episodes)
    except TMDbError as exc:
        return _record_terminal(
            connection, candidate_record, status="FAILED", query_text=query_text, query_year=query_year,
            error_message=str(exc), override_id=override_id,
        )

    if is_movie:
        match_inputs = [
            CandidateMatchInput(
                provider_media_type="MOVIE",
                provider_id=result.provider_id,
                title=result.title,
                release_year=result.release_year,
                series_title=None,
                series_provider_id=None,
                season_number=None,
                episode_number=None,
                score=score,
                selected=(decision.status == "RESOLVED" and index == 0),
            )
            for index, (result, score) in enumerate(ranked_movies)
        ]
    else:
        match_inputs = [
            CandidateMatchInput(
                provider_media_type="EPISODE",
                provider_id=episode.provider_id,
                title=episode.episode_title,
                release_year=None,
                series_title=series.title,
                series_provider_id=series.provider_id,
                season_number=episode.season_number,
                episode_number=episode.episode_number,
                score=score,
                selected=(decision.status == "RESOLVED" and index == 0),
            )
            for index, (series, episode, score) in enumerate(ranked_episodes)
        ]

    with connection:
        attempt = record_attempt(
            connection,
            identification_candidate_id=candidate_record.id,
            media_file_id=candidate_record.media_file_id,
            status=decision.status,
            query_text=query_text,
            query_year=query_year,
            error_message=None,
            matches=match_inputs,
            algorithm_version=ALGORITHM_VERSION,
            identification_override_id=override_id,
        )

        if decision.status == "RESOLVED":
            _confirm_match(
                connection,
                attempt=attempt,
                match=attempt.matches[0],
                assignment_method="AUTO",
                confidence=decision.confidence or "HIGH",
            )

        return attempt


def _confidence_for_score(total_score: float) -> str:
    """Confidence bucket for a manually-selected match, derived from the
    selected match's own score -- independent of the attempt's original
    (possibly REVIEW_REQUIRED or NO_MATCH) status, since a human
    reviewing and accepting a below-threshold suggestion is a different
    judgment than the automatic evaluation that flagged it."""
    if total_score >= AUTO_RESOLVE_MIN_SCORE:
        return "HIGH"
    if total_score >= MIN_PLAUSIBLE_SCORE:
        return "MEDIUM"
    return "LOW"


def _confirm_match(
    connection: sqlite3.Connection,
    *,
    attempt: AttemptRecord,
    match: MatchRecord,
    assignment_method: str,
    confidence: str,
) -> AssignmentRecord:
    """Upsert the external identity for one already-scored, already-
    persisted match and confirm it as the file's ACTIVE assignment.
    Shared by automatic resolution (the rank-1 match) and manual selection
    (whichever match the reviewer chose) -- both already have
    `attempt.media_file_id`/`attempt.identification_candidate_id` on hand,
    so neither needs a separate CandidateRecord lookup."""
    if match.provider_media_type == "MOVIE":
        identity = upsert_external_identity(
            connection,
            media_type="MOVIE",
            provider_id=match.provider_id,
            title=match.title,
            release_year=match.release_year,
        )
    else:
        identity = upsert_external_identity(
            connection,
            media_type="EPISODE",
            provider_id=match.provider_id,
            title=match.series_title,
            series_provider_id=match.series_provider_id,
            season_number=match.season_number,
            episode_number=match.episode_number,
            episode_title=match.title,
        )
    assert attempt.media_file_id is not None  # SET NULL only follows a media_files delete; nothing deletes one today
    return assign_identity(
        connection,
        media_file_id=attempt.media_file_id,
        identification_candidate_id=attempt.identification_candidate_id,
        external_identity_id=identity.id,
        resolution_attempt_id=attempt.id,
        assignment_method=assignment_method,
        confidence=confidence,
    )


def select_match_manually(connection: sqlite3.Connection, *, attempt_id: int, match_id: int) -> AttemptRecord:
    """Manual review: confirm one specific ranked match (not necessarily
    rank 1) as the file's identity. Marks the attempt RESOLVED, preserves
    every other ranked match untouched, and creates/updates the ACTIVE
    media_identity_assignment with `assignment_method='MANUAL'`. Writes
    no media files."""
    with connection:
        attempt = repo_select_match(connection, attempt_id=attempt_id, match_id=match_id)
        chosen = next(match for match in attempt.matches if match.id == match_id)
        _confirm_match(
            connection,
            attempt=attempt,
            match=chosen,
            assignment_method="MANUAL",
            confidence=_confidence_for_score(chosen.score),
        )
        return attempt


def reject_match_manually(connection: sqlite3.Connection, *, attempt_id: int) -> AttemptRecord:
    """Manual review: reject every returned match for this attempt. Marks
    the attempt NO_MATCH, creates no assignment, and preserves every
    ranked alternative untouched."""
    with connection:
        return repo_reject_attempt(connection, attempt_id=attempt_id)
