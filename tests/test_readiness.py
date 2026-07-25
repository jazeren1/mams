"""Tests for readiness.py: the pure execution-readiness audit engine.

Pure-function tests only -- no SQL, no filesystem access, matching the
module's own no-dependency contract. `ingest_service.audit_plan()`'s
gathering logic (the SQL/filesystem side) is covered separately in
test_ingest_service.py.
"""

from __future__ import annotations

from dataclasses import replace

from mams.readiness import (
    KNOWN_ACTION_TYPES,
    PlanActionSnapshot,
    ReadinessInput,
    ReadinessStatus,
    evaluate_readiness,
)

_ACTIONS = (
    PlanActionSnapshot(action_order=1, action_type="VALIDATE_SOURCE", execution_state="PROPOSED_NOT_EXECUTED", overwrite_requested=False),
    PlanActionSnapshot(action_order=2, action_type="VERIFY_MEDIA", execution_state="PROPOSED_NOT_EXECUTED", overwrite_requested=False),
    PlanActionSnapshot(action_order=3, action_type="CREATE_DIRECTORY", execution_state="PROPOSED_NOT_EXECUTED", overwrite_requested=False),
    PlanActionSnapshot(action_order=4, action_type="MOVE", execution_state="PROPOSED_NOT_EXECUTED", overwrite_requested=False),
    PlanActionSnapshot(action_order=5, action_type="REFRESH_INVENTORY", execution_state="PROPOSED_NOT_EXECUTED", overwrite_requested=False),
    PlanActionSnapshot(action_order=6, action_type="REQUEST_PLEX_REFRESH", execution_state="PROPOSED_NOT_EXECUTED", overwrite_requested=False),
)


def _ready_input(**overrides: object) -> ReadinessInput:
    base = ReadinessInput(
        plan_status="APPROVED",
        plan_source_path="/Incoming/Alien (1979).mkv",
        plan_source_size_bytes=1_000_000,
        plan_source_mtime=1700000000.0,
        plan_identification_candidate_id=10,
        plan_identification_override_id=None,
        plan_media_identity_assignment_id=20,
        plan_destination_library="/NAS/Movies",
        plan_destination_directory="/NAS/Movies/Alien (1979)",
        plan_destination_filename="Alien (1979).mkv",
        plan_verification_status="PASS",
        plan_actions=_ACTIONS,
        source_media_file_exists=True,
        source_state="ACTIVE",
        source_absolute_path="/Incoming/Alien (1979).mkv",
        source_size_bytes=1_000_000,
        source_mtime=1700000000.0,
        current_identification_candidate_id=10,
        current_active_override_id=None,
        current_active_assignment_id=20,
        external_identity_exists=True,
        current_blocking_finding_count=0,
        destination_root_configured=True,
        destination_path_inside_root=True,
        destination_still_unoccupied=True,
        competing_plan_id=None,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _failed_codes(result) -> set[str]:  # type: ignore[no-untyped-def]
    return {c.code for c in result.checks if not c.passed}


def test_fully_ready_plan_is_ready_for_executor() -> None:
    result = evaluate_readiness(_ready_input())
    assert result.readiness_status == ReadinessStatus.READY_FOR_EXECUTOR
    assert all(c.passed for c in result.checks)
    assert len(result.checks) == 25


def test_execution_status_is_always_not_executed() -> None:
    result = evaluate_readiness(_ready_input())
    assert result.to_dict()["execution_status"] == "NOT_EXECUTED"


# --- BLOCKED scenarios ---------------------------------------------------------


def test_unapproved_plan_is_blocked() -> None:
    result = evaluate_readiness(_ready_input(plan_status="READY_FOR_REVIEW"))
    assert result.readiness_status == ReadinessStatus.BLOCKED
    assert "plan_status_approved" in _failed_codes(result)


def test_superseded_plan_is_blocked() -> None:
    result = evaluate_readiness(_ready_input(plan_status="SUPERSEDED"))
    assert result.readiness_status == ReadinessStatus.BLOCKED
    assert {"plan_status_approved", "plan_not_superseded"} <= _failed_codes(result)


def test_blocking_finding_blocks() -> None:
    result = evaluate_readiness(_ready_input(current_blocking_finding_count=2))
    assert result.readiness_status == ReadinessStatus.BLOCKED
    assert "no_current_blocking_findings" in _failed_codes(result)


def test_destination_collision_after_approval_blocks() -> None:
    result = evaluate_readiness(_ready_input(destination_still_unoccupied=False))
    assert result.readiness_status == ReadinessStatus.BLOCKED
    assert "destination_still_unoccupied" in _failed_codes(result)


def test_competing_plan_blocks() -> None:
    result = evaluate_readiness(_ready_input(competing_plan_id=99))
    assert result.readiness_status == ReadinessStatus.BLOCKED
    assert "no_competing_current_plan" in _failed_codes(result)


def test_overwrite_requested_blocks() -> None:
    actions = _ACTIONS[:3] + (
        PlanActionSnapshot(action_order=4, action_type="MOVE", execution_state="PROPOSED_NOT_EXECUTED", overwrite_requested=True),
    ) + _ACTIONS[4:]
    result = evaluate_readiness(_ready_input(plan_actions=actions))
    assert result.readiness_status == ReadinessStatus.BLOCKED
    assert "overwrite_not_requested" in _failed_codes(result)


# --- STALE scenarios -------------------------------------------------------------


def test_missing_source_is_stale() -> None:
    result = evaluate_readiness(
        _ready_input(source_media_file_exists=False, source_state=None, source_absolute_path=None, source_size_bytes=None, source_mtime=None)
    )
    assert result.readiness_status == ReadinessStatus.STALE
    assert "source_media_file_exists" in _failed_codes(result)


def test_source_no_longer_active_is_stale() -> None:
    result = evaluate_readiness(_ready_input(source_state="MISSING"))
    assert result.readiness_status == ReadinessStatus.STALE
    assert "source_state_active" in _failed_codes(result)


def test_changed_source_size_is_stale() -> None:
    result = evaluate_readiness(_ready_input(source_size_bytes=2_000_000))
    assert result.readiness_status == ReadinessStatus.STALE
    assert "source_size_matches_plan_snapshot" in _failed_codes(result)


def test_changed_source_mtime_is_stale() -> None:
    result = evaluate_readiness(_ready_input(source_mtime=1800000000.0))
    assert result.readiness_status == ReadinessStatus.STALE
    assert "source_mtime_matches_plan_snapshot" in _failed_codes(result)


def test_stale_candidate_due_to_reparse_is_stale() -> None:
    result = evaluate_readiness(_ready_input(current_identification_candidate_id=999))
    assert result.readiness_status == ReadinessStatus.STALE
    assert "current_candidate_matches_plan" in _failed_codes(result)


def test_stale_candidate_due_to_new_override_is_stale() -> None:
    # An override was added after the assignment/plan were made -- the
    # underlying identification_candidates row id is unchanged, but the
    # override linkage now differs.
    result = evaluate_readiness(_ready_input(current_active_override_id=5))
    assert result.readiness_status == ReadinessStatus.STALE
    assert "current_candidate_matches_plan" in _failed_codes(result)


def test_stale_assignment_is_stale() -> None:
    result = evaluate_readiness(_ready_input(current_active_assignment_id=21))
    assert result.readiness_status == ReadinessStatus.STALE
    assert "active_assignment_matches_plan" in _failed_codes(result)


def test_missing_identity_is_stale() -> None:
    result = evaluate_readiness(_ready_input(external_identity_exists=False))
    assert result.readiness_status == ReadinessStatus.STALE
    assert "external_identity_exists" in _failed_codes(result)


def test_failed_verification_snapshot_is_stale() -> None:
    result = evaluate_readiness(_ready_input(plan_verification_status="FAIL"))
    assert result.readiness_status == ReadinessStatus.STALE
    assert "verification_snapshot_passed" in _failed_codes(result)


def test_missing_verification_snapshot_is_stale() -> None:
    result = evaluate_readiness(_ready_input(plan_verification_status=None))
    assert result.readiness_status == ReadinessStatus.STALE
    assert {"verification_snapshot_present", "verification_snapshot_passed"} <= _failed_codes(result)


def test_destination_root_removed_from_config_is_stale() -> None:
    result = evaluate_readiness(_ready_input(destination_root_configured=False))
    assert result.readiness_status == ReadinessStatus.STALE
    assert "destination_root_configured" in _failed_codes(result)


def test_destination_outside_root_is_stale() -> None:
    result = evaluate_readiness(_ready_input(destination_path_inside_root=False))
    assert result.readiness_status == ReadinessStatus.STALE
    assert "destination_path_inside_root" in _failed_codes(result)


def test_no_destination_specified_is_stale() -> None:
    result = evaluate_readiness(
        _ready_input(plan_destination_library=None, plan_destination_directory=None, plan_destination_filename=None)
    )
    assert result.readiness_status == ReadinessStatus.STALE
    assert "destination_category_valid" in _failed_codes(result)


# --- INCOMPLETE scenarios ---------------------------------------------------------


def test_missing_actions_is_incomplete() -> None:
    result = evaluate_readiness(_ready_input(plan_actions=()))
    assert result.readiness_status == ReadinessStatus.INCOMPLETE
    assert "proposed_actions_present" in _failed_codes(result)


def test_incorrect_action_order_is_incomplete() -> None:
    actions = (
        PlanActionSnapshot(action_order=1, action_type="VALIDATE_SOURCE", execution_state="PROPOSED_NOT_EXECUTED", overwrite_requested=False),
        PlanActionSnapshot(action_order=3, action_type="VERIFY_MEDIA", execution_state="PROPOSED_NOT_EXECUTED", overwrite_requested=False),
    )
    result = evaluate_readiness(_ready_input(plan_actions=actions))
    assert result.readiness_status == ReadinessStatus.INCOMPLETE
    assert "action_order_valid" in _failed_codes(result)


def test_unsupported_action_type_is_incomplete() -> None:
    actions = (
        PlanActionSnapshot(action_order=1, action_type="DELETE_SOURCE", execution_state="PROPOSED_NOT_EXECUTED", overwrite_requested=False),
    )
    result = evaluate_readiness(_ready_input(plan_actions=actions))
    assert result.readiness_status == ReadinessStatus.INCOMPLETE
    assert {"action_types_supported_by_future_executor", "no_unknown_or_unsupported_action"} <= _failed_codes(result)


def test_non_proposed_action_state_is_incomplete() -> None:
    actions = (
        PlanActionSnapshot(action_order=1, action_type="MOVE", execution_state="EXECUTED", overwrite_requested=False),
    )
    result = evaluate_readiness(_ready_input(plan_actions=actions))
    assert result.readiness_status == ReadinessStatus.INCOMPLETE
    assert "every_action_state_is_proposed_not_executed" in _failed_codes(result)


# --- precedence -----------------------------------------------------------------


def test_blocked_takes_precedence_over_stale_and_incomplete() -> None:
    result = evaluate_readiness(
        _ready_input(plan_status="READY_FOR_REVIEW", source_state="MISSING", plan_actions=())
    )
    assert result.readiness_status == ReadinessStatus.BLOCKED


def test_stale_takes_precedence_over_incomplete() -> None:
    result = evaluate_readiness(_ready_input(source_state="MISSING", plan_actions=()))
    assert result.readiness_status == ReadinessStatus.STALE


# --- determinism / structure -------------------------------------------------------


def test_all_checks_always_run_regardless_of_failures() -> None:
    result = evaluate_readiness(_ready_input(plan_status="BLOCKED", source_media_file_exists=False, plan_actions=()))
    assert len(result.checks) == 25


def test_repeated_calls_produce_identical_output() -> None:
    data = _ready_input()
    first = evaluate_readiness(data)
    second = evaluate_readiness(data)
    assert first.to_dict() == second.to_dict()


def test_known_action_types_matches_schema_enum() -> None:
    # Sanity: the constant this module checks against is exactly the
    # ingest_plan_actions.action_type CHECK constraint's value set
    # (database/migrations/0009_ingest_plans.sql).
    assert KNOWN_ACTION_TYPES == {
        "VALIDATE_SOURCE", "VERIFY_MEDIA", "CREATE_DIRECTORY", "RENAME", "MOVE",
        "REFRESH_INVENTORY", "REQUEST_PLEX_REFRESH",
    }
