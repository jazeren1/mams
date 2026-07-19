from __future__ import annotations

import json
from pathlib import Path

import yaml

from mams.cli import build_parser, run_inventory_scan
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


def test_parser_accepts_inventory_scan_flags() -> None:
    parser = build_parser()

    args = parser.parse_args(["inventory", "scan", "--json", "--output", "reports/custom.json"])

    assert args.command == "inventory"
    assert args.inventory_command == "scan"
    assert args.json is True
    assert args.output == "reports/custom.json"


def test_parser_defaults_for_inventory_scan() -> None:
    parser = build_parser()

    args = parser.parse_args(["inventory", "scan"])

    assert args.json is False
    assert args.output == "reports/library.json"


def test_run_inventory_scan_writes_json_and_summary_reports(tmp_path: Path) -> None:
    movies_root = tmp_path / "Movies"
    movies_root.mkdir()
    (movies_root / "Movie (2001).mkv").write_bytes(b"\0" * 10)

    config_path = _write_config(tmp_path, {"movies": str(movies_root)})
    config = load_config(config_path)

    output_path = tmp_path / "reports" / "library.json"
    report = run_inventory_scan(config, json_output=False, output=str(output_path))

    assert report.file_count == 1
    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["file_count"] == 1

    summary_path = output_path.with_name("library-summary.txt")
    assert summary_path.exists()
    assert "files: 1" in summary_path.read_text(encoding="utf-8")


def test_run_inventory_scan_never_writes_into_scanned_categories(tmp_path: Path) -> None:
    movies_root = tmp_path / "Movies"
    movies_root.mkdir()
    (movies_root / "Movie (2001).mkv").write_bytes(b"\0" * 10)
    before = sorted(p.name for p in movies_root.iterdir())

    config_path = _write_config(tmp_path, {"movies": str(movies_root)})
    config = load_config(config_path)

    run_inventory_scan(config, json_output=False, output=str(tmp_path / "reports" / "library.json"))

    after = sorted(p.name for p in movies_root.iterdir())
    assert before == after
