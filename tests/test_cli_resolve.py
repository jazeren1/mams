"""CLI tests for mams resolve evaluate/list/show/select/reject/stats.

Seeds real data through run_inventory_scan()/run_identify_evaluate() (the
actual CLI scan/identify path), same approach as test_cli_identify.py.
`resolve evaluate`'s TMDb call is stubbed via a FakeProvider injected in
place of resolution_service.build_provider, so no test here makes a real
network call; list/show/select/reject/stats are exercised against
resolution_repository rows seeded directly, independent of `evaluate`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mams import resolution_service
from mams.cli import (
    build_parser,
    run_identify_evaluate,
    run_inventory_scan,
    run_resolve_evaluate,
    run_resolve_list,
    run_resolve_reject,
    run_resolve_select,
    run_resolve_show,
    run_resolve_stats,
)
from mams.config import load_config
from mams.db import connect, migrate
from mams.identification_repository import list_candidates
from mams.resolution_repository import CandidateMatchInput, record_attempt
from mams.scoring import MatchScore
from mams.tmdb import MovieResult


def _write_config(tmp_path: Path, *, categories: dict[str, str], tmdb_token_env_var: str | None = "TMDB_API_TOKEN") -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "MAMS Test",
                    "database_path": str(tmp_path / "database" / "mams.db"),
                    "log_level": "INFO",
                    "dry_run": True,
                },
                "nas": {"categories": categories},
                "tmdb": {"token_env_var": tmdb_token_env_var} if tmdb_token_env_var else {},
            }
        ),
        encoding="utf-8",
    )
    return config_path


def _touch(path: Path, size: int = 1_000_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)


def _report_path(tmp_path: Path) -> str:
    return str(tmp_path / "reports" / "library.json")


def _seed_movie(tmp_path: Path, name: str = "Alien (1979).mkv") -> Path:
    movies_root = tmp_path / "Movies"
    _touch(movies_root / name)
    return movies_root


class FakeProvider:
    def __init__(self, movie_results: list[MovieResult]) -> None:
        self.movie_results = movie_results

    def search_movie(self, title: str, year: int | None = None) -> list[MovieResult]:
        return self.movie_results

    def get_movie(self, provider_id: int) -> MovieResult | None:
        return None

    def search_tv(self, series_title: str):  # type: ignore[no-untyped-def]
        return []

    def get_tv_series(self, provider_id: int):  # type: ignore[no-untyped-def]
        return None

    def get_tv_episode(self, series_id: int, season_number: int, episode_number: int):  # type: ignore[no-untyped-def]
        return None


def _movie_result(provider_id: int = 348) -> MovieResult:
    return MovieResult(
        provider_id=provider_id, title="Alien", original_title="Alien", release_year=1979,
        original_language="en", popularity=50.0,
    )


# --- parser -----------------------------------------------------------------


def test_parser_accepts_evaluate_flags() -> None:
    args = build_parser().parse_args(
        ["resolve", "evaluate", "--media-file-id", "1", "--candidate-id", "2", "--category", "movies", "--limit", "5", "--force", "--json"]
    )
    assert args.command == "resolve"
    assert args.resolve_command == "evaluate"
    assert args.media_file_id == 1
    assert args.candidate_id == 2
    assert args.category == "movies"
    assert args.limit == 5
    assert args.force is True
    assert args.json is True


def test_parser_defaults_for_evaluate() -> None:
    args = build_parser().parse_args(["resolve", "evaluate"])
    assert args.media_file_id is None
    assert args.candidate_id is None
    assert args.limit == 10
    assert args.force is False


def test_parser_accepts_list_flags() -> None:
    args = build_parser().parse_args(["resolve", "list", "--status", "resolved", "--limit", "5", "--json"])
    assert args.status == "RESOLVED"
    assert args.limit == 5


def test_parser_accepts_show_select_reject_stats() -> None:
    assert build_parser().parse_args(["resolve", "show", "1"]).attempt_id == 1
    select_args = build_parser().parse_args(["resolve", "select", "1", "2"])
    assert (select_args.attempt_id, select_args.match_id) == (1, 2)
    assert build_parser().parse_args(["resolve", "reject", "1"]).attempt_id == 1
    assert build_parser().parse_args(["resolve", "stats", "--json"]).json is True


# --- evaluate: missing token --------------------------------------------------


def test_run_resolve_evaluate_without_token_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TMDB_API_TOKEN", raising=False)
    movies_root = _seed_movie(tmp_path)
    config = load_config(_write_config(tmp_path, categories={"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_identify_evaluate(config)
    capsys.readouterr()

    attempts = run_resolve_evaluate(config)

    assert attempts == []
    out = capsys.readouterr().out
    assert "No TMDb API token configured" in out


def test_other_commands_unaffected_by_missing_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMDB_API_TOKEN", raising=False)
    movies_root = _seed_movie(tmp_path)
    config = load_config(_write_config(tmp_path, categories={"movies": str(movies_root)}))
    report = run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    result = run_identify_evaluate(config)
    assert report.file_count == 1
    assert result.created == 1


# --- evaluate: with a fake provider --------------------------------------------


def test_run_resolve_evaluate_auto_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TMDB_API_TOKEN", "test-token")
    movies_root = _seed_movie(tmp_path)
    config = load_config(_write_config(tmp_path, categories={"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_identify_evaluate(config)
    capsys.readouterr()
    monkeypatch.setattr(resolution_service, "build_provider", lambda cfg, conn: FakeProvider([_movie_result()]))

    attempts = run_resolve_evaluate(config)

    assert len(attempts) == 1
    assert attempts[0].status == "RESOLVED"
    out = capsys.readouterr().out
    assert "Evaluated: 1" in out


def test_run_resolve_evaluate_respects_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMDB_API_TOKEN", "test-token")
    movies_root = tmp_path / "Movies"
    _touch(movies_root / "Alien (1979).mkv")
    _touch(movies_root / "Aliens (1986).mkv")
    config = load_config(_write_config(tmp_path, categories={"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_identify_evaluate(config)
    monkeypatch.setattr(resolution_service, "build_provider", lambda cfg, conn: FakeProvider([_movie_result()]))

    attempts = run_resolve_evaluate(config, limit=1)

    assert len(attempts) == 1


def test_run_resolve_evaluate_skips_already_attempted_unless_forced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMDB_API_TOKEN", "test-token")
    movies_root = _seed_movie(tmp_path)
    config = load_config(_write_config(tmp_path, categories={"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_identify_evaluate(config)
    monkeypatch.setattr(resolution_service, "build_provider", lambda cfg, conn: FakeProvider([_movie_result()]))

    run_resolve_evaluate(config)
    second = run_resolve_evaluate(config)
    assert second == []

    third = run_resolve_evaluate(config, force=True)
    assert len(third) == 1


# --- list/show/select/reject/stats (seeded directly) ---------------------------


def _seeded_attempt(tmp_path: Path, config: object, *, review_required: bool = False) -> tuple[int, int, int]:
    _seed_movie(tmp_path)
    from mams.config import AppConfig

    assert isinstance(config, AppConfig)
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_identify_evaluate(config)

    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        candidate = list_candidates(connection)[0]
        matches = [
            CandidateMatchInput(
                provider_media_type="MOVIE", provider_id=348, title="Alien", release_year=1979,
                series_title=None, series_provider_id=None, season_number=None, episode_number=None,
                score=MatchScore(total_score=0.95, components={"title_score": 1.0}, reasons=("exact match",)),
                selected=not review_required,
            )
        ]
        if review_required:
            matches.append(
                CandidateMatchInput(
                    provider_media_type="MOVIE", provider_id=999, title="Alien", release_year=1979,
                    series_title=None, series_provider_id=None, season_number=None, episode_number=None,
                    score=MatchScore(total_score=0.90, components={"title_score": 1.0}, reasons=("close match",)),
                )
            )
        with connection:
            attempt = record_attempt(
                connection, identification_candidate_id=candidate.id, media_file_id=candidate.media_file_id,
                status="REVIEW_REQUIRED" if review_required else "RESOLVED", query_text="Alien", query_year=1979,
                error_message=None, matches=matches, algorithm_version=1,
            )
        return attempt.id, attempt.matches[0].id, candidate.media_file_id
    finally:
        connection.close()


def test_run_resolve_list_and_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = load_config(_write_config(tmp_path, categories={"movies": str(tmp_path / "Movies")}))
    attempt_id, _, _ = _seeded_attempt(tmp_path, config)
    capsys.readouterr()

    attempts = run_resolve_list(config)
    assert len(attempts) == 1
    out = capsys.readouterr().out
    assert "RESOLVED" in out

    attempt = run_resolve_show(config, attempt_id)
    assert attempt is not None
    assert attempt.id == attempt_id
    out = capsys.readouterr().out
    assert "Ranked matches" in out


def test_run_resolve_show_unknown_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = load_config(_write_config(tmp_path, categories={"movies": str(tmp_path / "Movies")}))
    migrate(config.database_path)
    result = run_resolve_show(config, 999999)
    assert result is None
    assert "not found" in capsys.readouterr().out


def test_run_resolve_select_and_reject(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = load_config(_write_config(tmp_path, categories={"movies": str(tmp_path / "Movies")}))
    attempt_id, _, media_file_id = _seeded_attempt(tmp_path, config, review_required=True)
    connection = connect(config.database_path)
    try:
        attempt = next(a for a in run_resolve_list(config) if a.id == attempt_id)
    finally:
        connection.close()
    second_match_id = attempt.matches[1].id
    capsys.readouterr()

    updated = run_resolve_select(config, attempt_id, second_match_id)
    assert updated is not None
    assert updated.status == "RESOLVED"
    out = capsys.readouterr().out
    assert "resolved via manual selection" in out


def test_run_resolve_reject_marks_no_match(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = load_config(_write_config(tmp_path, categories={"movies": str(tmp_path / "Movies")}))
    attempt_id, _, _ = _seeded_attempt(tmp_path, config, review_required=True)
    capsys.readouterr()

    updated = run_resolve_reject(config, attempt_id)
    assert updated is not None
    assert updated.status == "NO_MATCH"
    assert "manually rejected" in capsys.readouterr().out


def test_run_resolve_select_unknown_match(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = load_config(_write_config(tmp_path, categories={"movies": str(tmp_path / "Movies")}))
    attempt_id, _, _ = _seeded_attempt(tmp_path, config, review_required=True)
    capsys.readouterr()

    result = run_resolve_select(config, attempt_id, 999999)
    assert result is None
    assert "does not belong to attempt" in capsys.readouterr().out


def test_run_resolve_stats_empty_state(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = load_config(_write_config(tmp_path, categories={"movies": str(tmp_path / "Movies")}))
    migrate(config.database_path)
    stats = run_resolve_stats(config)
    assert stats.total_count == 0
    assert "Total: 0" in capsys.readouterr().out
