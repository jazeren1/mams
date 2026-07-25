"""CLI tests for mams ingest plan/plans/show/stats/approve.

Seeds real data through run_inventory_scan()/run_identify_evaluate() over
a real config-driven Incoming root (the actual CLI wiring, including the
Incoming-as-category merge in run_inventory_scan), then confirms an
external identity directly via resolution_repository before generating a
plan -- TMDb itself is exercised separately in test_cli_resolve.py.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from mams.cli import (
    build_parser,
    run_identify_evaluate,
    run_ingest_approve,
    run_ingest_audit,
    run_ingest_plan,
    run_ingest_plans,
    run_ingest_show,
    run_ingest_stats,
    run_inventory_scan,
)
from mams.config import load_config
from mams.db import connect, migrate
from mams.identification_repository import list_candidates
from mams.resolution_repository import assign_identity, upsert_external_identity


def _write_config(tmp_path: Path, *, incoming_root: Path, nas_root: Path) -> Path:
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
                "nas": {"categories": {"movies": str(nas_root / "Movies"), "tv": str(nas_root / "TV")}},
                "ingest": {
                    "incoming_roots": [str(incoming_root)],
                    "movie_destination_category": "movies",
                    "tv_destination_category": "tv",
                    "kids_movie_destination_category": "kids_movies",
                    "kids_tv_destination_category": "kids_shows",
                },
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


def _mark_media_file_healthy(connection: sqlite3.Connection, media_file_id: int) -> None:
    """The real inventory scan never invokes MediaInfo unless --metadata
    is passed (and the fake `\\0`-filled fixture files wouldn't parse as
    real media anyway), so directly simulate a completed, healthy probe
    -- same approach as test_ingest_service.py's _seed_media_file."""
    connection.execute(
        "UPDATE media_files SET duration_seconds = 7020.0, media_info_probed_at = '2024-01-01T00:00:00', "
        "container = 'Matroska' WHERE id = ?",
        (media_file_id,),
    )
    connection.execute("INSERT INTO video_tracks (media_file_id, track_index) VALUES (?, 0)", (media_file_id,))
    connection.execute("INSERT INTO audio_tracks (media_file_id, track_index) VALUES (?, 0)", (media_file_id,))
    connection.commit()


def _seed_and_resolve(tmp_path: Path) -> tuple[object, int]:
    """Scans an Incoming file, evaluates it locally, simulates a healthy
    MediaInfo probe, confirms an external identity directly, and returns
    (config, media_file_id)."""
    incoming_root = tmp_path / "Incoming"
    _touch(incoming_root / "Alien.mkv")
    config = load_config(_write_config(tmp_path, incoming_root=incoming_root, nas_root=tmp_path / "NAS"))
    run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))
    run_identify_evaluate(config)

    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        candidate = list_candidates(connection)[0]
        _mark_media_file_healthy(connection, candidate.media_file_id)
        identity = upsert_external_identity(connection, media_type="MOVIE", provider_id=348, title="Alien", release_year=1979)
        assign_identity(
            connection, media_file_id=candidate.media_file_id, identification_candidate_id=candidate.id,
            external_identity_id=identity.id, resolution_attempt_id=None, assignment_method="AUTO", confidence="HIGH",
        )
        connection.commit()
        media_file_id = candidate.media_file_id
    finally:
        connection.close()
    return config, media_file_id


# --- parser -----------------------------------------------------------------


def test_parser_accepts_plan_flags() -> None:
    args = build_parser().parse_args(["ingest", "plan", "1", "--destination-category", "movie", "--json"])
    assert args.command == "ingest"
    assert args.ingest_command == "plan"
    assert args.media_file_id == 1
    assert args.destination_category == "movie"
    assert args.json is True


def test_parser_accepts_plans_show_stats_approve() -> None:
    plans_args = build_parser().parse_args(["ingest", "plans", "--status", "blocked", "--limit", "5"])
    assert plans_args.status == "BLOCKED"
    assert plans_args.limit == 5
    assert build_parser().parse_args(["ingest", "show", "1"]).plan_id == 1
    assert build_parser().parse_args(["ingest", "stats"]).ingest_command == "stats"
    assert build_parser().parse_args(["ingest", "approve", "1"]).plan_id == 1


# --- incoming files flow through the canonical inventory scan ------------------


def test_incoming_root_is_scanned_as_a_category(tmp_path: Path) -> None:
    incoming_root = tmp_path / "Incoming"
    _touch(incoming_root / "Alien.mkv")
    config = load_config(_write_config(tmp_path, incoming_root=incoming_root, nas_root=tmp_path / "NAS"))

    report = run_inventory_scan(config, json_output=False, output=_report_path(tmp_path))

    assert report.file_count == 1
    incoming_scan = next(c for c in report.categories if c.category == "incoming")
    assert incoming_scan.file_count == 1


# --- plan generation -----------------------------------------------------------


def test_run_ingest_plan_ready_for_review(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, media_file_id = _seed_and_resolve(tmp_path)
    capsys.readouterr()

    plan = run_ingest_plan(config, media_file_id, destination_category="movie")

    assert plan is not None
    assert plan.status == "READY_FOR_REVIEW"
    out = capsys.readouterr().out
    assert "Dry-Run Ingest Plan" in out
    assert "No actions were executed." in out
    assert "PROPOSED -- NOT EXECUTED" in out


def test_run_ingest_plan_review_required_without_destination_category(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, media_file_id = _seed_and_resolve(tmp_path)
    capsys.readouterr()

    plan = run_ingest_plan(config, media_file_id)

    assert plan is not None
    assert plan.status == "REVIEW_REQUIRED"


def test_run_ingest_plan_unknown_media_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    incoming_root = tmp_path / "Incoming"
    incoming_root.mkdir(parents=True)
    config = load_config(_write_config(tmp_path, incoming_root=incoming_root, nas_root=tmp_path / "NAS"))
    migrate(config.database_path)

    plan = run_ingest_plan(config, 999999, destination_category="movie")

    assert plan is None
    assert "No media file" in capsys.readouterr().out


def test_run_ingest_plan_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, media_file_id = _seed_and_resolve(tmp_path)
    capsys.readouterr()

    plan = run_ingest_plan(config, media_file_id, destination_category="movie", json_output=True)

    assert plan is not None
    assert plan.to_dict()["status"] == "READY_FOR_REVIEW"
    assert capsys.readouterr().out.strip() != ""


# --- plans/show/stats -----------------------------------------------------------


def test_run_ingest_plans_and_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, media_file_id = _seed_and_resolve(tmp_path)
    run_ingest_plan(config, media_file_id, destination_category="movie")
    capsys.readouterr()

    plans = run_ingest_plans(config)
    assert len(plans) == 1
    out = capsys.readouterr().out
    assert "READY_FOR_REVIEW" in out

    plan = run_ingest_show(config, plans[0].id)
    assert plan is not None
    out = capsys.readouterr().out
    assert "Plan #" in out


def test_run_ingest_show_unknown_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    incoming_root = tmp_path / "Incoming"
    incoming_root.mkdir(parents=True)
    config = load_config(_write_config(tmp_path, incoming_root=incoming_root, nas_root=tmp_path / "NAS"))
    migrate(config.database_path)

    plan = run_ingest_show(config, 999999)

    assert plan is None
    assert "not found" in capsys.readouterr().out


def test_run_ingest_stats(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, media_file_id = _seed_and_resolve(tmp_path)
    run_ingest_plan(config, media_file_id, destination_category="movie")
    capsys.readouterr()

    stats = run_ingest_stats(config)

    assert stats.total_count == 1
    assert "READY_FOR_REVIEW=1" in capsys.readouterr().out


def test_run_ingest_stats_empty_state(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    incoming_root = tmp_path / "Incoming"
    incoming_root.mkdir(parents=True)
    config = load_config(_write_config(tmp_path, incoming_root=incoming_root, nas_root=tmp_path / "NAS"))
    migrate(config.database_path)

    stats = run_ingest_stats(config)

    assert stats.total_count == 0
    assert "Total: 0" in capsys.readouterr().out


# --- approve --------------------------------------------------------------------


def test_run_ingest_approve(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, media_file_id = _seed_and_resolve(tmp_path)
    plan = run_ingest_plan(config, media_file_id, destination_category="movie")
    assert plan is not None
    capsys.readouterr()

    approved = run_ingest_approve(config, plan.id)

    assert approved is not None
    assert approved.status == "APPROVED"
    out = capsys.readouterr().out
    assert "Plan approved. No actions executed." in out


def test_run_ingest_approve_rejects_review_required_plan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, media_file_id = _seed_and_resolve(tmp_path)
    plan = run_ingest_plan(config, media_file_id)  # no destination category -> REVIEW_REQUIRED
    assert plan is not None
    assert plan.status == "REVIEW_REQUIRED"
    capsys.readouterr()

    result = run_ingest_approve(config, plan.id)

    assert result is None
    assert "not READY_FOR_REVIEW" in capsys.readouterr().out


def test_run_ingest_approve_unknown_plan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    incoming_root = tmp_path / "Incoming"
    incoming_root.mkdir(parents=True)
    config = load_config(_write_config(tmp_path, incoming_root=incoming_root, nas_root=tmp_path / "NAS"))
    migrate(config.database_path)

    result = run_ingest_approve(config, 999999)

    assert result is None
    assert "No ingest plan" in capsys.readouterr().out


# --- audit (Milestone 7C, Phase F) ----------------------------------------------


def test_parser_accepts_audit_flags() -> None:
    args = build_parser().parse_args(["ingest", "audit", "1", "--json"])
    assert args.ingest_command == "audit"
    assert args.plan_id == 1
    assert args.json is True


def test_run_ingest_audit_ready_for_executor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, media_file_id = _seed_and_resolve(tmp_path)
    plan = run_ingest_plan(config, media_file_id, destination_category="movie")
    assert plan is not None
    run_ingest_approve(config, plan.id)
    capsys.readouterr()

    result = run_ingest_audit(config, plan.id)

    assert result is not None
    assert result.readiness_status.value == "READY_FOR_EXECUTOR"
    out = capsys.readouterr().out
    assert "MAMS Execution-Readiness Audit" in out
    assert f"Plan #{plan.id}" in out
    assert "Status: READY_FOR_EXECUTOR" in out
    assert "EXECUTION WAS NOT PERFORMED." in out


def test_run_ingest_audit_unapproved_plan_is_blocked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, media_file_id = _seed_and_resolve(tmp_path)
    plan = run_ingest_plan(config, media_file_id, destination_category="movie")
    assert plan is not None
    assert plan.status == "READY_FOR_REVIEW"
    capsys.readouterr()

    result = run_ingest_audit(config, plan.id)

    assert result is not None
    assert result.readiness_status.value == "BLOCKED"
    assert "Status: BLOCKED" in capsys.readouterr().out


def test_run_ingest_audit_unknown_plan_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    incoming_root = tmp_path / "Incoming"
    incoming_root.mkdir(parents=True)
    config = load_config(_write_config(tmp_path, incoming_root=incoming_root, nas_root=tmp_path / "NAS"))
    migrate(config.database_path)

    result = run_ingest_audit(config, 999999)

    assert result is None
    assert "No ingest plan" in capsys.readouterr().out


def test_run_ingest_audit_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, media_file_id = _seed_and_resolve(tmp_path)
    plan = run_ingest_plan(config, media_file_id, destination_category="movie")
    assert plan is not None
    run_ingest_approve(config, plan.id)
    capsys.readouterr()

    result = run_ingest_audit(config, plan.id, json_output=True)

    assert result is not None
    out = capsys.readouterr().out
    assert '"execution_status": "NOT_EXECUTED"' in out
    assert '"readiness_status": "READY_FOR_EXECUTOR"' in out
    assert '"checks"' in out


def test_run_ingest_audit_always_reports_all_checks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, media_file_id = _seed_and_resolve(tmp_path)
    plan = run_ingest_plan(config, media_file_id, destination_category="movie")
    assert plan is not None
    capsys.readouterr()

    result = run_ingest_audit(config, plan.id)

    assert result is not None
    assert len(result.checks) == 25
