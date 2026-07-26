"""Safety tests (Milestone 8): proves every *rejection* path in
`mams ingest execute` -- unapproved plan, stale audit, mismatched
confirmation, a lock already held, and an unconfigured state directory
-- produces zero filesystem mutation, even though `ingest execute` is
actually invoked (not merely a read-only command like Milestone 7C's
identify/resolve/plan/approve/audit workflow).

Same technique as test_safety_no_execution.py: `Path.rename/replace`,
`shutil.move/copy/copy2`, `os.rename/replace/remove/unlink`, and
`subprocess.run/Popen` are monkeypatched to raise unconditionally, and
`Path.mkdir` is guarded to allow only exist_ok-style no-ops on
directories that already exist (the local state directory legitimately
gets created by a *successful* lock acquisition attempt -- see the
per-scenario notes below for exactly when that's expected).

test_safety_no_execution.py itself is left completely untouched: it
still proves the read-only pre-execution workflow never mutates
anything, and nothing in this milestone changed that workflow.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sqlite3
from pathlib import Path

import pytest
import yaml

from mams.cli import (
    run_identify_evaluate,
    run_ingest_approve,
    run_ingest_execute,
    run_ingest_plan,
    run_inventory_scan,
)
from mams.config import AppConfig, load_config
from mams.db import connect, migrate
from mams.execution_lock import acquire_lock
from mams.identification_repository import list_candidates
from mams.ingest_repository import get_plan
from mams.resolution_repository import assign_identity, upsert_external_identity


def _write_config(tmp_path: Path, *, incoming_root: Path, nas_root: Path, configure_execution: bool = True) -> Path:
    config_path = tmp_path / "config.yaml"
    raw: dict[str, object] = {
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
    if configure_execution:
        raw["execution"] = {"state_directory": str(tmp_path / ".mams" / "locks")}
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return config_path


def _touch(path: Path, size: int = 1_000_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)


def _mark_media_file_healthy(connection: sqlite3.Connection, media_file_id: int) -> None:
    connection.execute(
        "UPDATE media_files SET duration_seconds = 7020.0, media_info_probed_at = '2024-01-01T00:00:00', "
        "container = 'Matroska' WHERE id = ?",
        (media_file_id,),
    )
    connection.execute("INSERT INTO video_tracks (media_file_id, track_index) VALUES (?, 0)", (media_file_id,))
    connection.execute("INSERT INTO audio_tracks (media_file_id, track_index) VALUES (?, 0)", (media_file_id,))
    connection.commit()


def _build_approved_ready_plan(tmp_path: Path, *, configure_execution: bool = True) -> tuple[AppConfig, int]:
    incoming_root = tmp_path / "Incoming"
    nas_root = tmp_path / "NAS"
    (nas_root / "Movies").mkdir(parents=True, exist_ok=True)
    (nas_root / "TV").mkdir(parents=True, exist_ok=True)
    _touch(incoming_root / "Alien.mkv")
    config = load_config(
        _write_config(tmp_path, incoming_root=incoming_root, nas_root=nas_root, configure_execution=configure_execution)
    )
    run_inventory_scan(config, json_output=False, output=str(tmp_path / "reports" / "library.json"))
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


def _forbid(name: str):  # type: ignore[no-untyped-def]
    def _raise(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"{name} must never be called for a rejected ingest execute attempt")

    return _raise


def _guarded_mkdir(self: Path, *args: object, **kwargs: object) -> None:
    if self.exists():
        return None
    raise AssertionError(f"Path.mkdir must never create a new directory for a rejected ingest execute attempt: {self}")


def _apply_safety_patches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "mkdir", _guarded_mkdir)
    monkeypatch.setattr(Path, "rename", _forbid("Path.rename"))
    monkeypatch.setattr(Path, "replace", _forbid("Path.replace"))
    monkeypatch.setattr(shutil, "move", _forbid("shutil.move"))
    monkeypatch.setattr(shutil, "copy", _forbid("shutil.copy"))
    monkeypatch.setattr(shutil, "copy2", _forbid("shutil.copy2"))
    monkeypatch.setattr(os, "rename", _forbid("os.rename"))
    monkeypatch.setattr(os, "replace", _forbid("os.replace"))
    monkeypatch.setattr(os, "remove", _forbid("os.remove"))
    monkeypatch.setattr(os, "unlink", _forbid("os.unlink"))
    monkeypatch.setattr(os, "link", _forbid("os.link"))
    monkeypatch.setattr(subprocess, "run", _forbid("subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", _forbid("subprocess.Popen"))


def _assert_untouched(config: AppConfig, plan_id: int, source_path: Path, expected_status: str) -> None:
    assert source_path.exists()
    assert source_path.stat().st_size == 1_000_000
    connection = connect(config.database_path)
    try:
        plan = get_plan(connection, plan_id)
        assert plan is not None
        assert plan.status == expected_status
        execution_count = connection.execute(
            "SELECT COUNT(*) FROM ingest_executions WHERE ingest_plan_id = ?", (plan_id,)
        ).fetchone()[0]
        assert execution_count == 0
    finally:
        connection.close()


def test_unapproved_plan_rejected_with_zero_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    incoming_root = tmp_path / "Incoming"
    nas_root = tmp_path / "NAS"
    (nas_root / "Movies").mkdir(parents=True, exist_ok=True)
    (nas_root / "TV").mkdir(parents=True, exist_ok=True)
    _touch(incoming_root / "Alien.mkv")
    config = load_config(_write_config(tmp_path, incoming_root=incoming_root, nas_root=nas_root))
    run_inventory_scan(config, json_output=False, output=str(tmp_path / "reports" / "library.json"))
    run_identify_evaluate(config)
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

    _apply_safety_patches(monkeypatch)

    result = run_ingest_execute(config, plan.id, confirm_plan=plan.id)

    assert result is None
    _assert_untouched(config, plan.id, incoming_root / "Alien.mkv", "READY_FOR_REVIEW")


def test_stale_plan_rejected_with_zero_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Approved, but canonical inventory was rescanned/updated afterward
    without regenerating the plan -- the audit reports STALE."""
    config, plan_id = _build_approved_ready_plan(tmp_path)
    plan = get_plan(connect(config.database_path), plan_id)
    assert plan is not None
    connection = connect(config.database_path)
    try:
        connection.execute("UPDATE media_files SET size_bytes = size_bytes + 1 WHERE id = ?", (plan.media_file_id,))
        connection.commit()
    finally:
        connection.close()

    _apply_safety_patches(monkeypatch)

    result = run_ingest_execute(config, plan_id, confirm_plan=plan_id)

    assert result is None
    _assert_untouched(config, plan_id, Path(plan.source_path), "APPROVED")


def test_missing_confirmation_rejected_with_zero_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, plan_id = _build_approved_ready_plan(tmp_path)
    source_path = tmp_path / "Incoming" / "Alien.mkv"

    _apply_safety_patches(monkeypatch)

    result = run_ingest_execute(config, plan_id, confirm_plan=None)

    assert result is None
    _assert_untouched(config, plan_id, source_path, "APPROVED")


def test_mismatched_confirmation_rejected_with_zero_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, plan_id = _build_approved_ready_plan(tmp_path)
    source_path = tmp_path / "Incoming" / "Alien.mkv"

    _apply_safety_patches(monkeypatch)

    result = run_ingest_execute(config, plan_id, confirm_plan=plan_id + 12345)

    assert result is None
    _assert_untouched(config, plan_id, source_path, "APPROVED")


def test_lock_already_held_rejected_with_zero_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, plan_id = _build_approved_ready_plan(tmp_path)
    source_path = tmp_path / "Incoming" / "Alien.mkv"
    state_directory = config.execution_state_directory
    assert state_directory is not None
    # A real, separate process already holds the lock -- created before
    # the safety patches, exactly like the legitimate scan step in
    # test_safety_no_execution.py.
    acquire_lock(state_directory, plan_id, token="another-process-token")

    _apply_safety_patches(monkeypatch)

    result = run_ingest_execute(config, plan_id, confirm_plan=plan_id)

    assert result is None
    _assert_untouched(config, plan_id, source_path, "APPROVED")


def test_unconfigured_state_directory_rejected_with_zero_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, plan_id = _build_approved_ready_plan(tmp_path, configure_execution=False)
    source_path = tmp_path / "Incoming" / "Alien.mkv"
    assert config.execution_state_directory is None

    _apply_safety_patches(monkeypatch)

    result = run_ingest_execute(config, plan_id, confirm_plan=plan_id)

    assert result is None
    _assert_untouched(config, plan_id, source_path, "APPROVED")


def test_unknown_plan_id_rejected_with_zero_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    incoming_root = tmp_path / "Incoming"
    incoming_root.mkdir(parents=True)
    config = load_config(_write_config(tmp_path, incoming_root=incoming_root, nas_root=tmp_path / "NAS"))
    migrate(config.database_path)

    _apply_safety_patches(monkeypatch)

    result = run_ingest_execute(config, 999_999, confirm_plan=999_999)

    assert result is None
