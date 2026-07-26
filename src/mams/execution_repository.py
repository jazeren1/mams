"""Persistence for Milestone 8 execution history: owns all SQL for
`ingest_executions` and `ingest_execution_steps`. Mirrors
`ingest_repository.py`'s role for its schema.

`execution_service.py` is the only caller that combines this module
with `execution.py` (pure strategy/preflight/verification logic),
`execution_filesystem.py` (the filesystem adapter), and
`execution_lock.py` (the process-level lock).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass


def _serialize_detail(value: dict[str, object] | None) -> str | None:
    if not value:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ExecutionStepRecord:
    id: int
    ingest_execution_id: int
    step_order: int
    step_type: str
    status: str
    started_at: str | None
    completed_at: str | None
    detail: dict[str, object] | None
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionRecord:
    id: int
    ingest_plan_id: int
    plan_version: int
    status: str
    transfer_strategy: str | None
    source_path: str
    destination_path: str
    source_device_id: int | None
    destination_device_id: int | None
    checksum_algorithm: str
    source_checksum: str | None
    destination_checksum: str | None
    source_size_bytes: int
    destination_size_bytes: int | None
    lock_token: str
    started_at: str
    completed_at: str | None
    failure_step: str | None
    failure_message: str | None
    recovery_status: str | None
    source_removed_at: str | None
    inventory_refresh_completed_at: str | None
    plex_refresh_status: str | None
    created_at: str
    updated_at: str
    steps: tuple[ExecutionStepRecord, ...]

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["steps"] = [step.to_dict() for step in self.steps]
        return data


def _row_to_step(row: sqlite3.Row) -> ExecutionStepRecord:
    return ExecutionStepRecord(
        id=row["id"],
        ingest_execution_id=row["ingest_execution_id"],
        step_order=row["step_order"],
        step_type=row["step_type"],
        status=row["status"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        detail=json.loads(row["detail_json"]) if row["detail_json"] is not None else None,
        created_at=row["created_at"],
    )


def _steps_for_execution(connection: sqlite3.Connection, execution_id: int) -> tuple[ExecutionStepRecord, ...]:
    rows = connection.execute(
        "SELECT * FROM ingest_execution_steps WHERE ingest_execution_id = ? ORDER BY step_order",
        (execution_id,),
    ).fetchall()
    return tuple(_row_to_step(row) for row in rows)


def _row_to_execution(row: sqlite3.Row, steps: tuple[ExecutionStepRecord, ...]) -> ExecutionRecord:
    return ExecutionRecord(
        id=row["id"],
        ingest_plan_id=row["ingest_plan_id"],
        plan_version=row["plan_version"],
        status=row["status"],
        transfer_strategy=row["transfer_strategy"],
        source_path=row["source_path"],
        destination_path=row["destination_path"],
        source_device_id=row["source_device_id"],
        destination_device_id=row["destination_device_id"],
        checksum_algorithm=row["checksum_algorithm"],
        source_checksum=row["source_checksum"],
        destination_checksum=row["destination_checksum"],
        source_size_bytes=row["source_size_bytes"],
        destination_size_bytes=row["destination_size_bytes"],
        lock_token=row["lock_token"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        failure_step=row["failure_step"],
        failure_message=row["failure_message"],
        recovery_status=row["recovery_status"],
        source_removed_at=row["source_removed_at"],
        inventory_refresh_completed_at=row["inventory_refresh_completed_at"],
        plex_refresh_status=row["plex_refresh_status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        steps=steps,
    )


def transition_plan_to_executing(connection: sqlite3.Connection, plan_id: int) -> bool:
    """Atomically move a plan from APPROVED to EXECUTING. Returns
    whether exactly one row was updated.

    The UPDATE's own WHERE clause -- not a prior SELECT -- is the
    concurrency guard: a `rowcount` of 0 means the plan's status was
    something other than APPROVED at the moment of this UPDATE, which
    means it moved since the caller last checked, and the caller must
    abort rather than proceed. This is deliberately stronger than
    `ingest_repository.approve_plan`'s existing SELECT-then-UPDATE
    pattern, which has no status predicate in its UPDATE itself --
    appropriate there (single-process CLI, no execution consequence),
    not appropriate here, where a missed race would begin a filesystem
    mutation."""
    cursor = connection.execute(
        "UPDATE ingest_plans SET status = 'EXECUTING', updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND status = 'APPROVED'",
        (plan_id,),
    )
    return cursor.rowcount == 1


def start_execution(
    connection: sqlite3.Connection,
    *,
    ingest_plan_id: int,
    plan_version: int,
    transfer_strategy: str,
    source_path: str,
    destination_path: str,
    source_device_id: int,
    destination_device_id: int,
    checksum_algorithm: str,
    source_size_bytes: int,
    lock_token: str,
    step_types: list[str],
) -> ExecutionRecord:
    """Insert one `ingest_executions` row (status `EXECUTING`) plus one
    `PENDING` `ingest_execution_steps` row per planned step, in order.
    Not itself transactional -- callers run this in the same
    `with connection:` block as `transition_plan_to_executing`, so both
    succeed or neither does."""
    cursor = connection.execute(
        """
        INSERT INTO ingest_executions (
            ingest_plan_id, plan_version, status, transfer_strategy, source_path, destination_path,
            source_device_id, destination_device_id, checksum_algorithm, source_size_bytes, lock_token
        ) VALUES (?, ?, 'EXECUTING', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ingest_plan_id,
            plan_version,
            transfer_strategy,
            source_path,
            destination_path,
            source_device_id,
            destination_device_id,
            checksum_algorithm,
            source_size_bytes,
            lock_token,
        ),
    )
    execution_id = cursor.lastrowid
    assert execution_id is not None
    for order, step_type in enumerate(step_types, start=1):
        connection.execute(
            "INSERT INTO ingest_execution_steps (ingest_execution_id, step_order, step_type, status) "
            "VALUES (?, ?, ?, 'PENDING')",
            (execution_id, order, step_type),
        )
    execution = get_execution(connection, execution_id)
    assert execution is not None
    return execution


def begin_step(connection: sqlite3.Connection, *, execution_id: int, step_order: int) -> ExecutionStepRecord:
    """Move one pre-seeded step from PENDING to RUNNING. Its own short
    transaction -- the real filesystem work for this step happens
    outside any open database transaction."""
    connection.execute(
        "UPDATE ingest_execution_steps SET status = 'RUNNING', started_at = CURRENT_TIMESTAMP "
        "WHERE ingest_execution_id = ? AND step_order = ?",
        (execution_id, step_order),
    )
    row = connection.execute(
        "SELECT * FROM ingest_execution_steps WHERE ingest_execution_id = ? AND step_order = ?",
        (execution_id, step_order),
    ).fetchone()
    assert row is not None
    return _row_to_step(row)


def complete_step(
    connection: sqlite3.Connection, *, step_id: int, status: str, detail: dict[str, object] | None = None
) -> ExecutionStepRecord:
    """Mutate one step row in place to a terminal status (SUCCEEDED,
    FAILED, or SKIPPED). Steps are not append-only like plan actions --
    each row is exactly one real, currently-attempted step; a future
    retry (deferred, see docs/EXECUTION-SAFETY.md) would need a whole
    new `ingest_executions` row, never a new step row."""
    connection.execute(
        "UPDATE ingest_execution_steps SET status = ?, completed_at = CURRENT_TIMESTAMP, detail_json = ? "
        "WHERE id = ?",
        (status, _serialize_detail(detail), step_id),
    )
    row = connection.execute("SELECT * FROM ingest_execution_steps WHERE id = ?", (step_id,)).fetchone()
    assert row is not None
    return _row_to_step(row)


def mark_execution_succeeded(
    connection: sqlite3.Connection,
    execution_id: int,
    *,
    destination_checksum: str | None,
    destination_size_bytes: int,
) -> ExecutionRecord:
    connection.execute(
        "UPDATE ingest_executions SET status = 'SUCCEEDED', completed_at = CURRENT_TIMESTAMP, "
        "destination_checksum = ?, destination_size_bytes = ?, recovery_status = 'NONE', "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (destination_checksum, destination_size_bytes, execution_id),
    )
    execution = get_execution(connection, execution_id)
    assert execution is not None
    return execution


def mark_execution_failed(
    connection: sqlite3.Connection, execution_id: int, *, failure_step: str, failure_message: str
) -> ExecutionRecord:
    """For a failure strictly before any filesystem mutation began --
    `recovery_status='NONE'` because nothing needs recovering."""
    connection.execute(
        "UPDATE ingest_executions SET status = 'FAILED', completed_at = CURRENT_TIMESTAMP, "
        "failure_step = ?, failure_message = ?, recovery_status = 'NONE', updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ?",
        (failure_step, failure_message, execution_id),
    )
    execution = get_execution(connection, execution_id)
    assert execution is not None
    return execution


def mark_execution_recovery_required(
    connection: sqlite3.Connection,
    execution_id: int,
    *,
    failure_step: str,
    failure_message: str,
    recovery_status: str,
) -> ExecutionRecord:
    """For a failure at or after the point some filesystem mutation was
    attempted -- an operator must inspect via `mams ingest recovery`
    before anything further happens to either copy."""
    connection.execute(
        "UPDATE ingest_executions SET status = 'RECOVERY_REQUIRED', completed_at = CURRENT_TIMESTAMP, "
        "failure_step = ?, failure_message = ?, recovery_status = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ?",
        (failure_step, failure_message, recovery_status, execution_id),
    )
    execution = get_execution(connection, execution_id)
    assert execution is not None
    return execution


def record_source_removed(connection: sqlite3.Connection, execution_id: int) -> None:
    connection.execute(
        "UPDATE ingest_executions SET source_removed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ?",
        (execution_id,),
    )


def record_inventory_refresh_completed(connection: sqlite3.Connection, execution_id: int) -> None:
    connection.execute(
        "UPDATE ingest_executions SET inventory_refresh_completed_at = CURRENT_TIMESTAMP, "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (execution_id,),
    )


def record_plex_refresh_status(connection: sqlite3.Connection, execution_id: int, status: str) -> None:
    connection.execute(
        "UPDATE ingest_executions SET plex_refresh_status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, execution_id),
    )


def transition_plan_after_execution(
    connection: sqlite3.Connection, plan_id: int, *, new_status: str, executed_at: str | None
) -> None:
    """Write the plan's terminal post-execution status
    (EXECUTED/EXECUTION_FAILED/RECOVERY_REQUIRED). By this point the
    plan is already exclusively owned (EXECUTING, lock still held by
    this process) -- no WHERE-status guard is needed the way
    `transition_plan_to_executing` needs one.

    `new_status='EXECUTED'` must only ever be passed from exactly one
    call site: the very last line of a successful `execute_plan()` run.
    A crash at any point before that call leaves the plan
    RECOVERY_REQUIRED, never silently EXECUTED and never back to
    APPROVED."""
    if executed_at is not None:
        connection.execute(
            "UPDATE ingest_plans SET status = ?, executed_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_status, executed_at, plan_id),
        )
    else:
        connection.execute(
            "UPDATE ingest_plans SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_status, plan_id),
        )


def get_execution(connection: sqlite3.Connection, execution_id: int) -> ExecutionRecord | None:
    row = connection.execute("SELECT * FROM ingest_executions WHERE id = ?", (execution_id,)).fetchone()
    if row is None:
        return None
    return _row_to_execution(row, _steps_for_execution(connection, execution_id))


def get_current_execution_for_plan(connection: sqlite3.Connection, plan_id: int) -> ExecutionRecord | None:
    """The most recent `ingest_executions` row for this plan (by id), or
    `None` if the plan has never been executed."""
    row = connection.execute(
        "SELECT * FROM ingest_executions WHERE ingest_plan_id = ? ORDER BY id DESC LIMIT 1", (plan_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_execution(row, _steps_for_execution(connection, row["id"]))


def list_executions(
    connection: sqlite3.Connection,
    *,
    status: str | None = None,
    recovery_status: str | None = None,
    plan_id: int | None = None,
    limit: int | None = None,
) -> list[ExecutionRecord]:
    """List execution history for browsing, most recent first. Two
    queries total regardless of result count: one for executions, one
    for all their steps."""
    clauses: list[str] = []
    params: list[object] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if recovery_status is not None:
        clauses.append("recovery_status = ?")
        params.append(recovery_status)
    if plan_id is not None:
        clauses.append("ingest_plan_id = ?")
        params.append(plan_id)

    sql = "SELECT * FROM ingest_executions"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    rows = connection.execute(sql, params).fetchall()
    steps_by_execution = {row["id"]: _steps_for_execution(connection, row["id"]) for row in rows}
    return [_row_to_execution(row, steps_by_execution[row["id"]]) for row in rows]
