"""CLI tests for mams inventory list/stats/find/diff.

Seeds real data through run_inventory_scan() (the actual CLI scan path)
rather than calling the repository layer directly, so these exercise the
full CLI wiring, not just the underlying query functions (already covered
in test_inventory_queries.py / test_scan_changes.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from mams.cli import (
    build_parser,
    run_inventory_diff,
    run_inventory_find,
    run_inventory_list,
    run_inventory_scan,
    run_inventory_stats,
)
from mams.config import load_config
from mams.db import connect
from mams.mediainfo import MediaInfo, MediaInfoOutcome, MediaInfoProvider


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


def _movies_root(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / "Movies"
    root.mkdir(exist_ok=True)
    for name in names:
        (root / name).write_bytes(b"\0" * 10)
    return root


def _report_path(tmp_path: Path) -> str:
    return str(tmp_path / "reports" / "library.json")


def _d(value: object) -> dict[str, Any]:
    """Narrow a dict[str, object] value's nested `object` entries to plain
    dicts for indexing in assertions, without repeating a cast() at every
    call site."""
    return cast("dict[str, Any]", value)


# --- parser -----------------------------------------------------------------


def test_parser_accepts_list_flags() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "inventory", "list", "--category", "movies", "--state", "active",
            "--layout", "movie_flat", "--metadata-error", "--limit", "5", "--json",
        ]
    )

    assert args.inventory_command == "list"
    assert args.category == "movies"
    assert args.state == "ACTIVE"
    assert args.layout == "movie_flat"
    assert args.metadata_error is True
    assert args.limit == 5
    assert args.json is True


def test_parser_defaults_for_list() -> None:
    parser = build_parser()

    args = parser.parse_args(["inventory", "list"])

    assert args.category is None
    assert args.state is None
    assert args.layout is None
    assert args.metadata_error is None
    assert args.limit is None
    assert args.json is False


def test_parser_accepts_stats_flags() -> None:
    parser = build_parser()

    args = parser.parse_args(["inventory", "stats", "--json"])

    assert args.inventory_command == "stats"
    assert args.json is True


def test_parser_accepts_find_query_and_flags() -> None:
    parser = build_parser()

    args = parser.parse_args(
        ["inventory", "find", "district", "--category", "movies", "--state", "missing", "--limit", "3"]
    )

    assert args.inventory_command == "find"
    assert args.query == "district"
    assert args.category == "movies"
    assert args.state == "MISSING"
    assert args.limit == 3


def test_parser_accepts_diff_single_scan_flags() -> None:
    parser = build_parser()

    args = parser.parse_args(["inventory", "diff", "--scan", "3", "--type", "updated", "--category", "movies", "--json"])

    assert args.inventory_command == "diff"
    assert args.scan == 3
    assert args.change_type == "UPDATED"
    assert args.category == "movies"
    assert args.json is True


def test_parser_accepts_diff_range_flags() -> None:
    parser = build_parser()

    args = parser.parse_args(["inventory", "diff", "--from-scan", "1", "--to-scan", "3"])

    assert args.from_scan == 1
    assert args.to_scan == 3


def test_parser_defaults_for_diff() -> None:
    parser = build_parser()

    args = parser.parse_args(["inventory", "diff"])

    assert args.scan is None
    assert args.from_scan is None
    assert args.to_scan is None
    assert args.change_type is None


# --- list ---------------------------------------------------------------


def test_run_inventory_list_text_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _movies_root(tmp_path, "Movie A (2001).mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    records = run_inventory_list(config)

    assert len(records) == 1
    assert records[0].filename == "Movie A (2001).mkv"
    out = capsys.readouterr().out
    assert "Movie A" in out


def test_run_inventory_list_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _movies_root(tmp_path, "Movie A (2001).mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    records = run_inventory_list(config, json_output=True)

    assert records[0].to_dict()["filename"] == "Movie A (2001).mkv"
    assert capsys.readouterr().out.strip() != ""


def test_run_inventory_list_filters_by_state(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _movies_root(tmp_path, "A.mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    (movies_root / "A.mkv").unlink()
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    active = run_inventory_list(config, state="ACTIVE")
    missing = run_inventory_list(config, state="MISSING")

    assert active == []
    assert len(missing) == 1
    assert missing[0].state == "MISSING"


def test_run_inventory_list_metadata_error_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = _movies_root(tmp_path, "Good.mkv", "Bad.mkv")

    def _fake_probe(self: MediaInfoProvider, path: Path) -> MediaInfoOutcome:
        if path.name == "Bad.mkv":
            return MediaInfoOutcome(media_info=None, error="mediainfo failed")
        info = MediaInfo(
            container="Matroska", duration_seconds=1.0, overall_bitrate=1,
            video_tracks=(), audio_tracks=(), subtitle_tracks=(),
        )
        return MediaInfoOutcome(media_info=info, error=None)

    monkeypatch.setattr(MediaInfoProvider, "probe", _fake_probe)

    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path), metadata=True)
    capsys.readouterr()

    errors_only = run_inventory_list(config, metadata_error=True)

    assert [r.filename for r in errors_only] == ["Bad.mkv"]


def test_run_inventory_list_empty_result_handled_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = _movies_root(tmp_path, "A.mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    records = run_inventory_list(config, category="does-not-exist")

    assert records == []
    assert "No matching files" in capsys.readouterr().out


def test_run_inventory_list_on_never_scanned_database_is_empty_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = tmp_path / "Movies"
    movies_root.mkdir()
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))

    records = run_inventory_list(config)

    assert records == []


# --- stats ----------------------------------------------------------------


def test_run_inventory_stats_text_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _movies_root(tmp_path, "A.mkv", "B.mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    stats = run_inventory_stats(config)

    assert stats.active_file_count == 2
    out = capsys.readouterr().out
    assert "MAMS Inventory Statistics" in out
    assert "movies" in out


def test_run_inventory_stats_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _movies_root(tmp_path, "A.mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    stats = run_inventory_stats(config, json_output=True)

    payload = stats.to_dict()
    assert payload["active_file_count"] == 1
    assert capsys.readouterr().out.strip() != ""


def test_run_inventory_stats_includes_most_recent_scan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = _movies_root(tmp_path, "A.mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    stats = run_inventory_stats(config)

    assert stats.most_recent_scan is not None
    assert stats.most_recent_scan.status == "COMPLETE"


# --- find -------------------------------------------------------------------


def test_run_inventory_find_text_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _movies_root(tmp_path, "District 9 (2009).mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    records = run_inventory_find(config, "district")

    assert len(records) == 1
    assert "District 9" in capsys.readouterr().out


def test_run_inventory_find_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _movies_root(tmp_path, "District 9 (2009).mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    records = run_inventory_find(config, "DISTRICT", json_output=True)

    assert records[0].to_dict()["filename"] == "District 9 (2009).mkv"
    assert capsys.readouterr().out.strip() != ""


def test_run_inventory_find_empty_result_handled_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = _movies_root(tmp_path, "A.mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    records = run_inventory_find(config, "nonexistent-title-xyz")

    assert records == []
    assert "No matching files" in capsys.readouterr().out


# --- diff ---------------------------------------------------------------


def test_run_inventory_diff_defaults_to_latest_completed_scan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = _movies_root(tmp_path, "A.mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    (movies_root / "B.mkv").write_bytes(b"\0" * 5)
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    result = run_inventory_diff(config)

    assert result is not None
    assert result["mode"] == "single"
    assert _d(result["scan"])["status"] == "COMPLETE"
    with connect(config.database_path) as connection:
        latest_id = connection.execute("SELECT MAX(id) FROM scan_runs").fetchone()[0]
    assert _d(result["scan"])["id"] == latest_id
    assert cast("dict[str, int]", result["counts_by_type"]).get("ADDED") == 1  # only B.mkv is new in the latest scan


def test_run_inventory_diff_explicit_scan_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = _movies_root(tmp_path, "A.mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    (movies_root / "B.mkv").write_bytes(b"\0" * 5)
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    result = run_inventory_diff(config, scan=1)

    assert result is not None
    assert _d(result["scan"])["id"] == 1
    assert cast("dict[str, int]", result["counts_by_type"]).get("ADDED") == 1  # A.mkv, from the first scan only


def test_run_inventory_diff_scan_range_semantics(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _movies_root(tmp_path, "A.mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))  # scan 1: A added
    (movies_root / "B.mkv").write_bytes(b"\0" * 5)
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))  # scan 2: B added
    (movies_root / "C.mkv").write_bytes(b"\0" * 5)
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))  # scan 3: C added
    capsys.readouterr()

    result = run_inventory_diff(config, from_scan=1, to_scan=3)

    assert result is not None
    assert result["mode"] == "range"
    assert result["total_changes"] == 2  # B and C, not A (from_scan=1 is exclusive)
    paths = {_d(c)["absolute_path"] for c in cast("list[object]", result["changes"])}
    assert str(movies_root / "A.mkv") not in paths
    assert str(movies_root / "B.mkv") in paths
    assert str(movies_root / "C.mkv") in paths


def test_run_inventory_diff_invalid_scan_id_is_a_clear_non_destructive_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = _movies_root(tmp_path, "A.mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    with connect(config.database_path) as connection:
        before_scan_runs = connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
        before_changes = connection.execute("SELECT COUNT(*) FROM scan_changes").fetchone()[0]

    result = run_inventory_diff(config, scan=9999)

    assert result is None
    assert "9999" in capsys.readouterr().out

    with connect(config.database_path) as connection:
        after_scan_runs = connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
        after_changes = connection.execute("SELECT COUNT(*) FROM scan_changes").fetchone()[0]
    assert (before_scan_runs, before_changes) == (after_scan_runs, after_changes)


def test_run_inventory_diff_scan_and_range_together_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = _movies_root(tmp_path, "A.mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    result = run_inventory_diff(config, scan=1, from_scan=1, to_scan=1)

    assert result is None
    assert "--scan" in capsys.readouterr().out


def test_run_inventory_diff_from_scan_without_to_scan_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = _movies_root(tmp_path, "A.mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    result = run_inventory_diff(config, from_scan=1)

    assert result is None
    assert "--from-scan" in capsys.readouterr().out


def test_run_inventory_diff_empty_when_no_scans_yet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = tmp_path / "Movies"
    movies_root.mkdir()
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))

    result = run_inventory_diff(config)

    assert result is not None
    assert result["scan"] is None
    assert result["total_changes"] == 0
    assert result["changes"] == []
    assert "No completed scans yet" in capsys.readouterr().out


def test_run_inventory_diff_text_output_includes_change_type_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root = _movies_root(tmp_path, "A.mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    run_inventory_diff(config)

    out = capsys.readouterr().out
    assert "MAMS Inventory Diff" in out
    assert "ADDED: 1" in out


def test_run_inventory_diff_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _movies_root(tmp_path, "A.mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    result = run_inventory_diff(config, json_output=True)

    assert result is not None
    assert capsys.readouterr().out.strip() != ""


def test_run_inventory_diff_type_filter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root = _movies_root(tmp_path, "A.mkv")
    config = load_config(_write_config(tmp_path, {"movies": str(movies_root)}))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    (movies_root / "A.mkv").unlink()
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    capsys.readouterr()

    result = run_inventory_diff(config, scan=2, change_type="MISSING")

    assert result is not None
    assert all(_d(c)["change_type"] == "MISSING" for c in cast("list[object]", result["changes"]))
    assert result["total_changes"] == 1
