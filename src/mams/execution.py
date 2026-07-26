"""Pure execution domain model for Milestone 8's approved-plan executor.

Distinct from `readiness.py` (which judges whether an already-APPROVED
plan is still an accurate description of what should happen) and
`verification.py` (which judges whether a file is structurally
plausible): this module judges how to safely carry out one approved
plan's transfer, and whether it is actually safe to begin doing so right
this moment.

Pure module: no SQL, no filesystem access, no subprocess calls. Every
function here takes plain, already-collected facts (a device id already
obtained via `os.stat`, a MediaInfo probe already run) and returns a
plain result -- `execution_service.py` is the only caller, and it alone
is responsible for gathering fresh evidence and acting on it.

The primary safety principle behind everything here: no plan action may
be trusted merely because it was approved (or even because
`readiness.py`'s audit passed a moment ago). Preflight re-checks facts
that the audit already checked, using fresh `stat()` calls taken after
the execution lock is held, specifically because time -- however little
-- has passed since the audit ran.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TransferStrategy(StrEnum):
    SAME_FILESYSTEM_ATOMIC_RENAME = "SAME_FILESYSTEM_ATOMIC_RENAME"
    CROSS_FILESYSTEM_COPY_VERIFY_REMOVE = "CROSS_FILESYSTEM_COPY_VERIFY_REMOVE"


class ExecutionStatus(StrEnum):
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class RecoveryStatus(StrEnum):
    NONE = "NONE"
    PARTIAL_DESTINATION_SOURCE_INTACT = "PARTIAL_DESTINATION_SOURCE_INTACT"
    DESTINATION_VERIFIED_SOURCE_NOT_REMOVED = "DESTINATION_VERIFIED_SOURCE_NOT_REMOVED"
    DESTINATION_UNVERIFIED_SOURCE_REMOVED = "DESTINATION_UNVERIFIED_SOURCE_REMOVED"
    INVENTORY_REFRESH_INCOMPLETE = "INVENTORY_REFRESH_INCOMPLETE"
    INTERRUPTED_STATE_UNKNOWN = "INTERRUPTED_STATE_UNKNOWN"
    OTHER_REQUIRES_MANUAL_INSPECTION = "OTHER_REQUIRES_MANUAL_INSPECTION"


class ExecutionStepType(StrEnum):
    VALIDATE_SOURCE = "VALIDATE_SOURCE"
    CREATE_DESTINATION_DIRECTORY = "CREATE_DESTINATION_DIRECTORY"
    STREAM_COPY_WITH_CHECKSUM = "STREAM_COPY_WITH_CHECKSUM"
    COMPUTE_DESTINATION_CHECKSUM = "COMPUTE_DESTINATION_CHECKSUM"
    VERIFY_CHECKSUM_MATCH = "VERIFY_CHECKSUM_MATCH"
    ATOMIC_RENAME = "ATOMIC_RENAME"
    FINAL_RENAME = "FINAL_RENAME"
    VERIFY_DESTINATION_MEDIA = "VERIFY_DESTINATION_MEDIA"
    REMOVE_SOURCE = "REMOVE_SOURCE"
    REFRESH_INVENTORY = "REFRESH_INVENTORY"
    PLEX_REFRESH = "PLEX_REFRESH"
    FINALIZE = "FINALIZE"


class FaultPoint(StrEnum):
    """Named hook points for deterministic failure-injection testing
    (never read from an environment variable -- `execution_service.py`
    accepts an injected `FaultInjector` object instead). Exactly 13,
    chosen to cover every filesystem-mutation boundary the milestone
    spec calls out; adjacent lower-risk boundaries where nothing
    filesystem-mutating happens between them are deliberately merged
    into one point."""

    BEFORE_DIRECTORY_CREATE = "BEFORE_DIRECTORY_CREATE"
    AFTER_DIRECTORY_CREATE = "AFTER_DIRECTORY_CREATE"
    BEFORE_COPY_START = "BEFORE_COPY_START"
    DURING_COPY = "DURING_COPY"
    AFTER_COPY_COMPLETE = "AFTER_COPY_COMPLETE"
    BEFORE_CHECKSUM_COMPUTE = "BEFORE_CHECKSUM_COMPUTE"
    AFTER_CHECKSUM_COMPUTE_BEFORE_RENAME = "AFTER_CHECKSUM_COMPUTE_BEFORE_RENAME"
    BEFORE_FINAL_RENAME = "BEFORE_FINAL_RENAME"
    AFTER_FINAL_RENAME = "AFTER_FINAL_RENAME"
    BEFORE_DESTINATION_VERIFY = "BEFORE_DESTINATION_VERIFY"
    AFTER_DESTINATION_VERIFY_BEFORE_SOURCE_REMOVE = "AFTER_DESTINATION_VERIFY_BEFORE_SOURCE_REMOVE"
    AFTER_SOURCE_REMOVE_BEFORE_INVENTORY_REFRESH = "AFTER_SOURCE_REMOVE_BEFORE_INVENTORY_REFRESH"
    AFTER_INVENTORY_REFRESH_BEFORE_FINALIZE = "AFTER_INVENTORY_REFRESH_BEFORE_FINALIZE"


def decide_transfer_strategy(source_device_id: int, destination_device_id: int) -> TransferStrategy:
    """Pure comparison of two already-obtained `os.stat(...).st_dev`
    values -- never a path-prefix heuristic. Callers
    (`execution_filesystem.stat_device_id`) do the actual `stat()` call;
    this function only decides what the comparison means."""
    if source_device_id == destination_device_id:
        return TransferStrategy.SAME_FILESYSTEM_ATOMIC_RENAME
    return TransferStrategy.CROSS_FILESYSTEM_COPY_VERIFY_REMOVE


def build_execution_step_plan(strategy: TransferStrategy) -> list[ExecutionStepType]:
    """The real, ordered step list for one strategy -- the execution
    analogue of `ingest_service._build_actions()`, but needing nothing
    beyond the strategy itself, so it stays pure."""
    if strategy is TransferStrategy.SAME_FILESYSTEM_ATOMIC_RENAME:
        return [
            ExecutionStepType.VALIDATE_SOURCE,
            ExecutionStepType.CREATE_DESTINATION_DIRECTORY,
            ExecutionStepType.ATOMIC_RENAME,
            ExecutionStepType.VERIFY_DESTINATION_MEDIA,
            ExecutionStepType.REFRESH_INVENTORY,
            ExecutionStepType.PLEX_REFRESH,
            ExecutionStepType.FINALIZE,
        ]
    return [
        ExecutionStepType.VALIDATE_SOURCE,
        ExecutionStepType.CREATE_DESTINATION_DIRECTORY,
        ExecutionStepType.STREAM_COPY_WITH_CHECKSUM,
        ExecutionStepType.COMPUTE_DESTINATION_CHECKSUM,
        ExecutionStepType.VERIFY_CHECKSUM_MATCH,
        ExecutionStepType.FINAL_RENAME,
        ExecutionStepType.VERIFY_DESTINATION_MEDIA,
        ExecutionStepType.REMOVE_SOURCE,
        ExecutionStepType.REFRESH_INVENTORY,
        ExecutionStepType.PLEX_REFRESH,
        ExecutionStepType.FINALIZE,
    ]


def _is_under_root(path: str, root: str) -> bool:
    normalized_root = root.rstrip("/")
    return path == normalized_root or path.startswith(normalized_root + "/")


@dataclass(frozen=True)
class PreflightInput:
    """Everything `evaluate_preflight()` needs, already gathered by
    `execution_service.execute_plan()` from a fresh re-run of
    `ingest_service.audit_plan()`, fresh `stat()`/`os.access()` calls, and
    the just-acquired execution lock/DB state -- gathered *after* the
    lock is held, specifically because time has passed since any earlier
    check.

    Deliberately does not re-check identity-assignment drift, blocking
    findings, or action-list shape: `readiness_status` already reflects
    a readiness audit re-run moments ago, and duplicating those DB-level
    checks here would only re-assert the same fact twice. What this
    input adds beyond that audit is live filesystem evidence the audit
    itself never gathers (regular-file-ness, read/writability, device
    ids, free space, lock state)."""

    plan_status: str
    readiness_status: str
    active_execution_exists: bool

    plan_source_path: str
    current_media_file_path: str | None
    incoming_roots: tuple[str, ...]
    source_exists: bool
    source_is_regular_file: bool
    source_size_bytes: int | None
    plan_source_size_bytes: int | None
    source_mtime: float | None
    plan_source_mtime: float | None
    source_readable: bool

    destination_root: str
    destination_path: str
    destination_path_exists: bool
    destination_directory_occupied_by_a_file: bool
    destination_root_writable: bool

    state_directory: str | None
    state_directory_exists_and_writable: bool
    existing_lock_present: bool

    source_device_id: int | None
    destination_device_id: int | None
    destination_free_bytes: int | None
    required_free_bytes: int

    checksum_algorithm_supported: bool


@dataclass(frozen=True)
class PreflightCheck:
    code: str
    passed: bool
    evidence: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "status": "PASS" if self.passed else "FAIL", "evidence": self.evidence}


@dataclass(frozen=True)
class PreflightResult:
    all_passed: bool
    checks: tuple[PreflightCheck, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "all_passed": self.all_passed,
            "checks": [check.to_dict() for check in self.checks],
        }


def evaluate_preflight(data: PreflightInput) -> PreflightResult:
    """Run all 20 checks and derive whether it is safe to begin any
    filesystem mutation. Deterministic and never short-circuits: every
    check always runs and is always reported, so a failure gives a full
    picture, not just the first problem found."""
    checks: list[PreflightCheck] = []

    def _check(code: str, passed: bool, **evidence: object) -> None:
        checks.append(PreflightCheck(code=code, passed=passed, evidence=evidence))

    _check("plan_status_approved", data.plan_status == "APPROVED", plan_status=data.plan_status)
    _check(
        "no_execution_already_in_progress_for_this_plan",
        not data.active_execution_exists,
        active_execution_exists=data.active_execution_exists,
    )
    _check(
        "readiness_status_ready_for_executor",
        data.readiness_status == "READY_FOR_EXECUTOR",
        readiness_status=data.readiness_status,
    )

    _check("source_exists", data.source_exists)
    _check(
        "source_is_regular_file",
        data.source_exists and data.source_is_regular_file,
        is_regular_file=data.source_is_regular_file,
    )
    under_incoming_root = any(_is_under_root(data.plan_source_path, root) for root in data.incoming_roots)
    _check(
        "source_path_under_incoming_root",
        under_incoming_root,
        source_path=data.plan_source_path,
        incoming_roots=list(data.incoming_roots),
    )
    _check(
        "source_path_matches_plan_exactly",
        data.current_media_file_path == data.plan_source_path,
        plan_source_path=data.plan_source_path,
        current_media_file_path=data.current_media_file_path,
    )
    _check(
        "source_size_matches_plan_snapshot",
        data.source_exists and data.source_size_bytes == data.plan_source_size_bytes,
        plan_size_bytes=data.plan_source_size_bytes,
        current_size_bytes=data.source_size_bytes,
    )
    _check(
        "source_mtime_matches_plan_snapshot",
        data.source_exists and data.source_mtime == data.plan_source_mtime,
        plan_mtime=data.plan_source_mtime,
        current_mtime=data.source_mtime,
    )
    _check("source_readable", data.source_exists and data.source_readable)

    _check(
        "destination_under_destination_root",
        _is_under_root(data.destination_path, data.destination_root),
        destination_path=data.destination_path,
        destination_root=data.destination_root,
    )
    _check("destination_path_does_not_exist", not data.destination_path_exists)
    _check(
        "destination_directory_not_occupied_by_a_file",
        not data.destination_directory_occupied_by_a_file,
    )
    _check("destination_root_writable", data.destination_root_writable)

    _check(
        "state_directory_configured_and_writable",
        data.state_directory is not None and data.state_directory_exists_and_writable,
        state_directory=data.state_directory,
    )
    _check("no_existing_lock_file_for_this_plan", not data.existing_lock_present)

    _check("source_device_id_resolvable", data.source_device_id is not None)
    _check("destination_device_id_resolvable", data.destination_device_id is not None)
    _check(
        "sufficient_free_space_on_destination_device",
        data.destination_free_bytes is not None and data.destination_free_bytes >= data.required_free_bytes,
        destination_free_bytes=data.destination_free_bytes,
        required_free_bytes=data.required_free_bytes,
    )
    _check("checksum_algorithm_supported", data.checksum_algorithm_supported)

    return PreflightResult(all_passed=all(check.passed for check in checks), checks=tuple(checks))
