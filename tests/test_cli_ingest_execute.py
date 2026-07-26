"""CLI tests for mams ingest execute/executions/execution/recovery
(Milestone 8). Mirrors test_cli_ingest.py's real config-driven seeding
pattern for everything up through APPROVED.

Confirmed-execution rendering is tested against a monkeypatched
execution_service.execute_plan/preview_execution rather than a real
run: the real executor's post-transfer verification step invokes the
real `mediainfo` binary, which correctly refuses to find video/audio
tracks in this test suite's fake null-byte fixture files (there is no
real playable media available in this environment) -- exactly the
behavior test_execution_service.py already proves exhaustively with an
injected fake metadata provider. This file's job is to prove the CLI
glue (confirmation gating, argument parsing, dispatch, rendering), not
to re-prove the engine.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from mams import cli
from mams.cli import (
    build_parser,
    run_identify_evaluate,
    run_ingest_approve,
    run_ingest_execute,
    run_ingest_execution,
    run_ingest_executions,
    run_ingest_plan,
    run_ingest_recovery,
    run_inventory_scan,
)
from mams.config import AppConfig, load_config
from mams.db import connect, migrate
from mams.execution_repository import ExecutionRecord
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
                "execution": {"state_directory": str(tmp_path / ".mams" / "locks")},
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
    connection.execute(
        "UPDATE media_files SET duration_seconds = 7020.0, media_info_probed_at = '2024-01-01T00:00:00', "
        "container = 'Matroska' WHERE id = ?",
        (media_file_id,),
    )
    connection.execute("INSERT INTO video_tracks (media_file_id, track_index) VALUES (?, 0)", (media_file_id,))
    connection.execute("INSERT INTO audio_tracks (media_file_id, track_index) VALUES (?, 0)", (media_file_id,))
    connection.commit()


def _build_approved_ready_plan(tmp_path: Path) -> tuple[AppConfig, int]:
    incoming_root = tmp_path / "Incoming"
    nas_root = tmp_path / "NAS"
    (nas_root / "Movies").mkdir(parents=True, exist_ok=True)
    (nas_root / "TV").mkdir(parents=True, exist_ok=True)
    _touch(incoming_root / "Alien.mkv")
    config = load_config(_write_config(tmp_path, incoming_root=incoming_root, nas_root=nas_root))
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

    plan = run_ingest_plan(config, media_file_id, destination_category="movie")
    assert plan is not None
    assert plan.status == "READY_FOR_REVIEW"
    approved = run_ingest_approve(config, plan.id)
    assert approved is not None
    return config, approved.id


def _fake_execution_record(*, plan_id: int, status: str = "SUCCEEDED") -> ExecutionRecord:
    return ExecutionRecord(
        id=7,
        ingest_plan_id=plan_id,
        plan_version=1,
        status=status,
        transfer_strategy="SAME_FILESYSTEM_ATOMIC_RENAME",
        source_path="/Incoming/Alien.mkv",
        destination_path="/NAS/Movies/Alien (1979)/Alien (1979).mkv",
        source_device_id=1,
        destination_device_id=1,
        checksum_algorithm="sha256",
        source_checksum=None,
        destination_checksum=None,
        source_size_bytes=1_000_000,
        destination_size_bytes=1_000_000 if status == "SUCCEEDED" else None,
        lock_token="tok",
        started_at="2026-07-26 00:00:00",
        completed_at="2026-07-26 00:00:01",
        failure_step=None if status == "SUCCEEDED" else "VERIFY_DESTINATION_MEDIA",
        failure_message=None if status == "SUCCEEDED" else "destination verification did not pass",
        recovery_status="NONE" if status == "SUCCEEDED" else "DESTINATION_VERIFIED_SOURCE_NOT_REMOVED",
        source_removed_at=None,
        inventory_refresh_completed_at="2026-07-26 00:00:01" if status == "SUCCEEDED" else None,
        plex_refresh_status="SKIPPED" if status == "SUCCEEDED" else None,
        created_at="2026-07-26 00:00:00",
        updated_at="2026-07-26 00:00:01",
        steps=(),
    )


# --- parser -----------------------------------------------------------------------


def test_parser_accepts_execute_flags() -> None:
    args = build_parser().parse_args(["ingest", "execute", "14", "--confirm-plan", "14", "--json"])
    assert args.command == "ingest"
    assert args.ingest_command == "execute"
    assert args.plan_id == 14
    assert args.confirm_plan == 14
    assert args.json is True


def test_parser_accepts_executions_execution_recovery() -> None:
    executions_args = build_parser().parse_args(
        ["ingest", "executions", "--plan-id", "3", "--status", "failed", "--recovery-status", "none", "--limit", "5"]
    )
    assert executions_args.plan_id == 3
    assert executions_args.status == "FAILED"
    assert executions_args.recovery_status == "NONE"
    assert executions_args.limit == 5
    assert build_parser().parse_args(["ingest", "execution", "7"]).execution_id == 7
    assert build_parser().parse_args(["ingest", "recovery", "7"]).execution_id == 7


# --- preview / confirmation gating (real preview_execution, no mediainfo needed) ---


def test_run_ingest_execute_without_confirm_shows_preview_and_does_not_execute(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, plan_id = _build_approved_ready_plan(tmp_path)
    source_path = tmp_path / "Incoming" / "Alien.mkv"
    capsys.readouterr()

    result = run_ingest_execute(config, plan_id, confirm_plan=None)

    assert result is None
    out = capsys.readouterr().out
    assert "NO ACTIONS WERE EXECUTED." in out
    assert f"mams ingest execute {plan_id} --confirm-plan {plan_id}" in out
    assert "This command will modify the filesystem." in out
    # Nothing was touched.
    assert source_path.exists()
    from mams.ingest_repository import get_plan

    connection = connect(config.database_path)
    try:
        plan_after = get_plan(connection, plan_id)
    finally:
        connection.close()
    assert plan_after is not None
    assert plan_after.status == "APPROVED"


def test_run_ingest_execute_with_mismatched_confirm_plan_does_not_execute(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, plan_id = _build_approved_ready_plan(tmp_path)
    capsys.readouterr()

    result = run_ingest_execute(config, plan_id, confirm_plan=plan_id + 999)

    assert result is None
    assert "NO ACTIONS WERE EXECUTED." in capsys.readouterr().out


def test_run_ingest_execute_preview_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, plan_id = _build_approved_ready_plan(tmp_path)
    capsys.readouterr()

    result = run_ingest_execute(config, plan_id, confirm_plan=None, json_output=True)

    assert result is None
    out = capsys.readouterr().out
    assert '"confirmed": false' in out
    assert '"execution_status": "NOT_EXECUTED"' in out
    assert '"plan_id"' in out


def test_run_ingest_execute_unknown_plan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    incoming_root = tmp_path / "Incoming"
    incoming_root.mkdir(parents=True)
    config = load_config(_write_config(tmp_path, incoming_root=incoming_root, nas_root=tmp_path / "NAS"))
    migrate(config.database_path)

    result = run_ingest_execute(config, 999_999, confirm_plan=999_999)

    assert result is None
    assert "No ingest plan" in capsys.readouterr().out


# --- confirmed execution: CLI glue only, engine monkeypatched ----------------------


def test_run_ingest_execute_confirmed_dispatches_and_renders_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, plan_id = _build_approved_ready_plan(tmp_path)
    capsys.readouterr()

    fake_result = _fake_execution_record(plan_id=plan_id, status="SUCCEEDED")
    calls: list[int] = []

    def _fake_execute_plan(connection: object, cfg: object, *, plan_id: int) -> ExecutionRecord:
        calls.append(plan_id)
        return fake_result

    monkeypatch.setattr(cli.execution_service, "execute_plan", _fake_execute_plan)

    result = run_ingest_execute(config, plan_id, confirm_plan=plan_id)

    assert calls == [plan_id]
    assert result is fake_result
    out = capsys.readouterr().out
    assert "Execution completed successfully." in out
    assert "Plex:" in out
    assert "SKIPPED -- disabled by configuration" in out


def test_run_ingest_execute_confirmed_renders_recovery_required(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, plan_id = _build_approved_ready_plan(tmp_path)
    capsys.readouterr()

    fake_result = _fake_execution_record(plan_id=plan_id, status="RECOVERY_REQUIRED")

    def _fake_execute_plan(connection: object, cfg: object, *, plan_id: int) -> ExecutionRecord:
        return fake_result

    monkeypatch.setattr(cli.execution_service, "execute_plan", _fake_execute_plan)

    result = run_ingest_execute(config, plan_id, confirm_plan=plan_id)

    assert result is fake_result
    out = capsys.readouterr().out
    assert "RECOVERY_REQUIRED" in out
    assert f"mams ingest recovery {fake_result.id}" in out
    assert "Nothing was auto-retried." in out


def test_run_ingest_execute_confirmed_json_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, plan_id = _build_approved_ready_plan(tmp_path)
    capsys.readouterr()
    fake_result = _fake_execution_record(plan_id=plan_id, status="SUCCEEDED")
    monkeypatch.setattr(cli.execution_service, "execute_plan", lambda connection, cfg, *, plan_id: fake_result)

    result = run_ingest_execute(config, plan_id, confirm_plan=plan_id, json_output=True)

    assert result is fake_result
    out = capsys.readouterr().out
    assert '"status": "SUCCEEDED"' in out


def test_run_ingest_execute_confirmed_handles_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from mams.execution_service import PlanNotExecutableError

    config, plan_id = _build_approved_ready_plan(tmp_path)
    capsys.readouterr()

    def _raise(connection: object, cfg: object, *, plan_id: int) -> ExecutionRecord:
        raise PlanNotExecutableError("STALE")

    monkeypatch.setattr(cli.execution_service, "execute_plan", _raise)

    result = run_ingest_execute(config, plan_id, confirm_plan=plan_id)

    assert result is None
    assert "STALE" in capsys.readouterr().out


# --- executions / execution / recovery (real repository queries) ------------------


def test_run_ingest_executions_lists_history(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, plan_id = _build_approved_ready_plan(tmp_path)
    connection = connect(config.database_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO ingest_executions (
                    ingest_plan_id, plan_version, status, source_path, destination_path, source_size_bytes, lock_token
                ) VALUES (?, 1, 'SUCCEEDED', '/x', '/y', 1, 'tok')
                """,
                (plan_id,),
            )
    finally:
        connection.close()
    capsys.readouterr()

    executions = run_ingest_executions(config, plan_id=plan_id)

    assert len(executions) == 1
    assert executions[0].ingest_plan_id == plan_id
    assert "execution(s)" in capsys.readouterr().out


def test_run_ingest_executions_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, plan_id = _build_approved_ready_plan(tmp_path)
    capsys.readouterr()

    executions = run_ingest_executions(config, json_output=True)

    assert executions == []
    assert capsys.readouterr().out.strip() == "[]"


def test_run_ingest_execution_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, plan_id = _build_approved_ready_plan(tmp_path)
    connection = connect(config.database_path)
    try:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO ingest_executions (
                    ingest_plan_id, plan_version, status, source_path, destination_path, source_size_bytes, lock_token
                ) VALUES (?, 1, 'FAILED', '/x', '/y', 1, 'tok')
                """,
                (plan_id,),
            )
            execution_id = cursor.lastrowid
    finally:
        connection.close()
    assert execution_id is not None
    capsys.readouterr()

    execution = run_ingest_execution(config, execution_id)

    assert execution is not None
    assert execution.id == execution_id
    assert f"Execution: #{execution_id}" in capsys.readouterr().out


def test_run_ingest_execution_show_missing_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, _ = _build_approved_ready_plan(tmp_path)
    capsys.readouterr()

    execution = run_ingest_execution(config, 999_999)

    assert execution is None
    assert "not found" in capsys.readouterr().out


def test_run_ingest_recovery_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, plan_id = _build_approved_ready_plan(tmp_path)
    connection = connect(config.database_path)
    try:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO ingest_executions (
                    ingest_plan_id, plan_version, status, source_path, destination_path,
                    source_size_bytes, lock_token, recovery_status
                ) VALUES (?, 1, 'RECOVERY_REQUIRED', ?, ?, 1, 'tok', 'PARTIAL_DESTINATION_SOURCE_INTACT')
                """,
                (plan_id, str(tmp_path / "Incoming" / "Alien.mkv"), str(tmp_path / "NAS" / "Movies" / "missing.mkv")),
            )
            execution_id = cursor.lastrowid
    finally:
        connection.close()
    assert execution_id is not None
    capsys.readouterr()

    guidance = run_ingest_recovery(config, execution_id)

    assert guidance is not None
    assert guidance.recovery_status == "PARTIAL_DESTINATION_SOURCE_INTACT"
    out = capsys.readouterr().out
    assert "NO ACTIONS WERE TAKEN." in out
    assert "Recommendation:" in out


def test_run_ingest_recovery_missing_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, _ = _build_approved_ready_plan(tmp_path)
    capsys.readouterr()

    guidance = run_ingest_recovery(config, 999_999)

    assert guidance is None
    assert "not found" in capsys.readouterr().out


def test_run_ingest_audit_shows_last_execution(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from mams.cli import run_ingest_audit

    config, plan_id = _build_approved_ready_plan(tmp_path)
    connection = connect(config.database_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO ingest_executions (
                    ingest_plan_id, plan_version, status, source_path, destination_path, source_size_bytes, lock_token
                ) VALUES (?, 1, 'SUCCEEDED', '/x', '/y', 1, 'tok')
                """,
                (plan_id,),
            )
    finally:
        connection.close()
    capsys.readouterr()

    result = run_ingest_audit(config, plan_id)

    assert result is not None
    out = capsys.readouterr().out
    assert "Last execution:" in out
    assert "SUCCEEDED" in out
