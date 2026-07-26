"""Tests for `mams inventory scan --category CATEGORY` (Milestone 8.2).

Covers the CLI contract, filesystem-discovery scoping, metadata-probe
scoping, reconciliation isolation, scan-run scope persistence, and report
separation described in the milestone brief. Full-scan (no `--category`)
regression coverage lives in test_cli_inventory.py; this file only adds
what's new.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from mams.cli import (
    InventoryScanScopeError,
    UnknownCategoryError,
    build_parser,
    main,
    run_inventory_scan,
)
from mams.config import load_config
from mams.db import connect
from mams.mediainfo import MediaInfo, MediaInfoOutcome, MediaInfoProvider


def _write_config(
    tmp_path: Path, *, nas_categories: dict[str, str], incoming_roots: list[str] | None = None
) -> Path:
    config_path = tmp_path / "config.yaml"
    raw: dict[str, object] = {
        "project": {
            "name": "MAMS Test",
            "database_path": str(tmp_path / "database" / "mams.db"),
            "log_level": "INFO",
            "dry_run": True,
        },
        "nas": {"categories": nas_categories},
    }
    if incoming_roots:
        raw["ingest"] = {"incoming_roots": incoming_roots}
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return config_path


def _make_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Movies / TV / Incoming roots, each with one file, none overlapping."""
    movies_root = tmp_path / "Movies"
    movies_root.mkdir()
    (movies_root / "Movie (2001).mkv").write_bytes(b"\0" * 10)

    tv_root = tmp_path / "TV"
    tv_root.mkdir()
    (tv_root / "Show" / "Season 01").mkdir(parents=True)
    (tv_root / "Show" / "Season 01" / "Show - S01E01.mkv").write_bytes(b"\0" * 10)

    incoming_root = tmp_path / "Incoming"
    incoming_root.mkdir()
    (incoming_root / "Ripped Movie (2020).mkv").write_bytes(b"\0" * 20)

    return movies_root, tv_root, incoming_root


# --- CLI: parsing -------------------------------------------------------


def test_parser_accepts_category_flag() -> None:
    parser = build_parser()
    args = parser.parse_args(["inventory", "scan", "--category", "incoming"])
    assert args.category == "incoming"


def test_parser_accepts_category_with_metadata() -> None:
    parser = build_parser()
    args = parser.parse_args(["inventory", "scan", "--category", "incoming", "--metadata"])
    assert args.category == "incoming"
    assert args.metadata is True


def test_parser_defaults_category_to_none() -> None:
    parser = build_parser()
    args = parser.parse_args(["inventory", "scan"])
    assert args.category is None


def test_parser_rejects_repeated_category_flag() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["inventory", "scan", "--category", "movies", "--category", "tv"])
    assert exc_info.value.code != 0


def test_help_text_documents_category_option() -> None:
    parser = build_parser()
    scan_parser = None
    for action in parser._subparsers._group_actions:  # type: ignore[attr-defined]
        if "inventory" in action.choices:
            inventory_parser = action.choices["inventory"]
            for sub_action in inventory_parser._subparsers._group_actions:  # type: ignore[attr-defined]
                if "scan" in sub_action.choices:
                    scan_parser = sub_action.choices["scan"]
    assert scan_parser is not None
    help_text = " ".join(scan_parser.format_help().split())
    assert "--category" in help_text
    assert "one configured inventory category" in help_text


# --- CLI: unknown category / error contract ------------------------------


def test_run_inventory_scan_unknown_category_raises_with_valid_list(tmp_path: Path) -> None:
    movies_root, _, _ = _make_tree(tmp_path)
    config_path = _write_config(tmp_path, nas_categories={"movies": str(movies_root)})
    config = load_config(config_path)

    with pytest.raises(UnknownCategoryError) as exc_info:
        run_inventory_scan(
            config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="tv"
        )
    assert "movies" in str(exc_info.value)


def test_unknown_category_is_an_inventory_scan_scope_error(tmp_path: Path) -> None:
    movies_root, _, _ = _make_tree(tmp_path)
    config_path = _write_config(tmp_path, nas_categories={"movies": str(movies_root)})
    config = load_config(config_path)

    with pytest.raises(InventoryScanScopeError):
        run_inventory_scan(
            config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="tv"
        )


def test_main_exits_non_zero_for_unknown_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    movies_root, _, _ = _make_tree(tmp_path)
    config_path = _write_config(tmp_path, nas_categories={"movies": str(movies_root)})

    monkeypatch.setattr(
        "sys.argv", ["mams", "--config", str(config_path), "inventory", "scan", "--category", "tv"]
    )
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "movies" in captured.out


def test_scoped_scan_with_default_output_never_writes_the_full_report_path(tmp_path: Path) -> None:
    """A bare `--category CATEGORY` (no explicit `--output`) must resolve to
    the scoped default filename, never to `reports/library.json` -- the
    CLI parser fills in `DEFAULT_INVENTORY_REPORT` as `args.output` whether
    or not the user typed `--output`, so this is the only place that
    distinction can be enforced."""
    movies_root, _, _ = _make_tree(tmp_path)
    config_path = _write_config(tmp_path, nas_categories={"movies": str(movies_root)})
    config = load_config(config_path)

    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        run_inventory_scan(config, json_output=False, output="reports/library.json", category="movies")
        assert not (tmp_path / "reports" / "library.json").exists()
        assert (tmp_path / "reports" / "library-movies.json").exists()
    finally:
        os.chdir(cwd)


# --- CLI: scoped text/JSON output identify the scan as scoped ------------


def test_scoped_scan_text_output_identifies_scope(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root, _, incoming_root = _make_tree(tmp_path)
    config_path = _write_config(
        tmp_path, nas_categories={"movies": str(movies_root)}, incoming_roots=[str(incoming_root)]
    )
    config = load_config(config_path)

    run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="incoming"
    )

    captured = capsys.readouterr()
    assert "Scope: incoming" in captured.out
    assert "Scoped scan complete." in captured.out
    assert "Other categories were not scanned or reconciled." in captured.out


def test_scoped_scan_json_output_identifies_scope(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    movies_root, _, incoming_root = _make_tree(tmp_path)
    config_path = _write_config(
        tmp_path, nas_categories={"movies": str(movies_root)}, incoming_roots=[str(incoming_root)]
    )
    config = load_config(config_path)

    run_inventory_scan(
        config,
        json_output=True,
        output=str(tmp_path / "reports" / "library.json"),
        category="incoming",
    )

    captured = capsys.readouterr()
    assert '"scan_scope": "CATEGORY"' in captured.out
    assert '"scope_category": "incoming"' in captured.out


# --- Filesystem discovery scoping ----------------------------------------


def test_incoming_only_scan_walks_only_incoming(tmp_path: Path) -> None:
    movies_root, tv_root, incoming_root = _make_tree(tmp_path)
    config_path = _write_config(
        tmp_path,
        nas_categories={"movies": str(movies_root), "tv": str(tv_root)},
        incoming_roots=[str(incoming_root)],
    )
    config = load_config(config_path)

    report = run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="incoming"
    )

    assert [c.category for c in report.categories] == ["incoming"]
    assert report.categories[0].file_count == 1


def test_movies_only_scan_walks_only_movies(tmp_path: Path) -> None:
    movies_root, tv_root, incoming_root = _make_tree(tmp_path)
    config_path = _write_config(
        tmp_path,
        nas_categories={"movies": str(movies_root), "tv": str(tv_root)},
        incoming_roots=[str(incoming_root)],
    )
    config = load_config(config_path)

    report = run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="movies"
    )

    assert [c.category for c in report.categories] == ["movies"]


def test_full_scan_still_walks_every_configured_category(tmp_path: Path) -> None:
    movies_root, tv_root, incoming_root = _make_tree(tmp_path)
    config_path = _write_config(
        tmp_path,
        nas_categories={"movies": str(movies_root), "tv": str(tv_root)},
        incoming_roots=[str(incoming_root)],
    )
    config = load_config(config_path)

    report = run_inventory_scan(config, json_output=False, output=str(tmp_path / "reports" / "library.json"))

    assert sorted(c.category for c in report.categories) == ["incoming", "movies", "tv"]


def test_incoming_only_scan_succeeds_while_nas_root_unavailable(tmp_path: Path) -> None:
    incoming_root = tmp_path / "Incoming"
    incoming_root.mkdir()
    (incoming_root / "Ripped Movie (2020).mkv").write_bytes(b"\0" * 20)

    unmounted_movies_root = tmp_path / "does-not-exist" / "Movies"

    config_path = _write_config(
        tmp_path,
        nas_categories={"movies": str(unmounted_movies_root)},
        incoming_roots=[str(incoming_root)],
    )
    config = load_config(config_path)

    report = run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="incoming"
    )

    assert report.categories[0].exists is True
    assert report.categories[0].file_count == 1


def test_unavailable_selected_root_is_reported_as_missing(tmp_path: Path) -> None:
    unmounted_incoming_root = tmp_path / "does-not-exist" / "Incoming"
    config_path = _write_config(
        tmp_path, nas_categories={"movies": str(tmp_path / "Movies")}, incoming_roots=[str(unmounted_incoming_root)]
    )
    config = load_config(config_path)

    report = run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="incoming"
    )

    assert report.categories == (report.categories[0],)
    assert report.categories[0].exists is False
    assert report.categories[0].file_count == 0


# --- Metadata scoping ------------------------------------------------------


def test_incoming_metadata_scan_probes_only_incoming_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    movies_root, tv_root, incoming_root = _make_tree(tmp_path)
    config_path = _write_config(
        tmp_path,
        nas_categories={"movies": str(movies_root), "tv": str(tv_root)},
        incoming_roots=[str(incoming_root)],
    )
    config = load_config(config_path)

    probed_paths: list[Path] = []
    fake_info = MediaInfo(
        container="Matroska", duration_seconds=100.0, overall_bitrate=5000,
        video_tracks=(), audio_tracks=(), subtitle_tracks=(),
    )

    def _fake_probe(self: MediaInfoProvider, path: Path) -> MediaInfoOutcome:
        probed_paths.append(path)
        return MediaInfoOutcome(media_info=fake_info, error=None)

    monkeypatch.setattr(MediaInfoProvider, "probe", _fake_probe)

    run_inventory_scan(
        config,
        json_output=False,
        output=str(tmp_path / "reports" / "library.json"),
        category="incoming",
        metadata=True,
    )

    assert len(probed_paths) == 1
    assert incoming_root in probed_paths[0].parents or probed_paths[0].parent == incoming_root


def test_two_incoming_files_produce_exactly_two_probe_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming_root = tmp_path / "Incoming"
    incoming_root.mkdir()
    (incoming_root / "Disc1.mkv").write_bytes(b"\0" * 10)
    (incoming_root / "Disc2.mkv").write_bytes(b"\0" * 10)

    config_path = _write_config(
        tmp_path, nas_categories={"movies": str(tmp_path / "Movies")}, incoming_roots=[str(incoming_root)]
    )
    config = load_config(config_path)

    call_count = 0
    fake_info = MediaInfo(
        container="Matroska", duration_seconds=100.0, overall_bitrate=5000,
        video_tracks=(), audio_tracks=(), subtitle_tracks=(),
    )

    def _fake_probe(self: MediaInfoProvider, path: Path) -> MediaInfoOutcome:
        nonlocal call_count
        call_count += 1
        return MediaInfoOutcome(media_info=fake_info, error=None)

    monkeypatch.setattr(MediaInfoProvider, "probe", _fake_probe)

    run_inventory_scan(
        config,
        json_output=False,
        output=str(tmp_path / "reports" / "library.json"),
        category="incoming",
        metadata=True,
    )

    assert call_count == 2


def test_incoming_metadata_scan_without_metadata_flag_never_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    movies_root, _, incoming_root = _make_tree(tmp_path)
    config_path = _write_config(
        tmp_path, nas_categories={"movies": str(movies_root)}, incoming_roots=[str(incoming_root)]
    )
    config = load_config(config_path)

    def _fail_if_called(self: MediaInfoProvider, path: Path) -> MediaInfoOutcome:
        raise AssertionError("probe should not be called without --metadata")

    monkeypatch.setattr(MediaInfoProvider, "probe", _fail_if_called)

    run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="incoming"
    )


def test_incoming_metadata_errors_are_scoped_to_selected_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    movies_root, _, incoming_root = _make_tree(tmp_path)
    config_path = _write_config(
        tmp_path, nas_categories={"movies": str(movies_root)}, incoming_roots=[str(incoming_root)]
    )
    config = load_config(config_path)

    def _fake_probe(self: MediaInfoProvider, path: Path) -> MediaInfoOutcome:
        return MediaInfoOutcome(media_info=None, error="simulated probe failure")

    monkeypatch.setattr(MediaInfoProvider, "probe", _fake_probe)

    report = run_inventory_scan(
        config,
        json_output=False,
        output=str(tmp_path / "reports" / "library.json"),
        category="incoming",
        metadata=True,
    )

    assert report.categories[0].files[0].media_info_error == "simulated probe failure"


# --- Reconciliation isolation ---------------------------------------------


def test_scoped_scan_adds_a_new_incoming_file(tmp_path: Path) -> None:
    movies_root, _, incoming_root = _make_tree(tmp_path)
    config_path = _write_config(
        tmp_path, nas_categories={"movies": str(movies_root)}, incoming_roots=[str(incoming_root)]
    )
    config = load_config(config_path)

    run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="incoming"
    )

    with connect(config.database_path) as connection:
        changes = connection.execute("SELECT change_type FROM scan_changes").fetchall()
        assert [c["change_type"] for c in changes] == ["ADDED"]


def test_scoped_scan_updates_a_changed_incoming_file(tmp_path: Path) -> None:
    movies_root, _, incoming_root = _make_tree(tmp_path)
    config_path = _write_config(
        tmp_path, nas_categories={"movies": str(movies_root)}, incoming_roots=[str(incoming_root)]
    )
    config = load_config(config_path)

    run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="incoming"
    )
    incoming_file = incoming_root / "Ripped Movie (2020).mkv"
    incoming_file.write_bytes(b"\0" * 999)

    run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="incoming"
    )

    with connect(config.database_path) as connection:
        change_types = [
            r["change_type"] for r in connection.execute("SELECT change_type FROM scan_changes ORDER BY id")
        ]
        assert change_types == ["ADDED", "UPDATED"]


def test_scoped_scan_marks_a_missing_incoming_file(tmp_path: Path) -> None:
    movies_root, _, incoming_root = _make_tree(tmp_path)
    config_path = _write_config(
        tmp_path, nas_categories={"movies": str(movies_root)}, incoming_roots=[str(incoming_root)]
    )
    config = load_config(config_path)

    run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="incoming"
    )
    (incoming_root / "Ripped Movie (2020).mkv").unlink()

    run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="incoming"
    )

    with connect(config.database_path) as connection:
        row = connection.execute("SELECT state FROM media_files").fetchone()
        assert row["state"] == "MISSING"


def test_scoped_scan_restores_a_missing_incoming_file(tmp_path: Path) -> None:
    movies_root, _, incoming_root = _make_tree(tmp_path)
    config_path = _write_config(
        tmp_path, nas_categories={"movies": str(movies_root)}, incoming_roots=[str(incoming_root)]
    )
    config = load_config(config_path)
    incoming_file = incoming_root / "Ripped Movie (2020).mkv"

    run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="incoming"
    )
    incoming_file.unlink()
    run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="incoming"
    )
    incoming_file.write_bytes(b"\0" * 20)
    run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="incoming"
    )

    with connect(config.database_path) as connection:
        change_types = [
            r["change_type"] for r in connection.execute("SELECT change_type FROM scan_changes ORDER BY id")
        ]
        assert change_types == ["ADDED", "MISSING", "RESTORED"]
        row = connection.execute("SELECT state FROM media_files").fetchone()
        assert row["state"] == "ACTIVE"


def test_scoped_incoming_scans_never_touch_movies_or_tv(tmp_path: Path) -> None:
    movies_root, tv_root, incoming_root = _make_tree(tmp_path)
    config_path = _write_config(
        tmp_path,
        nas_categories={"movies": str(movies_root), "tv": str(tv_root)},
        incoming_roots=[str(incoming_root)],
    )
    config = load_config(config_path)

    # Seed movies/tv into the canonical inventory with a full scan first.
    run_inventory_scan(config, json_output=False, output=str(tmp_path / "reports" / "library.json"))

    with connect(config.database_path) as connection:
        before = {
            r["absolute_path"]: (r["state"], r["last_seen_scan_id"])
            for r in connection.execute(
                "SELECT absolute_path, state, last_seen_scan_id FROM media_files "
                "WHERE library_id IN (SELECT id FROM libraries WHERE category != 'incoming')"
            )
        }
        assert len(before) == 2  # one movies file, one tv file

    # Now delete the incoming file (so a naive full reconciliation would
    # mark it missing) and run an Incoming-only scoped scan.
    (incoming_root / "Ripped Movie (2020).mkv").unlink()
    run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="incoming"
    )

    with connect(config.database_path) as connection:
        after = {
            r["absolute_path"]: (r["state"], r["last_seen_scan_id"])
            for r in connection.execute(
                "SELECT absolute_path, state, last_seen_scan_id FROM media_files "
                "WHERE library_id IN (SELECT id FROM libraries WHERE category != 'incoming')"
            )
        }
        assert after == before  # movies/tv completely untouched: state and last_seen_scan_id identical

        unrelated_changes = connection.execute(
            "SELECT COUNT(*) AS n FROM scan_changes "
            "WHERE library_id IN (SELECT id FROM libraries WHERE category != 'incoming') "
            "AND scan_run_id = (SELECT MAX(id) FROM scan_runs)"
        ).fetchone()
        assert unrelated_changes["n"] == 0


def test_scoped_incoming_scan_never_marks_unrelated_categories_missing(tmp_path: Path) -> None:
    movies_root, _, incoming_root = _make_tree(tmp_path)
    config_path = _write_config(
        tmp_path, nas_categories={"movies": str(movies_root)}, incoming_roots=[str(incoming_root)]
    )
    config = load_config(config_path)

    run_inventory_scan(config, json_output=False, output=str(tmp_path / "reports" / "library.json"))

    # Simulate the movies file physically disappearing -- a full scan would
    # mark it MISSING, but a scoped Incoming scan must not even notice.
    (movies_root / "Movie (2001).mkv").unlink()
    run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="incoming"
    )

    with connect(config.database_path) as connection:
        row = connection.execute(
            "SELECT state FROM media_files WHERE absolute_path LIKE '%Movie (2001).mkv'"
        ).fetchone()
        assert row["state"] == "ACTIVE"
        missing_events = connection.execute(
            "SELECT COUNT(*) AS n FROM scan_changes WHERE change_type = 'MISSING'"
        ).fetchone()
        assert missing_events["n"] == 0


# --- Scan history: scope persistence --------------------------------------


def test_scoped_scan_run_records_category_scope(tmp_path: Path) -> None:
    movies_root, _, incoming_root = _make_tree(tmp_path)
    config_path = _write_config(
        tmp_path, nas_categories={"movies": str(movies_root)}, incoming_roots=[str(incoming_root)]
    )
    config = load_config(config_path)

    run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), category="incoming"
    )

    with connect(config.database_path) as connection:
        row = connection.execute("SELECT scan_scope, scope_category FROM scan_runs").fetchone()
        assert row["scan_scope"] == "CATEGORY"
        assert row["scope_category"] == "incoming"


def test_full_scan_run_records_full_scope(tmp_path: Path) -> None:
    movies_root, _, _ = _make_tree(tmp_path)
    config_path = _write_config(tmp_path, nas_categories={"movies": str(movies_root)})
    config = load_config(config_path)

    run_inventory_scan(config, json_output=False, output=str(tmp_path / "reports" / "library.json"))

    with connect(config.database_path) as connection:
        row = connection.execute("SELECT scan_scope, scope_category FROM scan_runs").fetchone()
        assert row["scan_scope"] == "FULL"
        assert row["scope_category"] is None


# --- Reports ---------------------------------------------------------------


def test_scoped_report_contains_only_selected_category(tmp_path: Path) -> None:
    movies_root, tv_root, incoming_root = _make_tree(tmp_path)
    config_path = _write_config(
        tmp_path,
        nas_categories={"movies": str(movies_root), "tv": str(tv_root)},
        incoming_roots=[str(incoming_root)],
    )
    config = load_config(config_path)
    output_path = tmp_path / "reports" / "library-incoming.json"

    run_inventory_scan(config, json_output=False, output=str(output_path), category="incoming")

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(payload["categories"]) == 1
    assert payload["categories"][0]["category"] == "incoming"
    assert payload["file_count"] == payload["categories"][0]["file_count"]


def test_scoped_report_default_filenames_are_category_specific(tmp_path: Path) -> None:
    movies_root, _, incoming_root = _make_tree(tmp_path)
    config_path = _write_config(
        tmp_path, nas_categories={"movies": str(movies_root)}, incoming_roots=[str(incoming_root)]
    )
    config = load_config(config_path)

    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        run_inventory_scan(config, json_output=False, output="reports/library.json", category="incoming")
        assert (tmp_path / "reports" / "library-incoming.json").exists()
        assert (tmp_path / "reports" / "library-summary-incoming.txt").exists()
        assert not (tmp_path / "reports" / "library.json").exists()
    finally:
        os.chdir(cwd)


def test_scoped_report_never_overwrites_full_report(tmp_path: Path) -> None:
    movies_root, _, incoming_root = _make_tree(tmp_path)
    config_path = _write_config(
        tmp_path, nas_categories={"movies": str(movies_root)}, incoming_roots=[str(incoming_root)]
    )
    config = load_config(config_path)
    full_output = tmp_path / "reports" / "library.json"

    run_inventory_scan(config, json_output=False, output=str(full_output))
    full_payload_before = full_output.read_text(encoding="utf-8")

    run_inventory_scan(
        config,
        json_output=False,
        output=str(tmp_path / "reports" / "library-incoming.json"),
        category="incoming",
    )

    assert full_output.read_text(encoding="utf-8") == full_payload_before


def test_full_scan_report_behavior_unchanged(tmp_path: Path) -> None:
    movies_root, _, _ = _make_tree(tmp_path)
    config_path = _write_config(tmp_path, nas_categories={"movies": str(movies_root)})
    config = load_config(config_path)
    output_path = tmp_path / "reports" / "library.json"

    run_inventory_scan(config, json_output=False, output=str(output_path))

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert "scan_scope" not in payload
    assert set(payload.keys()) == {"file_count", "total_size_bytes", "categories"}
