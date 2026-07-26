"""Tests for execution.py: the pure Milestone 8 execution domain model.

Pure-function tests only -- no SQL, no filesystem access, matching the
module's own no-dependency contract.
"""

from __future__ import annotations

from mams.execution import (
    ExecutionStepType,
    FaultPoint,
    TransferStrategy,
    build_execution_step_plan,
    decide_transfer_strategy,
)


def test_same_device_id_selects_same_filesystem_strategy() -> None:
    assert decide_transfer_strategy(source_device_id=42, destination_device_id=42) == (
        TransferStrategy.SAME_FILESYSTEM_ATOMIC_RENAME
    )


def test_different_device_ids_select_cross_filesystem_strategy() -> None:
    assert decide_transfer_strategy(source_device_id=42, destination_device_id=99) == (
        TransferStrategy.CROSS_FILESYSTEM_COPY_VERIFY_REMOVE
    )


def test_same_filesystem_step_plan_uses_atomic_rename_and_no_source_removal_step() -> None:
    steps = build_execution_step_plan(TransferStrategy.SAME_FILESYSTEM_ATOMIC_RENAME)
    assert steps == [
        ExecutionStepType.VALIDATE_SOURCE,
        ExecutionStepType.CREATE_DESTINATION_DIRECTORY,
        ExecutionStepType.ATOMIC_RENAME,
        ExecutionStepType.VERIFY_DESTINATION_MEDIA,
        ExecutionStepType.REFRESH_INVENTORY,
        ExecutionStepType.PLEX_REFRESH,
        ExecutionStepType.FINALIZE,
    ]
    assert ExecutionStepType.REMOVE_SOURCE not in steps
    assert ExecutionStepType.STREAM_COPY_WITH_CHECKSUM not in steps


def test_cross_filesystem_step_plan_copies_checksums_then_removes_source() -> None:
    steps = build_execution_step_plan(TransferStrategy.CROSS_FILESYSTEM_COPY_VERIFY_REMOVE)
    assert steps == [
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
    # Source removal must never happen before destination verification.
    assert steps.index(ExecutionStepType.VERIFY_DESTINATION_MEDIA) < steps.index(ExecutionStepType.REMOVE_SOURCE)
    # The checksum-bearing commit must complete before source removal.
    assert steps.index(ExecutionStepType.VERIFY_CHECKSUM_MATCH) < steps.index(ExecutionStepType.REMOVE_SOURCE)


def test_both_strategies_end_with_finalize_as_the_last_step() -> None:
    for strategy in TransferStrategy:
        steps = build_execution_step_plan(strategy)
        assert steps[-1] == ExecutionStepType.FINALIZE


def test_fault_point_has_exactly_thirteen_named_boundaries() -> None:
    assert len(list(FaultPoint)) == 13
