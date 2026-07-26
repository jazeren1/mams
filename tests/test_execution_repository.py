"""Tests for execution_repository.py: the atomic APPROVED -> EXECUTING
transition, execution/step lifecycle, and query layer.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mams.db import connect, migrate
from mams.execution_repository import (
    begin_step,
    complete_step,
    get_current_execution_for_plan,
    get_execution,
    list_executions,
    mark_execution_failed,
    mark_execution_recovery_required,
    mark_execution_succeeded,
    record_inventory_refresh_completed,
    record_plex_refresh_status,
    record_source_removed,
    start_execution,
    transition_plan_after_execution,
    transition_plan_to_executing,
)

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"

_STEP_TYPES = ["VALIDATE_SOURCE", "CREATE_DESTINATION_DIRECTORY", "ATOMIC_RENAME", "FINALIZE"]


@pytest.fixture()
def connection(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "mams.db"
    migrate(db_path, REPO_MIGRATIONS_DIR)
    conn = connect(db_path)
    yield conn
    conn.close()


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _seed_library(connection: sqlite3.Connection, category: str = "incoming") -> int:
    row = connection.execute("SELECT id FROM libraries WHERE category = ?", (category,)).fetchone()
    if row is not None:
        return int(row["id"])
    return _lastrowid(
        connection.execute("INSERT INTO libraries (category, root_path) VALUES (?, ?)", (category, "/Incoming"))
    )


def _seed_media_file(connection: sqlite3.Connection, *, library_id: int, name: str = "Alien.mkv") -> int:
    scan_id = _lastrowid(connection.execute("INSERT INTO scan_runs DEFAULT VALUES"))
    return _lastrowid(
        connection.execute(
            """
            INSERT INTO media_files (
                library_id, absolute_path, relative_path, filename, extension,
                parent_directory, layout, size_bytes, first_seen_scan_id, last_seen_scan_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (library_id, f"/Incoming/{name}", name, name, ".mkv", "/Incoming", "unknown", 1234, scan_id, scan_id),
        )
    )


_plan_counter = 0


def _seed_plan(connection: sqlite3.Connection, *, status: str = "APPROVED") -> int:
    global _plan_counter
    _plan_counter += 1
    name = f"Alien{_plan_counter}.mkv"
    library_id = _seed_library(connection)
    media_file_id = _seed_media_file(connection, library_id=library_id, name=name)
    return _lastrowid(
        connection.execute(
            "INSERT INTO ingest_plans (media_file_id, status, source_path) VALUES (?, ?, ?)",
            (media_file_id, status, f"/Incoming/{name}"),
        )
    )


def _start(connection: sqlite3.Connection, plan_id: int, *, token: str = "tok-1") -> int:
    with connection:
        assert transition_plan_to_executing(connection, plan_id) is True
        execution = start_execution(
            connection,
            ingest_plan_id=plan_id,
            plan_version=1,
            transfer_strategy="SAME_FILESYSTEM_ATOMIC_RENAME",
            source_path="/Incoming/Alien.mkv",
            destination_path="/NAS/Movies/Alien (1979)/Alien (1979).mkv",
            source_device_id=1,
            destination_device_id=1,
            checksum_algorithm="sha256",
            source_size_bytes=1_000_000,
            lock_token=token,
            step_types=_STEP_TYPES,
        )
    return execution.id


# --- transition_plan_to_executing -----------------------------------------------


def test_transition_plan_to_executing_succeeds_from_approved(connection: sqlite3.Connection) -> None:
    plan_id = _seed_plan(connection, status="APPROVED")
    with connection:
        assert transition_plan_to_executing(connection, plan_id) is True
    row = connection.execute("SELECT status FROM ingest_plans WHERE id = ?", (plan_id,)).fetchone()
    assert row["status"] == "EXECUTING"


@pytest.mark.parametrize("status", ["DRAFT", "READY_FOR_REVIEW", "REVIEW_REQUIRED", "BLOCKED", "SUPERSEDED", "EXECUTING"])
def test_transition_plan_to_executing_fails_from_any_non_approved_status(
    connection: sqlite3.Connection, status: str
) -> None:
    plan_id = _seed_plan(connection, status=status)
    with connection:
        assert transition_plan_to_executing(connection, plan_id) is False
    row = connection.execute("SELECT status FROM ingest_plans WHERE id = ?", (plan_id,)).fetchone()
    assert row["status"] == status


def test_transition_plan_to_executing_is_the_concurrency_guard_on_second_call(connection: sqlite3.Connection) -> None:
    """Simulates two racing callers: the second call's rowcount must be
    0, and it must not have touched anything -- this is the actual proof
    that the UPDATE's WHERE clause, not a prior SELECT, is what prevents
    a second executor from proceeding."""
    plan_id = _seed_plan(connection, status="APPROVED")
    with connection:
        assert transition_plan_to_executing(connection, plan_id) is True
    with connection:
        assert transition_plan_to_executing(connection, plan_id) is False
    row = connection.execute("SELECT status FROM ingest_plans WHERE id = ?", (plan_id,)).fetchone()
    assert row["status"] == "EXECUTING"


# --- start_execution / get_execution --------------------------------------------


def test_start_execution_creates_execution_and_seeds_pending_steps(connection: sqlite3.Connection) -> None:
    plan_id = _seed_plan(connection)
    execution_id = _start(connection, plan_id)
    execution = get_execution(connection, execution_id)
    assert execution is not None
    assert execution.ingest_plan_id == plan_id
    assert execution.status == "EXECUTING"
    assert execution.transfer_strategy == "SAME_FILESYSTEM_ATOMIC_RENAME"
    assert [step.step_type for step in execution.steps] == _STEP_TYPES
    assert all(step.status == "PENDING" for step in execution.steps)
    assert [step.step_order for step in execution.steps] == [1, 2, 3, 4]


def test_get_execution_returns_none_for_unknown_id(connection: sqlite3.Connection) -> None:
    assert get_execution(connection, 999) is None


# --- begin_step / complete_step --------------------------------------------------


def test_begin_step_moves_pending_to_running(connection: sqlite3.Connection) -> None:
    plan_id = _seed_plan(connection)
    execution_id = _start(connection, plan_id)
    with connection:
        step = begin_step(connection, execution_id=execution_id, step_order=1)
    assert step.status == "RUNNING"
    assert step.started_at is not None


def test_complete_step_records_terminal_status_and_detail(connection: sqlite3.Connection) -> None:
    plan_id = _seed_plan(connection)
    execution_id = _start(connection, plan_id)
    with connection:
        step = begin_step(connection, execution_id=execution_id, step_order=1)
    with connection:
        completed = complete_step(connection, step_id=step.id, status="SUCCEEDED", detail={"bytes": 123})
    assert completed.status == "SUCCEEDED"
    assert completed.completed_at is not None
    assert completed.detail == {"bytes": 123}


def test_complete_step_without_detail_stores_none(connection: sqlite3.Connection) -> None:
    plan_id = _seed_plan(connection)
    execution_id = _start(connection, plan_id)
    with connection:
        step = begin_step(connection, execution_id=execution_id, step_order=1)
    with connection:
        completed = complete_step(connection, step_id=step.id, status="FAILED")
    assert completed.status == "FAILED"
    assert completed.detail is None


# --- mark_execution_* / record_* -------------------------------------------------


def test_mark_execution_succeeded_sets_status_checksum_and_size(connection: sqlite3.Connection) -> None:
    plan_id = _seed_plan(connection)
    execution_id = _start(connection, plan_id)
    with connection:
        execution = mark_execution_succeeded(
            connection, execution_id, destination_checksum="abc123", destination_size_bytes=1_000_000
        )
    assert execution.status == "SUCCEEDED"
    assert execution.destination_checksum == "abc123"
    assert execution.destination_size_bytes == 1_000_000
    assert execution.recovery_status == "NONE"
    assert execution.completed_at is not None


def test_mark_execution_failed_sets_status_and_none_recovery(connection: sqlite3.Connection) -> None:
    plan_id = _seed_plan(connection)
    execution_id = _start(connection, plan_id)
    with connection:
        execution = mark_execution_failed(
            connection, execution_id, failure_step="CREATE_DESTINATION_DIRECTORY", failure_message="disk full"
        )
    assert execution.status == "FAILED"
    assert execution.failure_step == "CREATE_DESTINATION_DIRECTORY"
    assert execution.failure_message == "disk full"
    assert execution.recovery_status == "NONE"


def test_mark_execution_recovery_required_sets_status_and_recovery_status(connection: sqlite3.Connection) -> None:
    plan_id = _seed_plan(connection)
    execution_id = _start(connection, plan_id)
    with connection:
        execution = mark_execution_recovery_required(
            connection,
            execution_id,
            failure_step="STREAM_COPY_WITH_CHECKSUM",
            failure_message="interrupted",
            recovery_status="PARTIAL_DESTINATION_SOURCE_INTACT",
        )
    assert execution.status == "RECOVERY_REQUIRED"
    assert execution.recovery_status == "PARTIAL_DESTINATION_SOURCE_INTACT"


def test_record_source_removed_sets_timestamp(connection: sqlite3.Connection) -> None:
    plan_id = _seed_plan(connection)
    execution_id = _start(connection, plan_id)
    with connection:
        record_source_removed(connection, execution_id)
    execution = get_execution(connection, execution_id)
    assert execution is not None
    assert execution.source_removed_at is not None


def test_record_inventory_refresh_completed_sets_timestamp(connection: sqlite3.Connection) -> None:
    plan_id = _seed_plan(connection)
    execution_id = _start(connection, plan_id)
    with connection:
        record_inventory_refresh_completed(connection, execution_id)
    execution = get_execution(connection, execution_id)
    assert execution is not None
    assert execution.inventory_refresh_completed_at is not None


def test_record_plex_refresh_status_sets_status(connection: sqlite3.Connection) -> None:
    plan_id = _seed_plan(connection)
    execution_id = _start(connection, plan_id)
    with connection:
        record_plex_refresh_status(connection, execution_id, "SKIPPED")
    execution = get_execution(connection, execution_id)
    assert execution is not None
    assert execution.plex_refresh_status == "SKIPPED"


# --- transition_plan_after_execution ---------------------------------------------


def test_transition_plan_after_execution_sets_executed_status_and_timestamp(connection: sqlite3.Connection) -> None:
    plan_id = _seed_plan(connection)
    _start(connection, plan_id)
    with connection:
        transition_plan_after_execution(connection, plan_id, new_status="EXECUTED", executed_at="2026-07-26T00:00:00")
    row = connection.execute("SELECT status, executed_at FROM ingest_plans WHERE id = ?", (plan_id,)).fetchone()
    assert row["status"] == "EXECUTED"
    assert row["executed_at"] == "2026-07-26T00:00:00"


def test_transition_plan_after_execution_to_recovery_required_leaves_executed_at_null(
    connection: sqlite3.Connection,
) -> None:
    plan_id = _seed_plan(connection)
    _start(connection, plan_id)
    with connection:
        transition_plan_after_execution(connection, plan_id, new_status="RECOVERY_REQUIRED", executed_at=None)
    row = connection.execute("SELECT status, executed_at FROM ingest_plans WHERE id = ?", (plan_id,)).fetchone()
    assert row["status"] == "RECOVERY_REQUIRED"
    assert row["executed_at"] is None


# --- get_current_execution_for_plan / list_executions ----------------------------


def test_get_current_execution_for_plan_returns_none_when_never_executed(connection: sqlite3.Connection) -> None:
    plan_id = _seed_plan(connection)
    assert get_current_execution_for_plan(connection, plan_id) is None


def test_get_current_execution_for_plan_returns_most_recent(connection: sqlite3.Connection) -> None:
    plan_id = _seed_plan(connection)
    execution_id = _start(connection, plan_id)
    current = get_current_execution_for_plan(connection, plan_id)
    assert current is not None
    assert current.id == execution_id


def test_list_executions_filters_by_status_recovery_status_and_plan_id(connection: sqlite3.Connection) -> None:
    plan_a = _seed_plan(connection)
    plan_b = _seed_plan(connection)
    execution_a = _start(connection, plan_a, token="tok-a")
    execution_b = _start(connection, plan_b, token="tok-b")
    with connection:
        mark_execution_succeeded(connection, execution_a, destination_checksum=None, destination_size_bytes=1)
        mark_execution_recovery_required(
            connection, execution_b, failure_step="FINAL_RENAME", failure_message="x", recovery_status="INVENTORY_REFRESH_INCOMPLETE"
        )

    assert {e.id for e in list_executions(connection, status="SUCCEEDED")} == {execution_a}
    assert {e.id for e in list_executions(connection, recovery_status="INVENTORY_REFRESH_INCOMPLETE")} == {execution_b}
    assert {e.id for e in list_executions(connection, plan_id=plan_a)} == {execution_a}


def test_list_executions_orders_most_recent_first_and_respects_limit(connection: sqlite3.Connection) -> None:
    plan_id = _seed_plan(connection)
    first = _start(connection, plan_id, token="tok-1")
    with connection:
        mark_execution_failed(connection, first, failure_step="VALIDATE_SOURCE", failure_message="x")
        transition_plan_after_execution(connection, plan_id, new_status="EXECUTION_FAILED", executed_at=None)
        connection.execute("UPDATE ingest_plans SET status = 'APPROVED' WHERE id = ?", (plan_id,))
    second = _start(connection, plan_id, token="tok-2")

    executions = list_executions(connection, plan_id=plan_id)
    assert [e.id for e in executions] == [second, first]

    limited = list_executions(connection, plan_id=plan_id, limit=1)
    assert [e.id for e in limited] == [second]


def test_historical_failed_execution_is_retained_after_a_new_one_starts(connection: sqlite3.Connection) -> None:
    plan_id = _seed_plan(connection)
    first = _start(connection, plan_id, token="tok-1")
    with connection:
        mark_execution_failed(connection, first, failure_step="VALIDATE_SOURCE", failure_message="x")
        transition_plan_after_execution(connection, plan_id, new_status="EXECUTION_FAILED", executed_at=None)
        connection.execute("UPDATE ingest_plans SET status = 'APPROVED' WHERE id = ?", (plan_id,))
    _start(connection, plan_id, token="tok-2")

    assert get_execution(connection, first) is not None
    assert get_execution(connection, first).status == "FAILED"  # type: ignore[union-attr]
