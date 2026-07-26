"""Tests for execution.py: the pure Milestone 8 execution domain model.

Pure-function tests only -- no SQL, no filesystem access, matching the
module's own no-dependency contract.
"""

from __future__ import annotations

from dataclasses import replace

from mams.execution import (
    ExecutionStepType,
    FaultPoint,
    PreflightInput,
    TransferStrategy,
    build_execution_step_plan,
    decide_transfer_strategy,
    evaluate_preflight,
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


# --- evaluate_preflight ---------------------------------------------------------


def _ready_preflight_input(**overrides: object) -> PreflightInput:
    base = PreflightInput(
        plan_status="APPROVED",
        readiness_status="READY_FOR_EXECUTOR",
        active_execution_exists=False,
        plan_source_path="/Incoming/Alien (1979).mkv",
        current_media_file_path="/Incoming/Alien (1979).mkv",
        incoming_roots=("/Incoming",),
        source_exists=True,
        source_is_regular_file=True,
        source_size_bytes=1_000_000,
        plan_source_size_bytes=1_000_000,
        source_mtime=1700000000.0,
        plan_source_mtime=1700000000.0,
        source_readable=True,
        destination_root="/NAS/Movies",
        destination_path="/NAS/Movies/Alien (1979)/Alien (1979).mkv",
        destination_path_exists=False,
        destination_directory_occupied_by_a_file=False,
        destination_root_writable=True,
        state_directory="/Users/johnzeren/Media Archive/.mams",
        state_directory_exists_and_writable=True,
        existing_lock_present=False,
        source_device_id=1,
        destination_device_id=1,
        destination_free_bytes=10_000_000,
        required_free_bytes=1_000_000,
        checksum_algorithm_supported=True,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _failed_preflight_codes(result) -> set[str]:  # type: ignore[no-untyped-def]
    return {check.code for check in result.checks if not check.passed}


def test_fully_ready_preflight_passes_every_check() -> None:
    result = evaluate_preflight(_ready_preflight_input())
    assert result.all_passed is True
    assert len(result.checks) == 20
    assert all(check.passed for check in result.checks)


def test_preflight_never_short_circuits() -> None:
    """Every check runs and is reported even when several fail at once."""
    result = evaluate_preflight(
        _ready_preflight_input(plan_status="EXECUTING", source_exists=False, active_execution_exists=True)
    )
    assert len(result.checks) == 20
    assert result.all_passed is False
    failed = _failed_preflight_codes(result)
    assert "plan_status_approved" in failed
    assert "no_execution_already_in_progress_for_this_plan" in failed
    assert "source_exists" in failed


def test_plan_not_approved_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(plan_status="EXECUTING"))
    assert result.all_passed is False
    assert "plan_status_approved" in _failed_preflight_codes(result)


def test_active_execution_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(active_execution_exists=True))
    assert result.all_passed is False
    assert "no_execution_already_in_progress_for_this_plan" in _failed_preflight_codes(result)


def test_stale_readiness_status_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(readiness_status="STALE"))
    assert result.all_passed is False
    assert "readiness_status_ready_for_executor" in _failed_preflight_codes(result)


def test_missing_source_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(source_exists=False))
    assert result.all_passed is False
    assert "source_exists" in _failed_preflight_codes(result)


def test_source_not_a_regular_file_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(source_is_regular_file=False))
    assert result.all_passed is False
    assert "source_is_regular_file" in _failed_preflight_codes(result)


def test_source_outside_incoming_root_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(incoming_roots=("/SomewhereElse",)))
    assert result.all_passed is False
    assert "source_path_under_incoming_root" in _failed_preflight_codes(result)


def test_source_path_drifted_from_canonical_inventory_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(current_media_file_path="/Incoming/Renamed.mkv"))
    assert result.all_passed is False
    assert "source_path_matches_plan_exactly" in _failed_preflight_codes(result)


def test_source_size_drift_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(source_size_bytes=999))
    assert result.all_passed is False
    assert "source_size_matches_plan_snapshot" in _failed_preflight_codes(result)


def test_source_mtime_drift_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(source_mtime=1.0))
    assert result.all_passed is False
    assert "source_mtime_matches_plan_snapshot" in _failed_preflight_codes(result)


def test_unreadable_source_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(source_readable=False))
    assert result.all_passed is False
    assert "source_readable" in _failed_preflight_codes(result)


def test_destination_outside_root_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(destination_root="/NAS/TV"))
    assert result.all_passed is False
    assert "destination_under_destination_root" in _failed_preflight_codes(result)


def test_destination_already_exists_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(destination_path_exists=True))
    assert result.all_passed is False
    assert "destination_path_does_not_exist" in _failed_preflight_codes(result)


def test_destination_directory_occupied_by_a_file_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(destination_directory_occupied_by_a_file=True))
    assert result.all_passed is False
    assert "destination_directory_not_occupied_by_a_file" in _failed_preflight_codes(result)


def test_destination_root_not_writable_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(destination_root_writable=False))
    assert result.all_passed is False
    assert "destination_root_writable" in _failed_preflight_codes(result)


def test_unconfigured_state_directory_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(state_directory=None, state_directory_exists_and_writable=False))
    assert result.all_passed is False
    assert "state_directory_configured_and_writable" in _failed_preflight_codes(result)


def test_existing_lock_file_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(existing_lock_present=True))
    assert result.all_passed is False
    assert "no_existing_lock_file_for_this_plan" in _failed_preflight_codes(result)


def test_unresolvable_source_device_id_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(source_device_id=None))
    assert result.all_passed is False
    assert "source_device_id_resolvable" in _failed_preflight_codes(result)


def test_unresolvable_destination_device_id_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(destination_device_id=None))
    assert result.all_passed is False
    assert "destination_device_id_resolvable" in _failed_preflight_codes(result)


def test_insufficient_free_space_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(destination_free_bytes=100, required_free_bytes=1_000_000))
    assert result.all_passed is False
    assert "sufficient_free_space_on_destination_device" in _failed_preflight_codes(result)


def test_unresolvable_free_space_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(destination_free_bytes=None))
    assert result.all_passed is False
    assert "sufficient_free_space_on_destination_device" in _failed_preflight_codes(result)


def test_unsupported_checksum_algorithm_blocks() -> None:
    result = evaluate_preflight(_ready_preflight_input(checksum_algorithm_supported=False))
    assert result.all_passed is False
    assert "checksum_algorithm_supported" in _failed_preflight_codes(result)
