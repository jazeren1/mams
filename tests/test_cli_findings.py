"""CLI tests for mams findings evaluate/list/stats/show.

Seeds real data through run_inventory_scan() (the actual CLI scan path),
same approach as test_cli_inventory_queries.py, so these exercise the full
findings CLI wiring end to end: scan -> evaluate -> list/stats/show.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mams.cli import (
    build_parser,
    run_findings_evaluate,
    run_findings_list,
    run_findings_show,
    run_findings_stats,
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


def _touch(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)


def _report_path(tmp_path: Path) -> str:
    return str(tmp_path / "reports" / "library.json")


def _seed_zero_byte_file(tmp_path: Path, name: str = "Zero.mkv") -> Path:
    movies_root = tmp_path / "Movies"
    _touch(movies_root / name, 0)
    return movies_root


# --- parser -----------------------------------------------------------------


def test_parser_accepts_evaluate_flags() -> None:
    args = build_parser().parse_args(["findings", "evaluate", "--json"])
    assert args.command == "findings"
    assert args.findings_command == "evaluate"
    assert args.json is True


def test_parser_accepts_list_flags() -> None:
    args = build_parser().parse_args(
        [
            "findings", "list", "--status", "active", "--severity", "error",
            "--rule", "zero_byte_file", "--category", "movies", "--limit", "5", "--json",
        ]
    )
    assert args.findings_command == "list"
    assert args.status == "ACTIVE"
    assert args.severity == "ERROR"
    assert args.rule_code == "zero_byte_file"
    assert args.category == "movies"
    assert args.limit == 5
    assert args.json is True


def test_parser_defaults_for_list() -> None:
    args = build_parser().parse_args(["findings", "list"])
    assert args.status is None
    assert args.severity is None
    assert args.rule_code is None
    assert args.category is None
    assert args.limit is None
    assert args.json is False


def test_parser_accepts_stats_flags() -> None:
    args = build_parser().parse_args(["findings", "stats", "--json"])
    assert args.findings_command == "stats"
    assert args.json is True


def test_parser_accepts_show_flags() -> None:
    args = build_parser().parse_args(["findings", "show", "42", "--json"])
    assert args.findings_command == "show"
    assert args.finding_id == 42
    assert args.json is True


# --- evaluate -----------------------------------------------------------------


def test_run_findings_evaluate_text_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _seed_zero_byte_file(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    result = run_findings_evaluate(config)

    assert result.created >= 1
    out = capsys.readouterr().out
    assert "Created:" in out


def test_run_findings_evaluate_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _seed_zero_byte_file(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    result = run_findings_evaluate(config, json_output=True)

    assert result.to_dict()["created"] == result.created
    assert capsys.readouterr().out.strip() != ""


def test_run_findings_evaluate_repeated_run_creates_no_duplicates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = _seed_zero_byte_file(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    first = run_findings_evaluate(config)
    second = run_findings_evaluate(config)

    assert second.created == 0
    assert second.unchanged == first.created


# --- list -----------------------------------------------------------------------


def test_run_findings_list_text_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _seed_zero_byte_file(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_findings_evaluate(config)
    capsys.readouterr()

    records = run_findings_list(config, rule_code="zero_byte_file")

    assert len(records) == 1
    assert records[0].rule_code == "zero_byte_file"
    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "zero_byte_file" in out
    assert "movies" in out
    assert "Zero.mkv" in out


def test_run_findings_list_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _seed_zero_byte_file(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_findings_evaluate(config)
    capsys.readouterr()

    records = run_findings_list(config, rule_code="zero_byte_file", json_output=True)

    assert records[0].to_dict()["rule_code"] == "zero_byte_file"
    assert capsys.readouterr().out.strip() != ""


def test_run_findings_list_filters_by_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _seed_zero_byte_file(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_findings_evaluate(config)
    (movies_root / "Zero.mkv").write_bytes(b"\0" * 50_000_000)
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_findings_evaluate(config)
    capsys.readouterr()

    resolved = run_findings_list(config, rule_code="zero_byte_file", status="RESOLVED")
    active = run_findings_list(config, rule_code="zero_byte_file", status="ACTIVE")

    assert len(resolved) == 1
    assert active == []


def test_run_findings_list_filters_by_severity_and_category(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = _seed_zero_byte_file(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_findings_evaluate(config)
    capsys.readouterr()

    errors = run_findings_list(config, severity="ERROR", category="movies")
    infos = run_findings_list(config, severity="INFO")

    assert any(r.rule_code == "zero_byte_file" for r in errors)
    assert infos == []


def test_run_findings_list_empty_result_handled_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = _seed_zero_byte_file(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_findings_evaluate(config)
    capsys.readouterr()

    records = run_findings_list(config, category="does-not-exist")

    assert records == []
    assert "No matching findings" in capsys.readouterr().out


def test_run_findings_list_on_never_evaluated_database_is_empty_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = tmp_path / "Movies"
    movies_root.mkdir()
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))

    records = run_findings_list(config)

    assert records == []


# --- stats --------------------------------------------------------------------


def test_run_findings_stats_text_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _seed_zero_byte_file(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_findings_evaluate(config)
    capsys.readouterr()

    stats = run_findings_stats(config)

    assert stats.rule_counts.get("zero_byte_file") == 1
    out = capsys.readouterr().out
    assert "Active:" in out


def test_run_findings_stats_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _seed_zero_byte_file(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_findings_evaluate(config)
    capsys.readouterr()

    stats = run_findings_stats(config, json_output=True)

    assert stats.to_dict()["active_count"] == stats.active_count
    assert capsys.readouterr().out.strip() != ""


def test_run_findings_stats_on_empty_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = tmp_path / "Movies"
    movies_root.mkdir()
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))

    stats = run_findings_stats(config)

    assert stats.total_count == 0


# --- show -----------------------------------------------------------------------


def test_run_findings_show_text_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _seed_zero_byte_file(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_findings_evaluate(config)
    capsys.readouterr()
    [target] = run_findings_list(config, rule_code="zero_byte_file")
    capsys.readouterr()

    record = run_findings_show(config, target.id)

    assert record is not None
    out = capsys.readouterr().out
    assert f"Finding #{target.id}" in out
    assert "zero_byte_file" in out
    assert "Zero.mkv" in out
    assert "Evidence:" in out
    assert "Recommendation:" in out
    assert "First detected:" in out


def test_run_findings_show_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _seed_zero_byte_file(tmp_path)
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_findings_evaluate(config)
    capsys.readouterr()
    [target] = run_findings_list(config, rule_code="zero_byte_file")
    capsys.readouterr()

    record = run_findings_show(config, target.id, json_output=True)

    assert record is not None
    assert record.to_dict()["id"] == target.id
    assert capsys.readouterr().out.strip() != ""


def test_run_findings_show_unknown_id_is_a_clear_non_destructive_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = tmp_path / "Movies"
    movies_root.mkdir()
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))

    record = run_findings_show(config, 9999)

    assert record is None
    assert "not found" in capsys.readouterr().out
