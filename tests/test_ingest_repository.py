"""Tests for ingest_repository.py: plan reconciliation lifecycle,
action ordering, approval, and query layer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mams.db import connect, migrate
from mams.ingest_repository import (
    PlanActionInput,
    PlanNotApprovableError,
    approve_plan,
    get_current_plan_for_media_file,
    get_plan_stats,
    list_plans,
    reconcile_plan,
)

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"


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


def _get_or_create_library(connection: sqlite3.Connection, category: str) -> int:
    row = connection.execute("SELECT id FROM libraries WHERE category = ?", (category,)).fetchone()
    if row is not None:
        return int(row["id"])
    return _lastrowid(connection.execute("INSERT INTO libraries (category, root_path) VALUES (?, ?)", (category, "/Incoming")))


def _seed_media_file(connection: sqlite3.Connection, *, name: str = "Alien.mkv", category: str = "incoming") -> int:
    library_id = _get_or_create_library(connection, category)
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


def _plan_kwargs(media_file_id: int, **overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = dict(
        media_file_id=media_file_id,
        identification_candidate_id=None,
        media_identity_assignment_id=None,
        status="DRAFT",
        source_path=f"/Incoming/file{media_file_id}.mkv",
        source_size_bytes=1_000_000,
        source_mtime=1700000000.0,
        destination_library=None,
        destination_directory=None,
        destination_filename=None,
        verification_status=None,
        verification=None,
        blocking_reasons=[],
        summary=None,
        actions=[],
    )
    defaults.update(overrides)
    return defaults


def test_reconcile_plan_creates_new_plan(connection: sqlite3.Connection) -> None:
    media_file_id = _seed_media_file(connection)
    plan = reconcile_plan(connection, **_plan_kwargs(media_file_id, status="READY_FOR_REVIEW"))
    assert plan.status == "READY_FOR_REVIEW"
    assert plan.plan_version == 1
    current = get_current_plan_for_media_file(connection, media_file_id)
    assert current is not None
    assert current.id == plan.id


def test_reconcile_plan_with_actions_persists_ordered_actions(connection: sqlite3.Connection) -> None:
    media_file_id = _seed_media_file(connection)
    actions = [
        PlanActionInput(action_type="VALIDATE_SOURCE", source_path="/Incoming/x.mkv", destination_path=None),
        PlanActionInput(action_type="VERIFY_MEDIA", source_path="/Incoming/x.mkv", destination_path=None),
        PlanActionInput(action_type="MOVE", source_path="/Incoming/x.mkv", destination_path="/Movies/X (2001)/X (2001).mkv"),
    ]
    plan = reconcile_plan(connection, **_plan_kwargs(media_file_id, status="READY_FOR_REVIEW", actions=actions))
    assert [a.action_type for a in plan.actions] == ["VALIDATE_SOURCE", "VERIFY_MEDIA", "MOVE"]
    assert [a.action_order for a in plan.actions] == [1, 2, 3]


def test_reconcile_plan_unchanged_produces_no_writes(connection: sqlite3.Connection) -> None:
    media_file_id = _seed_media_file(connection)
    kwargs = _plan_kwargs(media_file_id, status="READY_FOR_REVIEW")
    first = reconcile_plan(connection, **kwargs)
    second = reconcile_plan(connection, **kwargs)
    assert first.id == second.id
    assert first.updated_at == second.updated_at
    assert first.plan_version == second.plan_version == 1
    count = connection.execute("SELECT COUNT(*) FROM ingest_plans").fetchone()[0]
    assert count == 1


def test_reconcile_plan_changed_content_updates_in_place(connection: sqlite3.Connection) -> None:
    media_file_id = _seed_media_file(connection)
    first = reconcile_plan(connection, **_plan_kwargs(media_file_id, status="BLOCKED", blocking_reasons=["x"]))
    second = reconcile_plan(connection, **_plan_kwargs(media_file_id, status="READY_FOR_REVIEW", blocking_reasons=[]))
    assert first.id == second.id
    assert second.status == "READY_FOR_REVIEW"
    assert second.plan_version == 2
    count = connection.execute("SELECT COUNT(*) FROM ingest_plans").fetchone()[0]
    assert count == 1


def test_reconcile_plan_supersedes_approved_plan_on_change(connection: sqlite3.Connection) -> None:
    media_file_id = _seed_media_file(connection)
    first = reconcile_plan(connection, **_plan_kwargs(media_file_id, status="READY_FOR_REVIEW"))
    approved = approve_plan(connection, first.id)
    assert approved.status == "APPROVED"

    second = reconcile_plan(connection, **_plan_kwargs(media_file_id, status="BLOCKED", blocking_reasons=["source changed"]))

    assert second.id != first.id
    assert second.plan_version == 2
    count = connection.execute("SELECT COUNT(*) FROM ingest_plans").fetchone()[0]
    assert count == 2
    superseded = connection.execute("SELECT status FROM ingest_plans WHERE id = ?", (first.id,)).fetchone()
    assert superseded["status"] == "SUPERSEDED"
    current = get_current_plan_for_media_file(connection, media_file_id)
    assert current is not None
    assert current.id == second.id


def test_source_snapshot_round_trips(connection: sqlite3.Connection) -> None:
    media_file_id = _seed_media_file(connection)
    plan = reconcile_plan(
        connection,
        **_plan_kwargs(media_file_id, status="READY_FOR_REVIEW", source_size_bytes=123456, source_mtime=1700000123.5),
    )
    assert plan.source_size_bytes == 123456
    assert plan.source_mtime == 1700000123.5


def test_source_size_change_supersedes_approved_plan(connection: sqlite3.Connection) -> None:
    media_file_id = _seed_media_file(connection)
    first = reconcile_plan(connection, **_plan_kwargs(media_file_id, status="READY_FOR_REVIEW", source_size_bytes=1000))
    approve_plan(connection, first.id)

    second = reconcile_plan(connection, **_plan_kwargs(media_file_id, status="READY_FOR_REVIEW", source_size_bytes=2000))

    assert second.id != first.id
    superseded = connection.execute("SELECT status FROM ingest_plans WHERE id = ?", (first.id,)).fetchone()
    assert superseded["status"] == "SUPERSEDED"
    assert second.source_size_bytes == 2000


def test_source_mtime_change_supersedes_approved_plan(connection: sqlite3.Connection) -> None:
    media_file_id = _seed_media_file(connection)
    first = reconcile_plan(connection, **_plan_kwargs(media_file_id, status="READY_FOR_REVIEW", source_mtime=1000.0))
    approve_plan(connection, first.id)

    second = reconcile_plan(connection, **_plan_kwargs(media_file_id, status="READY_FOR_REVIEW", source_mtime=2000.0))

    assert second.id != first.id
    superseded = connection.execute("SELECT status FROM ingest_plans WHERE id = ?", (first.id,)).fetchone()
    assert superseded["status"] == "SUPERSEDED"


def test_reconcile_plan_leaves_approved_plan_untouched_when_unchanged(connection: sqlite3.Connection) -> None:
    """A regeneration that reaches the same READY_FOR_REVIEW conclusion
    against unchanged content must never disturb an existing approval --
    the row stays APPROVED, not silently reset to READY_FOR_REVIEW."""
    media_file_id = _seed_media_file(connection)
    kwargs = _plan_kwargs(media_file_id, status="READY_FOR_REVIEW")
    first = reconcile_plan(connection, **kwargs)
    approved = approve_plan(connection, first.id)
    second = reconcile_plan(connection, **kwargs)
    assert second.id == first.id
    assert second.status == "APPROVED"
    assert second.updated_at == approved.updated_at
    count = connection.execute("SELECT COUNT(*) FROM ingest_plans").fetchone()[0]
    assert count == 1


def test_blocked_plan(connection: sqlite3.Connection) -> None:
    media_file_id = _seed_media_file(connection)
    plan = reconcile_plan(connection, **_plan_kwargs(media_file_id, status="BLOCKED", blocking_reasons=["identity unresolved"]))
    assert plan.status == "BLOCKED"
    assert plan.blocking_reasons == ("identity unresolved",)


def test_review_required_plan(connection: sqlite3.Connection) -> None:
    media_file_id = _seed_media_file(connection)
    plan = reconcile_plan(connection, **_plan_kwargs(media_file_id, status="REVIEW_REQUIRED", blocking_reasons=["destination category not specified"]))
    assert plan.status == "REVIEW_REQUIRED"


# --- approval -----------------------------------------------------------------


def test_approve_plan_requires_ready_for_review(connection: sqlite3.Connection) -> None:
    media_file_id = _seed_media_file(connection)
    plan = reconcile_plan(connection, **_plan_kwargs(media_file_id, status="BLOCKED", blocking_reasons=["x"]))
    with pytest.raises(PlanNotApprovableError):
        approve_plan(connection, plan.id)


def test_approve_plan_sets_timestamp_and_actor(connection: sqlite3.Connection) -> None:
    media_file_id = _seed_media_file(connection)
    plan = reconcile_plan(connection, **_plan_kwargs(media_file_id, status="READY_FOR_REVIEW"))
    approved = approve_plan(connection, plan.id)
    assert approved.status == "APPROVED"
    assert approved.approved_at is not None
    assert approved.approved_by == "MANUAL_CLI"
    assert approved.executed_at is None


def test_approve_plan_raises_for_unknown_id(connection: sqlite3.Connection) -> None:
    with pytest.raises(PlanNotApprovableError):
        approve_plan(connection, 999999)


# --- query layer -----------------------------------------------------------------


def test_list_plans_filters_by_status(connection: sqlite3.Connection) -> None:
    media_file_id_1 = _seed_media_file(connection, name="A.mkv")
    media_file_id_2 = _seed_media_file(connection, name="B.mkv")
    reconcile_plan(connection, **_plan_kwargs(media_file_id_1, status="READY_FOR_REVIEW"))
    reconcile_plan(connection, **_plan_kwargs(media_file_id_2, status="BLOCKED", blocking_reasons=["x"]))

    ready = list_plans(connection, status="READY_FOR_REVIEW")
    assert len(ready) == 1
    assert ready[0].media_file_id == media_file_id_1


def test_list_plans_uses_a_bounded_number_of_queries(connection: sqlite3.Connection) -> None:
    for i in range(3):
        media_file_id = _seed_media_file(connection, name=f"F{i}.mkv")
        reconcile_plan(connection, **_plan_kwargs(media_file_id, status="READY_FOR_REVIEW"))

    executed: list[str] = []
    connection.set_trace_callback(executed.append)
    list_plans(connection)
    connection.set_trace_callback(None)
    selects = [s for s in executed if s.strip().upper().startswith("SELECT")]
    assert len(selects) == 2


def test_get_plan_stats(connection: sqlite3.Connection) -> None:
    media_file_id_1 = _seed_media_file(connection, name="A.mkv")
    media_file_id_2 = _seed_media_file(connection, name="B.mkv")
    reconcile_plan(connection, **_plan_kwargs(media_file_id_1, status="READY_FOR_REVIEW"))
    reconcile_plan(connection, **_plan_kwargs(media_file_id_2, status="BLOCKED", blocking_reasons=["x"]))

    stats = get_plan_stats(connection)
    assert stats.total_count == 2
    assert stats.status_counts == {"READY_FOR_REVIEW": 1, "BLOCKED": 1}
