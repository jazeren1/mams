"""Persistence for dry-run ingest plans: owns all SQL for `ingest_plans`
and `ingest_plan_actions`. Mirrors `resolution_repository.py`'s role for
its schema -- see docs/DATABASE.md for full table rationale.

`ingest_service.py` is the only caller that combines this module with
`verification.py` and `destination.py` (both pure) and with
`inventory_repository.py`/`identification_repository.py`/
`resolution_repository.py` (other read-only lookups).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass


class PlanNotApprovableError(Exception):
    """Raised by `approve_plan` when the plan doesn't exist or isn't
    READY_FOR_REVIEW -- a BLOCKED or already-APPROVED plan can never be
    approved through this path."""


@dataclass(frozen=True)
class PlanActionInput:
    action_type: str
    source_path: str | None
    destination_path: str | None
    details: dict[str, object] | None = None


@dataclass(frozen=True)
class PlanActionRecord:
    id: int
    ingest_plan_id: int
    action_order: int
    action_type: str
    source_path: str | None
    destination_path: str | None
    details: dict[str, object] | None
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PlanRecord:
    id: int
    media_file_id: int
    category: str | None
    absolute_path: str | None
    identification_candidate_id: int | None
    media_identity_assignment_id: int | None
    status: str
    source_path: str
    source_size_bytes: int | None
    source_mtime: float | None
    destination_library: str | None
    destination_directory: str | None
    destination_filename: str | None
    plan_version: int
    verification_status: str | None
    verification: dict[str, object] | None
    blocking_reasons: tuple[str, ...]
    summary: str | None
    created_at: str
    updated_at: str
    approved_at: str | None
    approved_by: str | None
    executed_at: str | None
    actions: tuple[PlanActionRecord, ...]

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["blocking_reasons"] = list(self.blocking_reasons)
        data["actions"] = [a.to_dict() for a in self.actions]
        return data


def _serialize_json(value: object) -> str | None:
    if not value:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


_PLAN_BASE_SELECT = """
    SELECT
        ingest_plans.*,
        libraries.category AS category,
        media_files.absolute_path AS absolute_path
    FROM ingest_plans
    LEFT JOIN media_files ON media_files.id = ingest_plans.media_file_id
    LEFT JOIN libraries ON libraries.id = media_files.library_id
"""


def _row_to_action(row: sqlite3.Row) -> PlanActionRecord:
    return PlanActionRecord(
        id=row["id"],
        ingest_plan_id=row["ingest_plan_id"],
        action_order=row["action_order"],
        action_type=row["action_type"],
        source_path=row["source_path"],
        destination_path=row["destination_path"],
        details=json.loads(row["details_json"]) if row["details_json"] is not None else None,
        created_at=row["created_at"],
    )


def _actions_for_plans(connection: sqlite3.Connection, plan_ids: list[int]) -> dict[int, list[PlanActionRecord]]:
    if not plan_ids:
        return {}
    placeholders = ",".join("?" for _ in plan_ids)
    rows = connection.execute(
        f"SELECT * FROM ingest_plan_actions WHERE ingest_plan_id IN ({placeholders}) ORDER BY ingest_plan_id, action_order",
        plan_ids,
    ).fetchall()
    by_plan: dict[int, list[PlanActionRecord]] = {plan_id: [] for plan_id in plan_ids}
    for row in rows:
        by_plan[row["ingest_plan_id"]].append(_row_to_action(row))
    return by_plan


def _row_to_plan(row: sqlite3.Row, actions: tuple[PlanActionRecord, ...]) -> PlanRecord:
    return PlanRecord(
        id=row["id"],
        media_file_id=row["media_file_id"],
        category=row["category"],
        absolute_path=row["absolute_path"],
        identification_candidate_id=row["identification_candidate_id"],
        media_identity_assignment_id=row["media_identity_assignment_id"],
        status=row["status"],
        source_path=row["source_path"],
        source_size_bytes=row["source_size_bytes"],
        source_mtime=row["source_mtime"],
        destination_library=row["destination_library"],
        destination_directory=row["destination_directory"],
        destination_filename=row["destination_filename"],
        plan_version=row["plan_version"],
        verification_status=row["verification_status"],
        verification=json.loads(row["verification_json"]) if row["verification_json"] is not None else None,
        blocking_reasons=tuple(json.loads(row["blocking_reasons_json"])) if row["blocking_reasons_json"] is not None else (),
        summary=row["summary"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        approved_at=row["approved_at"],
        approved_by=row["approved_by"],
        executed_at=row["executed_at"],
        actions=actions,
    )


def get_plan(connection: sqlite3.Connection, plan_id: int) -> PlanRecord | None:
    row = connection.execute(_PLAN_BASE_SELECT + " WHERE ingest_plans.id = ?", (plan_id,)).fetchone()
    if row is None:
        return None
    actions = _actions_for_plans(connection, [plan_id]).get(plan_id, [])
    return _row_to_plan(row, tuple(actions))


def get_current_plan_for_media_file(connection: sqlite3.Connection, media_file_id: int) -> PlanRecord | None:
    row = connection.execute(
        _PLAN_BASE_SELECT + " WHERE ingest_plans.media_file_id = ? AND ingest_plans.status != 'SUPERSEDED'",
        (media_file_id,),
    ).fetchone()
    if row is None:
        return None
    actions = _actions_for_plans(connection, [row["id"]]).get(row["id"], [])
    return _row_to_plan(row, tuple(actions))


def list_plans(
    connection: sqlite3.Connection,
    *,
    status: str | None = None,
    category: str | None = None,
    limit: int | None = None,
) -> list[PlanRecord]:
    """List ingest_plans for browsing, most recently updated first. Two
    queries total regardless of result count: one for plans, one for all
    their actions."""
    clauses: list[str] = []
    params: list[object] = []
    if status is not None:
        clauses.append("ingest_plans.status = ?")
        params.append(status)
    if category is not None:
        clauses.append("libraries.category = ?")
        params.append(category)

    sql = _PLAN_BASE_SELECT
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY ingest_plans.updated_at DESC, ingest_plans.id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    rows = connection.execute(sql, params).fetchall()
    plan_ids = [row["id"] for row in rows]
    actions_by_plan = _actions_for_plans(connection, plan_ids)
    return [_row_to_plan(row, tuple(actions_by_plan.get(row["id"], []))) for row in rows]


@dataclass(frozen=True)
class PlanStats:
    total_count: int
    status_counts: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def get_plan_stats(connection: sqlite3.Connection) -> PlanStats:
    total = connection.execute("SELECT COUNT(*) AS n FROM ingest_plans").fetchone()["n"]
    status_counts = {
        row["status"]: row["n"]
        for row in connection.execute("SELECT status, COUNT(*) AS n FROM ingest_plans GROUP BY status").fetchall()
    }
    return PlanStats(total_count=total, status_counts=status_counts)


# Deliberately excludes `status`: it is compared separately in
# reconcile_plan() because an APPROVED plan's *content* can be identical
# to what a fresh regeneration computes while the freshly-computed status
# is never itself "APPROVED" (only the `approve` command sets that) --
# comparing status as ordinary content would make every regeneration look
# like a change and needlessly supersede every approved plan.
_PLAN_CONTENT_COLUMNS = (
    "identification_candidate_id",
    "media_identity_assignment_id",
    "source_path",
    "source_size_bytes",
    "source_mtime",
    "destination_library",
    "destination_directory",
    "destination_filename",
    "verification_status",
    "verification_json",
    "blocking_reasons_json",
    "summary",
)


def _replace_actions(connection: sqlite3.Connection, plan_id: int, actions: list[PlanActionInput]) -> None:
    connection.execute("DELETE FROM ingest_plan_actions WHERE ingest_plan_id = ?", (plan_id,))
    for order, action in enumerate(actions, start=1):
        connection.execute(
            """
            INSERT INTO ingest_plan_actions (
                ingest_plan_id, action_order, action_type, source_path, destination_path, details_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (plan_id, order, action.action_type, action.source_path, action.destination_path, _serialize_json(action.details)),
        )


def reconcile_plan(
    connection: sqlite3.Connection,
    *,
    media_file_id: int,
    identification_candidate_id: int | None,
    media_identity_assignment_id: int | None,
    status: str,
    source_path: str,
    source_size_bytes: int | None,
    source_mtime: float | None,
    destination_library: str | None,
    destination_directory: str | None,
    destination_filename: str | None,
    verification_status: str | None,
    verification: dict[str, object] | None,
    blocking_reasons: list[str],
    summary: str | None,
    actions: list[PlanActionInput],
) -> PlanRecord:
    """Create or reconcile the one current (non-SUPERSEDED) dry-run plan
    for a media file. Not itself transactional -- callers run this inside
    `with connection:`.

    - No current plan exists -> INSERT (plan_version=1) plus its actions.
    - Current plan exists, not APPROVED, content+status identical -> left
      completely untouched (no UPDATE, no action rewrite, no `updated_at`
      churn).
    - Current plan exists, not APPROVED, content or status differs ->
      UPDATE in place (plan_version incremented), actions replaced
      wholesale.
    - Current plan exists, IS APPROVED, content identical and the freshly
      computed `status` is still READY_FOR_REVIEW -> left completely
      untouched (an approval remains valid; content is compared
      separately from status here specifically so a routine regeneration
      that reaches the same READY_FOR_REVIEW conclusion never looks like
      a change merely because `status` isn't literally "APPROVED").
    - Current plan exists, IS APPROVED, and either the content changed or
      the freshly computed status is no longer READY_FOR_REVIEW (the
      approval is stale) -> the approved row is marked SUPERSEDED (an
      approval must never be silently mutated) and a new current plan is
      inserted with an incremented plan_version, carrying its own fresh
      actions.

    `source_size_bytes`/`source_mtime` (Milestone 7C) snapshot the source
    file's size/mtime at generation time -- included in
    `_PLAN_CONTENT_COLUMNS` like every other content column, so a source
    file that changes size or mtime between plan generations is treated
    as a content change like any other: an APPROVED plan whose source has
    since changed on the next regeneration goes SUPERSEDED, the same
    mechanism a destination or verification change already triggers.
    """
    verification_json = _serialize_json(verification)
    blocking_reasons_json = _serialize_json(blocking_reasons)
    new_content = (
        identification_candidate_id,
        media_identity_assignment_id,
        source_path,
        source_size_bytes,
        source_mtime,
        destination_library,
        destination_directory,
        destination_filename,
        verification_status,
        verification_json,
        blocking_reasons_json,
        summary,
    )

    existing = connection.execute(
        "SELECT * FROM ingest_plans WHERE media_file_id = ? AND status != 'SUPERSEDED'", (media_file_id,)
    ).fetchone()

    if existing is None:
        return _insert_plan(connection, media_file_id, status, new_content, actions, plan_version=1)

    existing_content = tuple(existing[column] for column in _PLAN_CONTENT_COLUMNS)
    content_unchanged = existing_content == new_content

    if existing["status"] == "APPROVED":
        if content_unchanged and status == "READY_FOR_REVIEW":
            plan = get_plan(connection, existing["id"])
            assert plan is not None
            return plan
        connection.execute(
            "UPDATE ingest_plans SET status = 'SUPERSEDED', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (existing["id"],),
        )
        return _insert_plan(
            connection, media_file_id, status, new_content, actions, plan_version=existing["plan_version"] + 1
        )

    if content_unchanged and existing["status"] == status:
        plan = get_plan(connection, existing["id"])
        assert plan is not None
        return plan

    set_clause = ", ".join(f"{column} = ?" for column in _PLAN_CONTENT_COLUMNS)
    connection.execute(
        f"UPDATE ingest_plans SET status = ?, {set_clause}, plan_version = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, *new_content, existing["plan_version"] + 1, existing["id"]),
    )
    _replace_actions(connection, existing["id"], actions)
    plan = get_plan(connection, existing["id"])
    assert plan is not None
    return plan


def _insert_plan(
    connection: sqlite3.Connection,
    media_file_id: int,
    status: str,
    content: tuple[object, ...],
    actions: list[PlanActionInput],
    *,
    plan_version: int,
) -> PlanRecord:
    cursor = connection.execute(
        f"""
        INSERT INTO ingest_plans (
            media_file_id, status, {", ".join(_PLAN_CONTENT_COLUMNS)}, plan_version
        ) VALUES (?, ?, {", ".join("?" for _ in _PLAN_CONTENT_COLUMNS)}, ?)
        """,
        (media_file_id, status, *content, plan_version),
    )
    plan_id = cursor.lastrowid
    assert plan_id is not None
    _replace_actions(connection, plan_id, actions)
    plan = get_plan(connection, plan_id)
    assert plan is not None
    return plan


def approve_plan(connection: sqlite3.Connection, plan_id: int, *, approved_by: str = "MANUAL_CLI") -> PlanRecord:
    """Flip a READY_FOR_REVIEW plan to APPROVED. Database state only --
    no filesystem change, no Plex call. Raises `PlanNotApprovableError`
    for an unknown plan id or any status other than READY_FOR_REVIEW."""
    row = connection.execute("SELECT status FROM ingest_plans WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        raise PlanNotApprovableError(f"No ingest plan with id {plan_id}")
    if row["status"] != "READY_FOR_REVIEW":
        raise PlanNotApprovableError(f"Plan {plan_id} is {row['status']}, not READY_FOR_REVIEW -- cannot approve")
    connection.execute(
        "UPDATE ingest_plans SET status = 'APPROVED', approved_at = CURRENT_TIMESTAMP, approved_by = ?, "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (approved_by, plan_id),
    )
    plan = get_plan(connection, plan_id)
    assert plan is not None
    return plan
