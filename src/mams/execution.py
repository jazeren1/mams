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
