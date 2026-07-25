"""CLI tests for mams identify evaluate/list/stats/show.

Seeds real data through run_inventory_scan() (the actual CLI scan path),
same approach as test_cli_findings.py, so these exercise the full
identify CLI wiring end to end: scan -> evaluate -> list/stats/show.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mams.cli import (
    build_parser,
    run_identify_clear_override,
    run_identify_evaluate,
    run_identify_list,
    run_identify_override,
    run_identify_show,
    run_identify_show_effective,
    run_identify_stats,
    run_inventory_scan,
)
from mams.config import load_config


def _write_config(tmp_path: Path, categories: dict[str, str]) -> Path:
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


# --- parser -----------------------------------------------------------------


def test_parser_accepts_evaluate_flags() -> None:
    args = build_parser().parse_args(["identify", "evaluate", "--json"])
    assert args.command == "identify"
    assert args.identify_command == "evaluate"
    assert args.json is True


def test_parser_accepts_list_flags() -> None:
    args = build_parser().parse_args(
        ["identify", "list", "--type", "movie", "--confidence", "high", "--category", "movies", "--limit", "5", "--json"]
    )
    assert args.identify_command == "list"
    assert args.candidate_type == "MOVIE"
    assert args.confidence == "HIGH"
    assert args.category == "movies"
    assert args.limit == 5
    assert args.json is True


def test_parser_defaults_for_list() -> None:
    args = build_parser().parse_args(["identify", "list"])
    assert args.candidate_type is None
    assert args.confidence is None
    assert args.category is None
    assert args.limit is None
    assert args.json is False


def test_parser_accepts_stats_flags() -> None:
    args = build_parser().parse_args(["identify", "stats", "--json"])
    assert args.identify_command == "stats"
    assert args.json is True


def test_parser_accepts_show_flags() -> None:
    args = build_parser().parse_args(["identify", "show", "42", "--json"])
    assert args.identify_command == "show"
    assert args.candidate_id == 42
    assert args.json is True


# --- evaluate -----------------------------------------------------------------


def test_run_identify_evaluate_text_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _seed_movie(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    result = run_identify_evaluate(config)

    assert result.created == 1
    out = capsys.readouterr().out
    assert "Created:" in out


def test_run_identify_evaluate_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _seed_movie(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    result = run_identify_evaluate(config, json_output=True)

    assert result.to_dict()["created"] == result.created
    assert capsys.readouterr().out.strip() != ""


def test_run_identify_evaluate_repeated_run_creates_no_duplicates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = _seed_movie(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    first = run_identify_evaluate(config)
    second = run_identify_evaluate(config)

    assert second.created == 0
    assert second.unchanged == first.created


# --- list -----------------------------------------------------------------------


def test_run_identify_list_text_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _seed_movie(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_identify_evaluate(config)
    capsys.readouterr()

    records = run_identify_list(config, candidate_type="MOVIE")

    assert len(records) == 1
    assert records[0].candidate_type == "MOVIE"
    out = capsys.readouterr().out
    assert "HIGH" in out
    assert "MOVIE" in out
    assert "movies" in out
    assert "Alien (1979)" in out
    assert "not confirmed identities" in out


def test_run_identify_list_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _seed_movie(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_identify_evaluate(config)
    capsys.readouterr()

    records = run_identify_list(config, json_output=True)

    assert records[0].to_dict()["candidate_type"] == "MOVIE"
    assert capsys.readouterr().out.strip() != ""


def test_run_identify_list_filters_by_confidence_and_category(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = _seed_movie(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_identify_evaluate(config)
    capsys.readouterr()

    high = run_identify_list(config, confidence="HIGH", category="movies")
    low = run_identify_list(config, confidence="LOW")

    assert any(r.candidate_type == "MOVIE" for r in high)
    assert low == []


def test_run_identify_list_empty_result_handled_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = _seed_movie(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_identify_evaluate(config)
    capsys.readouterr()

    records = run_identify_list(config, category="does-not-exist")

    assert records == []
    assert "No matching candidates" in capsys.readouterr().out


def test_run_identify_list_on_never_evaluated_database_is_empty_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = tmp_path / "Movies"
    movies_root.mkdir()
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))

    records = run_identify_list(config)

    assert records == []


# --- stats --------------------------------------------------------------------


def test_run_identify_stats_text_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _seed_movie(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_identify_evaluate(config)
    capsys.readouterr()

    stats = run_identify_stats(config)

    assert stats.type_counts.get("MOVIE") == 1
    out = capsys.readouterr().out
    assert "Total:" in out


def test_run_identify_stats_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _seed_movie(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_identify_evaluate(config)
    capsys.readouterr()

    stats = run_identify_stats(config, json_output=True)

    assert stats.to_dict()["total_count"] == stats.total_count
    assert capsys.readouterr().out.strip() != ""


def test_run_identify_stats_on_empty_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = tmp_path / "Movies"
    movies_root.mkdir()
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))

    stats = run_identify_stats(config)

    assert stats.total_count == 0


# --- show -----------------------------------------------------------------------


def test_run_identify_show_text_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _seed_movie(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_identify_evaluate(config)
    capsys.readouterr()
    [target] = run_identify_list(config)
    capsys.readouterr()

    record = run_identify_show(config, target.id)

    assert record is not None
    out = capsys.readouterr().out
    assert f"Candidate #{target.id}" in out
    assert "not a confirmed identity" in out
    assert "MOVIE" in out
    assert "Alien (1979)" in out


def test_run_identify_show_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _seed_movie(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_identify_evaluate(config)
    capsys.readouterr()
    [target] = run_identify_list(config)
    capsys.readouterr()

    record = run_identify_show(config, target.id, json_output=True)

    assert record is not None
    assert record.to_dict()["id"] == target.id
    assert capsys.readouterr().out.strip() != ""


def test_run_identify_show_unknown_id_is_a_clear_non_destructive_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = tmp_path / "Movies"
    movies_root.mkdir()
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))

    record = run_identify_show(config, 9999)

    assert record is None
    assert "not found" in capsys.readouterr().out


# --- override / clear-override / show-effective ------------------------------


def _scan_and_evaluate(tmp_path: Path, name: str = "Alien.mkv") -> tuple[object, int]:
    movies_root = _seed_movie(tmp_path, name=name)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_identify_evaluate(config)
    records = run_identify_list(config)
    return config, records[0].media_file_id


def test_parser_accepts_override_flags() -> None:
    args = build_parser().parse_args(
        ["identify", "override", "1", "--type", "movie", "--title", "Alien", "--year", "1979", "--reason", "no year in filename"]
    )
    assert args.candidate_type == "MOVIE"
    assert args.title == "Alien"
    assert args.year == 1979
    assert args.reason == "no year in filename"

    episode_args = build_parser().parse_args(
        ["identify", "override", "1", "--type", "episode", "--series", "Carnivale", "--season", "1", "--episode", "2"]
    )
    assert episode_args.candidate_type == "EPISODE"
    assert episode_args.series_title == "Carnivale"
    assert episode_args.season_number == 1
    assert episode_args.episode_number == 2

    assert build_parser().parse_args(["identify", "clear-override", "1", "--json"]).json is True
    assert build_parser().parse_args(["identify", "show-effective", "1"]).media_file_id == 1


def test_override_movie_without_year_reaches_resolvable_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, media_file_id = _scan_and_evaluate(tmp_path)
    capsys.readouterr()

    override = run_identify_override(config, media_file_id, candidate_type="MOVIE", title="Alien", year=1979)

    assert override is not None
    assert override.title == "Alien"
    assert override.year == 1979
    out = capsys.readouterr().out
    assert "Override created" in out


def test_override_unknown_media_file_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = load_config(_write_config(tmp_path, {"movies": str(tmp_path / "Movies")}))
    result = run_identify_override(config, 9999, candidate_type="MOVIE", title="Alien")
    assert result is None
    assert "No media file with id 9999" in capsys.readouterr().out


def test_override_movie_missing_title_is_a_clear_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, media_file_id = _scan_and_evaluate(tmp_path)
    capsys.readouterr()

    result = run_identify_override(config, media_file_id, candidate_type="MOVIE")

    assert result is None
    assert "requires a non-empty title" in capsys.readouterr().out


def test_show_effective_reflects_override_then_reverts_after_clear(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, media_file_id = _scan_and_evaluate(tmp_path)
    run_identify_override(config, media_file_id, candidate_type="MOVIE", title="Alien", year=1979)
    capsys.readouterr()

    effective = run_identify_show_effective(config, media_file_id)
    assert effective is not None
    assert effective.source == "MANUAL_OVERRIDE"
    assert "MANUAL_OVERRIDE" in capsys.readouterr().out

    run_identify_clear_override(config, media_file_id)
    capsys.readouterr()

    reverted = run_identify_show_effective(config, media_file_id)
    assert reverted is not None
    assert reverted.source == "PARSED"


def test_clear_override_noop_when_none_active(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, media_file_id = _scan_and_evaluate(tmp_path)
    capsys.readouterr()

    result = run_identify_clear_override(config, media_file_id)

    assert result is None
    assert "nothing to clear" in capsys.readouterr().out


def test_show_effective_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, media_file_id = _scan_and_evaluate(tmp_path)
    run_identify_override(config, media_file_id, candidate_type="MOVIE", title="Alien", year=1979)
    capsys.readouterr()

    effective = run_identify_show_effective(config, media_file_id, json_output=True)

    assert effective is not None
    out = capsys.readouterr().out
    assert '"source": "MANUAL_OVERRIDE"' in out
