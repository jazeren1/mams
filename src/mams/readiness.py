"""Execution-readiness audit for approved dry-run ingest plans (Milestone
7C, Phase F): a pure, read-only, deterministic check that an APPROVED plan
still has every precondition a future Milestone 8 executor will need.

Distinct from verification.py, which judges whether a *file* is
structurally plausible: this module judges whether a *plan* -- the
specific combination of source, identity, verification snapshot,
destination, and proposed actions already persisted -- is still an
accurate, actionable description of what should happen, right now. This
audit is the formal contract between Milestone 7C and Milestone 8: it
does not execute the plan, and no executor exists anywhere in this
codebase.

Pure module: no SQL, no filesystem access, no external calls.
`ingest_service.audit_plan()` is the only caller, gathering a
`ReadinessInput` from the database/config/a fresh read-only
`Path.exists()` check and handing it here -- this module never mutates
anything and is never told to. "The audit reports state; explicit
commands perform changes" (Phase G) -- nothing in this module or its
caller writes to the database.

Every one of the 25 checks always runs and is always reported, regardless
of which check(s) actually determine the overall `readiness_status` -- an
operator should see the full picture, not just the first failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

EXECUTION_STATUS_NOT_EXECUTED = "NOT_EXECUTED"

_PROPOSED_NOT_EXECUTED = "PROPOSED_NOT_EXECUTED"

# The full action_type set this codebase's schema (ingest_plan_actions'
# CHECK constraint, database/migrations/0009_ingest_plans.sql) and a
# future Milestone 8 executor are both expected to support. Referenced by
# two independently-named checks below (action_types_supported_by_future_executor /
# no_unknown_or_unsupported_action) -- deliberately redundant: one asks
# "is this an action an executor should perform", the other "is this even
# a recognized action type at all". Both happen to be the same set today;
# nothing requires them to stay that way if a future milestone narrows
# what the executor actually implements.
KNOWN_ACTION_TYPES = frozenset(
    {
        "VALIDATE_SOURCE",
        "VERIFY_MEDIA",
        "CREATE_DIRECTORY",
        "RENAME",
        "MOVE",
        "REFRESH_INVENTORY",
        "REQUEST_PLEX_REFRESH",
    }
)


class ReadinessStatus(StrEnum):
    READY_FOR_EXECUTOR = "READY_FOR_EXECUTOR"
    STALE = "STALE"
    BLOCKED = "BLOCKED"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True)
class PlanActionSnapshot:
    """The fields of one ingest_plan_actions row the audit actually
    needs, gathered by `ingest_service.audit_plan()` from
    `PlanActionRecord.details`."""

    action_order: int
    action_type: str
    execution_state: str | None
    overwrite_requested: bool


@dataclass(frozen=True)
class ReadinessInput:
    """Everything `evaluate_readiness()` needs, already gathered by
    `ingest_service.audit_plan()` -- plain values only; no SQL or
    filesystem access happens past this point."""

    plan_status: str
    plan_source_path: str
    plan_source_size_bytes: int | None
    plan_source_mtime: float | None
    plan_identification_candidate_id: int | None
    plan_identification_override_id: int | None
    plan_media_identity_assignment_id: int | None
    plan_destination_library: str | None
    plan_destination_directory: str | None
    plan_destination_filename: str | None
    plan_verification_status: str | None
    plan_actions: tuple[PlanActionSnapshot, ...]

    source_media_file_exists: bool
    source_state: str | None
    source_absolute_path: str | None
    source_size_bytes: int | None
    source_mtime: float | None

    current_identification_candidate_id: int | None
    current_active_override_id: int | None

    current_active_assignment_id: int | None
    external_identity_exists: bool

    current_blocking_finding_count: int

    destination_root_configured: bool
    destination_path_inside_root: bool
    destination_still_unoccupied: bool
    competing_plan_id: int | None


@dataclass(frozen=True)
class ReadinessCheck:
    code: str
    passed: bool
    evidence: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "status": "PASS" if self.passed else "FAIL", "evidence": self.evidence}


@dataclass(frozen=True)
class ReadinessResult:
    readiness_status: ReadinessStatus
    checks: tuple[ReadinessCheck, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "execution_status": EXECUTION_STATUS_NOT_EXECUTED,
            "readiness_status": self.readiness_status.value,
            "checks": [check.to_dict() for check in self.checks],
        }


# Check-code groupings for readiness_status precedence: BLOCKED > STALE >
# INCOMPLETE > READY_FOR_EXECUTOR. Each check code appears in exactly one
# group. BLOCKED codes are hard stops the plan itself cannot proceed past
# (wrong approval state, a new blocking condition, an occupied
# destination, a competing plan). STALE codes mean the world has moved
# since this plan was generated/approved (source, identity, verification,
# or destination-config drift) -- regeneration, not blind trust, is
# needed. INCOMPLETE codes mean the plan's own action list is malformed --
# shouldn't happen given the ingest_plan_actions schema constraints, but
# audited defensively rather than assumed.
_BLOCKED_CODES = frozenset(
    {
        "plan_status_approved",
        "plan_not_superseded",
        "no_current_blocking_findings",
        "destination_still_unoccupied",
        "no_competing_current_plan",
        "overwrite_not_requested",
    }
)
_STALE_CODES = frozenset(
    {
        "source_media_file_exists",
        "source_state_active",
        "source_path_matches_plan_snapshot",
        "source_size_matches_plan_snapshot",
        "source_mtime_matches_plan_snapshot",
        "current_candidate_matches_plan",
        "active_assignment_matches_plan",
        "external_identity_exists",
        "verification_snapshot_present",
        "verification_snapshot_passed",
        "destination_category_valid",
        "destination_root_configured",
        "destination_path_inside_root",
    }
)
_INCOMPLETE_CODES = frozenset(
    {
        "proposed_actions_present",
        "action_order_valid",
        "action_types_supported_by_future_executor",
        "every_action_state_is_proposed_not_executed",
        "no_unknown_or_unsupported_action",
    }
)


def evaluate_readiness(data: ReadinessInput) -> ReadinessResult:
    """Run all 25 checks and derive the overall `readiness_status`.
    Deterministic: identical input always produces byte-identical output
    -- checks run in a fixed order, and nothing here reads the wall clock
    or any other non-deterministic source."""
    checks: list[ReadinessCheck] = []

    def _check(code: str, passed: bool, **evidence: object) -> None:
        checks.append(ReadinessCheck(code=code, passed=passed, evidence=evidence))

    _check("plan_exists", True)
    _check("plan_status_approved", data.plan_status == "APPROVED", plan_status=data.plan_status)
    _check("plan_not_superseded", data.plan_status != "SUPERSEDED", plan_status=data.plan_status)

    _check("source_media_file_exists", data.source_media_file_exists)
    _check(
        "source_state_active",
        data.source_media_file_exists and data.source_state == "ACTIVE",
        state=data.source_state,
    )
    _check(
        "source_path_matches_plan_snapshot",
        data.source_media_file_exists and data.source_absolute_path == data.plan_source_path,
        plan_path=data.plan_source_path,
        current_path=data.source_absolute_path,
    )
    _check(
        "source_size_matches_plan_snapshot",
        data.source_media_file_exists and data.source_size_bytes == data.plan_source_size_bytes,
        plan_size_bytes=data.plan_source_size_bytes,
        current_size_bytes=data.source_size_bytes,
    )
    _check(
        "source_mtime_matches_plan_snapshot",
        data.source_media_file_exists and data.source_mtime == data.plan_source_mtime,
        plan_mtime=data.plan_source_mtime,
        current_mtime=data.source_mtime,
    )

    candidate_matches = (
        data.plan_identification_candidate_id is not None
        and data.plan_identification_candidate_id == data.current_identification_candidate_id
        and data.plan_identification_override_id == data.current_active_override_id
    )
    _check(
        "current_candidate_matches_plan",
        candidate_matches,
        plan_candidate_id=data.plan_identification_candidate_id,
        current_candidate_id=data.current_identification_candidate_id,
        plan_override_id=data.plan_identification_override_id,
        current_override_id=data.current_active_override_id,
    )

    assignment_matches = (
        data.plan_media_identity_assignment_id is not None
        and data.plan_media_identity_assignment_id == data.current_active_assignment_id
    )
    _check(
        "active_assignment_matches_plan",
        assignment_matches,
        plan_assignment_id=data.plan_media_identity_assignment_id,
        current_assignment_id=data.current_active_assignment_id,
    )
    _check("external_identity_exists", data.external_identity_exists)

    _check("verification_snapshot_present", data.plan_verification_status is not None)
    _check("verification_snapshot_passed", data.plan_verification_status == "PASS")
    _check(
        "no_current_blocking_findings",
        data.current_blocking_finding_count == 0,
        count=data.current_blocking_finding_count,
    )

    destination_specified = (
        data.plan_destination_library is not None
        and data.plan_destination_directory is not None
        and data.plan_destination_filename is not None
    )
    _check("destination_category_valid", destination_specified)
    _check("destination_root_configured", destination_specified and data.destination_root_configured)
    _check("destination_path_inside_root", destination_specified and data.destination_path_inside_root)
    _check("destination_still_unoccupied", data.destination_still_unoccupied)
    _check("no_competing_current_plan", data.competing_plan_id is None, competing_plan_id=data.competing_plan_id)

    actions = data.plan_actions
    _check("proposed_actions_present", len(actions) > 0)
    expected_orders = list(range(1, len(actions) + 1))
    actual_orders = [action.action_order for action in actions]
    _check("action_order_valid", actual_orders == expected_orders, orders=actual_orders)
    _check(
        "action_types_supported_by_future_executor",
        all(action.action_type in KNOWN_ACTION_TYPES for action in actions),
    )
    _check(
        "every_action_state_is_proposed_not_executed",
        all(action.execution_state == _PROPOSED_NOT_EXECUTED for action in actions),
    )
    _check("overwrite_not_requested", not any(action.overwrite_requested for action in actions))
    _check(
        "no_unknown_or_unsupported_action",
        all(action.action_type in KNOWN_ACTION_TYPES for action in actions),
    )

    failed_codes = {check.code for check in checks if not check.passed}
    if failed_codes & _BLOCKED_CODES:
        status = ReadinessStatus.BLOCKED
    elif failed_codes & _STALE_CODES:
        status = ReadinessStatus.STALE
    elif failed_codes & _INCOMPLETE_CODES:
        status = ReadinessStatus.INCOMPLETE
    else:
        status = ReadinessStatus.READY_FOR_EXECUTOR

    return ReadinessResult(readiness_status=status, checks=tuple(checks))
