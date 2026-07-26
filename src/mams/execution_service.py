"""Orchestrates `mams ingest execute`: Milestone 8's safe,
one-plan-at-a-time approved-plan executor.

Primary safety principle: no plan action may be trusted merely because
it was approved, or even because `readiness.py`'s audit passed a
moment ago. This module re-runs that audit fresh, then re-gathers
fresh filesystem evidence (`execution.evaluate_preflight`) *after* the
execution lock is held, immediately before any mutation -- every
consequential precondition is revalidated right up against the moment
it matters, never assumed from an earlier check.

This is the only module that combines `execution.py` (pure
strategy/preflight/verification logic), `execution_repository.py` (all
execution-history SQL), `execution_filesystem.py` (the filesystem
adapter), `execution_lock.py` (the process-level lock),
`ingest_service.audit_plan` (the Milestone 7C readiness contract), and
`inventory_repository.relocate_media_file` (the targeted, single-file
canonical-inventory refresh). `cli.py` calls this module only -- it
contains no execution logic of its own.

A mid-execution failure is never re-raised to the caller: it is a
fully recorded, legitimate outcome (mirroring how `generate_plan`
returns a BLOCKED `PlanRecord` rather than raising). Only a *usage-level*
rejection -- unknown plan, wrong status, failed audit, lock already
held, failed preflight, or a lost APPROVED -> EXECUTING race -- raises
an `ExecutionUsageError`, always before any execution row exists and
before any filesystem mutation begins.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

from . import execution_repository, ingest_repository, inventory_repository
from .config import AppConfig
from .execution import (
    DestinationVerificationInput,
    DestinationVerificationResult,
    FaultPoint,
    PreflightInput,
    PreflightResult,
    TransferStrategy,
    build_execution_step_plan,
    decide_transfer_strategy,
    evaluate_preflight,
    verify_destination,
)
from .execution_lock import LockAlreadyHeldError, LockInfo, acquire_lock, read_lock, release_lock
from . import execution_filesystem as fs
from .ingest_repository import PlanRecord
from .ingest_service import audit_plan
from .inventory import Layout, detect_layout
from .mediainfo import MediaInfoOutcome, MediaInfoProvider, MetadataProvider
from .readiness import ReadinessStatus

_EXECUTOR_VERSION = "1"


class ExecutionUsageError(Exception):
    """Raised for a usage-level rejection -- before any execution row is
    created and before any filesystem mutation begins."""


class PlanNotFoundError(ExecutionUsageError):
    pass


class PlanNotApprovedError(ExecutionUsageError):
    def __init__(self, plan_id: int, status: str) -> None:
        self.plan_id = plan_id
        self.status = status
        super().__init__(f"Plan {plan_id} is {status}, not APPROVED -- cannot execute")


class ActiveExecutionExistsError(ExecutionUsageError):
    def __init__(self, plan_id: int, execution_id: int) -> None:
        self.plan_id = plan_id
        self.execution_id = execution_id
        super().__init__(f"Plan {plan_id} already has an active execution (#{execution_id})")


class PlanNotExecutableError(ExecutionUsageError):
    def __init__(self, readiness_status: str) -> None:
        self.readiness_status = readiness_status
        super().__init__(f"Plan's readiness audit is {readiness_status}, not READY_FOR_EXECUTOR")


class StateDirectoryNotConfiguredError(ExecutionUsageError):
    def __init__(self) -> None:
        super().__init__("execution.state_directory is not configured")


class ExecutionLockHeldError(ExecutionUsageError):
    def __init__(self, existing: LockInfo | None) -> None:
        self.existing = existing
        super().__init__(f"An execution lock is already held for this plan: {existing}")


class PreflightFailedError(ExecutionUsageError):
    def __init__(self, result: PreflightResult) -> None:
        self.result = result
        failed = [check.code for check in result.checks if not check.passed]
        super().__init__(f"Preflight failed: {', '.join(failed)}")


class ExecutionRaceError(ExecutionUsageError):
    def __init__(self, plan_id: int) -> None:
        self.plan_id = plan_id
        super().__init__(f"Plan {plan_id} was no longer APPROVED at the moment of execution -- lost a race")


class DestinationVerificationFailedError(Exception):
    """Not a usage error -- raised only after mutation has begun, so it
    is always caught and converted into a recorded RECOVERY_REQUIRED
    outcome, never propagated to the caller."""

    def __init__(self, result: DestinationVerificationResult) -> None:
        failed = [check.code for check in result.checks if check.status.value == "FAIL"]
        super().__init__(f"Destination verification did not pass: {', '.join(failed)}")


class FaultInjector(Protocol):
    """Test-only seam for deterministic failure injection (Phase T) --
    never read from an environment variable. Production code always
    passes `NullFaultInjector`."""

    def maybe_fail(self, point: FaultPoint) -> None: ...


class NullFaultInjector:
    def maybe_fail(self, point: FaultPoint) -> None:
        return None


def _current_timestamp() -> str:
    """Matches SQLite's own `CURRENT_TIMESTAMP` format (UTC,
    `YYYY-MM-DD HH:MM:SS`) for consistency with every other
    database-generated timestamp in this schema."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _category_key_for_root(config: AppConfig, root: str) -> str | None:
    for key, value in config.nas_categories.items():
        if value == root:
            return key
    return None


def _gather_preflight_input(
    connection: sqlite3.Connection,
    config: AppConfig,
    plan: PlanRecord,
    *,
    token: str,
    state_directory: Path,
    readiness_status: str,
    active_execution_exists: bool,
) -> PreflightInput:
    """Gather fresh, live evidence -- this function performs the actual
    `stat()`/`os.access()` calls (via `execution_filesystem.py`) that
    make preflight a genuine revalidation, not a repeat of what the
    audit already checked from the database alone."""
    media_file = inventory_repository.get_media_file(connection, plan.media_file_id)
    current_media_file_path = media_file.absolute_path if media_file is not None else None

    source_exists = fs.path_exists(plan.source_path)
    source_is_regular_file = fs.is_regular_file(plan.source_path) if source_exists else False
    source_size_bytes = fs.stat_size_bytes(plan.source_path) if source_exists else None
    source_mtime = fs.stat_mtime(plan.source_path) if source_exists else None
    source_readable = fs.is_readable(plan.source_path) if source_exists else False
    source_device_id = fs.stat_device_id(plan.source_path) if source_exists else None

    assert plan.destination_library is not None
    assert plan.destination_directory is not None
    assert plan.destination_filename is not None
    destination_path = str(PurePosixPath(plan.destination_directory) / plan.destination_filename)
    destination_path_exists = fs.path_exists(destination_path)
    destination_directory_occupied_by_a_file = fs.path_exists(
        plan.destination_directory
    ) and not fs.is_directory(plan.destination_directory)
    destination_root_exists = fs.path_exists(plan.destination_library)
    destination_root_writable = destination_root_exists and fs.is_writable(plan.destination_library)
    destination_device_id = fs.stat_device_id(plan.destination_library) if destination_root_exists else None
    destination_free_bytes = fs.free_space_bytes(plan.destination_library) if destination_root_exists else None

    if source_device_id is not None and destination_device_id is not None:
        strategy_guess = decide_transfer_strategy(source_device_id, destination_device_id)
        required_free_bytes = (
            0 if strategy_guess is TransferStrategy.SAME_FILESYSTEM_ATOMIC_RENAME else (plan.source_size_bytes or 0)
        )
    else:
        required_free_bytes = plan.source_size_bytes or 0

    held_lock = read_lock(state_directory, plan.id)
    existing_lock_present = held_lock is not None and held_lock.token != token

    return PreflightInput(
        plan_status=plan.status,
        readiness_status=readiness_status,
        active_execution_exists=active_execution_exists,
        plan_source_path=plan.source_path,
        current_media_file_path=current_media_file_path,
        incoming_roots=tuple(config.ingest_incoming_roots),
        source_exists=source_exists,
        source_is_regular_file=source_is_regular_file,
        source_size_bytes=source_size_bytes,
        plan_source_size_bytes=plan.source_size_bytes,
        source_mtime=source_mtime,
        plan_source_mtime=plan.source_mtime,
        source_readable=source_readable,
        destination_root=plan.destination_library,
        destination_path=destination_path,
        destination_path_exists=destination_path_exists,
        destination_directory_occupied_by_a_file=destination_directory_occupied_by_a_file,
        destination_root_writable=destination_root_writable,
        state_directory=str(state_directory),
        state_directory_exists_and_writable=fs.is_directory(str(state_directory))
        and fs.is_writable(str(state_directory)),
        existing_lock_present=existing_lock_present,
        source_device_id=source_device_id,
        destination_device_id=destination_device_id,
        destination_free_bytes=destination_free_bytes,
        required_free_bytes=required_free_bytes,
        checksum_algorithm_supported=_checksum_algorithm_supported(config.checksum_algorithm),
    )


def _checksum_algorithm_supported(algorithm: str) -> bool:
    import hashlib

    return algorithm in hashlib.algorithms_available


@dataclass(frozen=True)
class ExecutionPreview:
    """Read-only preview for the CLI's pre-confirmation prompt. Never
    acquires the lock and never mutates anything -- the `transfer_strategy`
    shown here is a best-effort guess from currently observable device
    IDs; `execute_plan()` always redecides it fresh under the lock, and
    that later decision is the only one that matters."""

    plan_id: int
    plan_status: str
    source_path: str
    destination_path: str | None
    transfer_strategy: str | None
    readiness_status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "plan_status": self.plan_status,
            "source_path": self.source_path,
            "destination_path": self.destination_path,
            "transfer_strategy": self.transfer_strategy,
            "readiness_status": self.readiness_status,
        }


def preview_execution(connection: sqlite3.Connection, config: AppConfig, *, plan_id: int) -> ExecutionPreview:
    plan = ingest_repository.get_plan(connection, plan_id)
    if plan is None:
        raise PlanNotFoundError(f"No ingest plan with id {plan_id}")

    readiness_result = audit_plan(connection, config, plan_id=plan_id)

    destination_path: str | None = None
    strategy: str | None = None
    if plan.destination_directory is not None and plan.destination_filename is not None:
        destination_path = str(PurePosixPath(plan.destination_directory) / plan.destination_filename)
        if (
            plan.destination_library is not None
            and fs.path_exists(plan.source_path)
            and fs.path_exists(plan.destination_library)
        ):
            source_device_id = fs.stat_device_id(plan.source_path)
            destination_device_id = fs.stat_device_id(plan.destination_library)
            strategy = decide_transfer_strategy(source_device_id, destination_device_id).value

    return ExecutionPreview(
        plan_id=plan_id,
        plan_status=plan.status,
        source_path=plan.source_path,
        destination_path=destination_path,
        transfer_strategy=strategy,
        readiness_status=readiness_result.readiness_status.value,
    )


def execute_plan(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    plan_id: int,
    metadata_provider: MetadataProvider | None = None,
    fault_injector: FaultInjector | None = None,
) -> execution_repository.ExecutionRecord:
    """Execute exactly one `APPROVED` plan. Raises `ExecutionUsageError`
    subclasses for every rejection that happens before any filesystem
    mutation begins; returns a (possibly RECOVERY_REQUIRED or FAILED)
    `ExecutionRecord` for anything that goes wrong after that point --
    a mid-execution failure is a recorded outcome, not an exception."""
    provider = metadata_provider or MediaInfoProvider()
    injector = fault_injector or NullFaultInjector()

    plan = ingest_repository.get_plan(connection, plan_id)
    if plan is None:
        raise PlanNotFoundError(f"No ingest plan with id {plan_id}")
    if plan.status != "APPROVED":
        raise PlanNotApprovedError(plan_id, plan.status)

    existing_execution = execution_repository.get_current_execution_for_plan(connection, plan_id)
    if existing_execution is not None and existing_execution.status == "EXECUTING":
        raise ActiveExecutionExistsError(plan_id, existing_execution.id)

    readiness_result = audit_plan(connection, config, plan_id=plan_id)
    if readiness_result.readiness_status != ReadinessStatus.READY_FOR_EXECUTOR:
        raise PlanNotExecutableError(readiness_result.readiness_status.value)

    state_directory = config.execution_state_directory
    if state_directory is None:
        raise StateDirectoryNotConfiguredError()

    token = secrets.token_hex(16)
    try:
        acquire_lock(state_directory, plan_id, token=token)
    except LockAlreadyHeldError as exc:
        raise ExecutionLockHeldError(exc.existing) from exc

    try:
        return _execute_locked(connection, config, plan=plan, token=token, state_directory=state_directory, provider=provider, injector=injector)
    finally:
        release_lock(state_directory, plan_id, token=token)


def _execute_locked(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    plan: PlanRecord,
    token: str,
    state_directory: Path,
    provider: MetadataProvider,
    injector: FaultInjector,
) -> execution_repository.ExecutionRecord:
    plan_id = plan.id

    # Immediate re-validation, now that the lock is held: nothing above
    # this line may be trusted merely because it was true a moment ago.
    fresh_readiness = audit_plan(connection, config, plan_id=plan_id)
    fresh_active_execution = execution_repository.get_current_execution_for_plan(connection, plan_id)
    active_execution_exists = fresh_active_execution is not None and fresh_active_execution.status == "EXECUTING"

    preflight_input = _gather_preflight_input(
        connection,
        config,
        plan,
        token=token,
        state_directory=state_directory,
        readiness_status=fresh_readiness.readiness_status.value,
        active_execution_exists=active_execution_exists,
    )
    preflight_result = evaluate_preflight(preflight_input)
    if not preflight_result.all_passed:
        raise PreflightFailedError(preflight_result)

    assert preflight_input.source_device_id is not None
    assert preflight_input.destination_device_id is not None
    strategy = decide_transfer_strategy(preflight_input.source_device_id, preflight_input.destination_device_id)
    step_types = build_execution_step_plan(strategy)

    with connection:
        if not execution_repository.transition_plan_to_executing(connection, plan_id):
            raise ExecutionRaceError(plan_id)
        execution = execution_repository.start_execution(
            connection,
            ingest_plan_id=plan_id,
            plan_version=plan.plan_version,
            transfer_strategy=strategy.value,
            source_path=plan.source_path,
            destination_path=preflight_input.destination_path,
            source_device_id=preflight_input.source_device_id,
            destination_device_id=preflight_input.destination_device_id,
            checksum_algorithm=config.checksum_algorithm,
            source_size_bytes=preflight_input.source_size_bytes or 0,
            lock_token=token,
            step_types=[step.value for step in step_types],
        )

    runner = _ExecutionRun(
        connection=connection,
        config=config,
        plan=plan,
        execution=execution,
        preflight_result=preflight_result,
        provider=provider,
        injector=injector,
    )
    if strategy is TransferStrategy.SAME_FILESYSTEM_ATOMIC_RENAME:
        return runner.run_same_filesystem()
    return runner.run_cross_filesystem()


@dataclass
class _ExecutionRun:
    connection: sqlite3.Connection
    config: AppConfig
    plan: PlanRecord
    execution: execution_repository.ExecutionRecord
    preflight_result: PreflightResult
    provider: MetadataProvider
    injector: FaultInjector

    def _fail(
        self, *, step_id: int | None, step_type: str, error: BaseException, recovery_status: str
    ) -> execution_repository.ExecutionRecord:
        """Shared terminal-failure path: mark the in-flight step (if one
        was begun) FAILED, mark the execution and plan
        RECOVERY_REQUIRED (or FAILED/EXECUTION_FAILED when
        `recovery_status` is `NONE`), and return the resulting record --
        never re-raised."""
        message = str(error)
        connection = self.connection
        execution_id = self.execution.id
        plan_id = self.plan.id
        with connection:
            if step_id is not None:
                execution_repository.complete_step(
                    connection, step_id=step_id, status="FAILED", detail={"error": message}
                )
            if recovery_status == "NONE":
                execution_repository.mark_execution_failed(
                    connection, execution_id, failure_step=step_type, failure_message=message
                )
                execution_repository.transition_plan_after_execution(
                    connection, plan_id, new_status="EXECUTION_FAILED", executed_at=None
                )
            else:
                execution_repository.mark_execution_recovery_required(
                    connection,
                    execution_id,
                    failure_step=step_type,
                    failure_message=message,
                    recovery_status=recovery_status,
                )
                execution_repository.transition_plan_after_execution(
                    connection, plan_id, new_status="RECOVERY_REQUIRED", executed_at=None
                )
        result = execution_repository.get_execution(connection, execution_id)
        assert result is not None
        return result

    def _begin(self, step_order: int) -> execution_repository.ExecutionStepRecord:
        with self.connection:
            return execution_repository.begin_step(self.connection, execution_id=self.execution.id, step_order=step_order)

    def _complete(self, step_id: int, *, status: str = "SUCCEEDED", detail: dict[str, object] | None = None) -> None:
        with self.connection:
            execution_repository.complete_step(self.connection, step_id=step_id, status=status, detail=detail)

    def _verify_destination(
        self, *, destination_path: str, expected_size_bytes: int, checksum_required: bool, source_checksum: str | None, destination_checksum: str | None
    ) -> tuple[DestinationVerificationResult, MediaInfoOutcome]:
        outcome = self.provider.probe(Path(destination_path))
        exists = fs.path_exists(destination_path)
        is_file = fs.is_regular_file(destination_path) if exists else False
        size = fs.stat_size_bytes(destination_path) if exists else None
        media_info = outcome.media_info
        data = DestinationVerificationInput(
            destination_path=destination_path,
            plan_destination_path=self.execution.destination_path,
            destination_exists=exists,
            destination_is_regular_file=is_file,
            destination_size_bytes=size,
            expected_size_bytes=expected_size_bytes,
            media_info_probed=True,
            media_info_error=outcome.error,
            container=media_info.container if media_info is not None else None,
            extension=Path(destination_path).suffix,
            duration_seconds=media_info.duration_seconds if media_info is not None else None,
            video_track_count=len(media_info.video_tracks) if media_info is not None else 0,
            audio_track_count=len(media_info.audio_tracks) if media_info is not None else 0,
            checksum_required=checksum_required,
            source_checksum=source_checksum,
            destination_checksum=destination_checksum,
        )
        return verify_destination(data), outcome

    def _refresh_inventory(self, *, destination_path: str, outcome: MediaInfoOutcome) -> int:
        """Targeted, single-file canonical inventory refresh -- never a
        category walk. See `inventory_repository.relocate_media_file`."""
        connection = self.connection
        config = self.config
        plan = self.plan
        assert plan.destination_library is not None

        category_key = _category_key_for_root(config, plan.destination_library)
        assert category_key is not None, "destination root must map to a configured NAS category"
        library_ids = inventory_repository.sync_libraries(connection, {category_key: plan.destination_library})
        library_id = library_ids[category_key]

        destination_path_obj = Path(destination_path)
        relative_path = destination_path_obj.relative_to(Path(plan.destination_library))
        layout: Layout = detect_layout(category_key, relative_path)

        scan_run_id = inventory_repository.start_scan_run(
            connection, metadata_enabled=True, mediainfo_version=None, triggered_by="EXECUTION"
        )
        size_bytes = fs.stat_size_bytes(destination_path)
        with connection:
            inventory_repository.relocate_media_file(
                connection,
                media_file_id=plan.media_file_id,
                new_library_id=library_id,
                new_absolute_path=destination_path,
                new_relative_path=str(relative_path),
                new_filename=destination_path_obj.name,
                new_extension=destination_path_obj.suffix,
                new_parent_directory=str(destination_path_obj.parent),
                new_layout=layout.value,
                new_size_bytes=size_bytes,
                new_mtime=fs.stat_mtime(destination_path),
                scan_run_id=scan_run_id,
                media_info=outcome.media_info,
                media_info_error=outcome.error,
            )
            inventory_repository.complete_scan_run(connection, scan_run_id, file_count=1, total_size_bytes=size_bytes)
        return scan_run_id

    def _plex_step(self, step_order: int) -> None:
        step = self._begin(step_order)
        reason = "disabled_by_config" if not self.config.enable_plex_refresh else "plex_client_not_implemented"
        with self.connection:
            execution_repository.record_plex_refresh_status(self.connection, self.execution.id, "SKIPPED")
            execution_repository.complete_step(
                self.connection, step_id=step.id, status="SKIPPED", detail={"reason": reason}
            )

    def _finalize_step(self, step_order: int, *, destination_size_bytes: int, destination_checksum: str | None) -> None:
        step = self._begin(step_order)
        with self.connection:
            execution_repository.mark_execution_succeeded(
                self.connection,
                self.execution.id,
                destination_checksum=destination_checksum,
                destination_size_bytes=destination_size_bytes,
            )
            execution_repository.transition_plan_after_execution(
                self.connection, self.plan.id, new_status="EXECUTED", executed_at=_current_timestamp()
            )
            execution_repository.complete_step(self.connection, step_id=step.id, status="SUCCEEDED")

    def run_same_filesystem(self) -> execution_repository.ExecutionRecord:
        plan = self.plan
        execution = self.execution
        destination_path = execution.destination_path
        assert plan.destination_directory is not None

        # 1. VALIDATE_SOURCE -- records the preflight evidence that
        # already ran and passed; performs no new filesystem access.
        step = self._begin(1)
        self._complete(step.id, detail={"preflight": self.preflight_result.to_dict()})

        # 2. CREATE_DESTINATION_DIRECTORY
        step = self._begin(2)
        try:
            self.injector.maybe_fail(FaultPoint.BEFORE_DIRECTORY_CREATE)
            created = fs.create_destination_directory(plan.destination_directory)
            self.injector.maybe_fail(FaultPoint.AFTER_DIRECTORY_CREATE)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad: any failure here is recorded, not propagated
            return self._fail(step_id=step.id, step_type="CREATE_DESTINATION_DIRECTORY", error=exc, recovery_status="NONE")
        self._complete(step.id, detail={"created": created})

        # 3. ATOMIC_RENAME
        step = self._begin(3)
        try:
            self.injector.maybe_fail(FaultPoint.BEFORE_FINAL_RENAME)
            fs.finalize_same_filesystem_source_move(plan.source_path, destination_path)
            self.injector.maybe_fail(FaultPoint.AFTER_FINAL_RENAME)
        except Exception as exc:  # noqa: BLE001
            return self._fail(step_id=step.id, step_type="ATOMIC_RENAME", error=exc, recovery_status="PARTIAL_DESTINATION_SOURCE_INTACT")
        self._complete(step.id)

        # 4. VERIFY_DESTINATION_MEDIA
        step = self._begin(4)
        try:
            self.injector.maybe_fail(FaultPoint.BEFORE_DESTINATION_VERIFY)
            verification, outcome = self._verify_destination(
                destination_path=destination_path,
                expected_size_bytes=execution.source_size_bytes,
                checksum_required=False,
                source_checksum=None,
                destination_checksum=None,
            )
            if verification.status.value == "FAIL":
                raise DestinationVerificationFailedError(verification)
        except Exception as exc:  # noqa: BLE001
            return self._fail(step_id=step.id, step_type="VERIFY_DESTINATION_MEDIA", error=exc, recovery_status="DESTINATION_UNVERIFIED_SOURCE_REMOVED")
        self._complete(step.id, detail=verification.to_dict())

        try:
            self.injector.maybe_fail(FaultPoint.AFTER_DESTINATION_VERIFY_BEFORE_SOURCE_REMOVE)
            self.injector.maybe_fail(FaultPoint.AFTER_SOURCE_REMOVE_BEFORE_INVENTORY_REFRESH)
        except Exception as exc:  # noqa: BLE001
            return self._fail(step_id=None, step_type="REFRESH_INVENTORY", error=exc, recovery_status="INVENTORY_REFRESH_INCOMPLETE")

        # 5. REFRESH_INVENTORY
        step = self._begin(5)
        try:
            scan_run_id = self._refresh_inventory(destination_path=destination_path, outcome=outcome)
        except Exception as exc:  # noqa: BLE001
            return self._fail(step_id=step.id, step_type="REFRESH_INVENTORY", error=exc, recovery_status="INVENTORY_REFRESH_INCOMPLETE")
        with self.connection:
            execution_repository.record_inventory_refresh_completed(self.connection, execution.id)
        self._complete(step.id, detail={"scan_run_id": scan_run_id})

        try:
            self.injector.maybe_fail(FaultPoint.AFTER_INVENTORY_REFRESH_BEFORE_FINALIZE)
        except Exception as exc:  # noqa: BLE001
            return self._fail(step_id=None, step_type="FINALIZE", error=exc, recovery_status="INVENTORY_REFRESH_INCOMPLETE")

        # 6. PLEX_REFRESH
        self._plex_step(6)

        # 7. FINALIZE
        self._finalize_step(7, destination_size_bytes=fs.stat_size_bytes(destination_path), destination_checksum=None)

        result = execution_repository.get_execution(self.connection, execution.id)
        assert result is not None
        return result

    def run_cross_filesystem(self) -> execution_repository.ExecutionRecord:
        plan = self.plan
        execution = self.execution
        destination_path = execution.destination_path
        assert plan.destination_directory is not None
        assert plan.destination_filename is not None
        config = self.config

        # 1. VALIDATE_SOURCE
        step = self._begin(1)
        self._complete(step.id, detail={"preflight": self.preflight_result.to_dict()})

        # 2. CREATE_DESTINATION_DIRECTORY
        step = self._begin(2)
        try:
            self.injector.maybe_fail(FaultPoint.BEFORE_DIRECTORY_CREATE)
            created = fs.create_destination_directory(plan.destination_directory)
            self.injector.maybe_fail(FaultPoint.AFTER_DIRECTORY_CREATE)
        except Exception as exc:  # noqa: BLE001
            return self._fail(step_id=step.id, step_type="CREATE_DESTINATION_DIRECTORY", error=exc, recovery_status="NONE")
        self._complete(step.id, detail={"created": created})

        temp_path = fs.temp_destination_path(plan.destination_directory, plan.destination_filename, token=execution.lock_token)

        # 3. STREAM_COPY_WITH_CHECKSUM
        step = self._begin(3)
        halfway_fired = {"done": False}

        def _on_progress(copied: int, total: int) -> None:
            if not halfway_fired["done"] and total > 0 and copied >= total // 2:
                halfway_fired["done"] = True
                self.injector.maybe_fail(FaultPoint.DURING_COPY)

        try:
            self.injector.maybe_fail(FaultPoint.BEFORE_COPY_START)
            source_checksum, bytes_copied = fs.stream_copy_with_checksum(
                plan.source_path,
                str(temp_path),
                buffer_bytes=config.copy_buffer_bytes,
                algorithm=config.checksum_algorithm,
                on_progress=_on_progress,
            )
            self.injector.maybe_fail(FaultPoint.AFTER_COPY_COMPLETE)
        except Exception as exc:  # noqa: BLE001
            return self._fail(step_id=step.id, step_type="STREAM_COPY_WITH_CHECKSUM", error=exc, recovery_status="PARTIAL_DESTINATION_SOURCE_INTACT")
        with self.connection:
            execution_repository.record_source_checksum(self.connection, execution.id, source_checksum)
        self._complete(step.id, detail={"bytes_copied": bytes_copied})

        # 4. COMPUTE_DESTINATION_CHECKSUM
        step = self._begin(4)
        try:
            self.injector.maybe_fail(FaultPoint.BEFORE_CHECKSUM_COMPUTE)
            destination_checksum = fs.checksum_file(
                str(temp_path), algorithm=config.checksum_algorithm, buffer_bytes=config.copy_buffer_bytes
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(step_id=step.id, step_type="COMPUTE_DESTINATION_CHECKSUM", error=exc, recovery_status="PARTIAL_DESTINATION_SOURCE_INTACT")
        self._complete(step.id, detail={"destination_checksum": destination_checksum})

        # 5. VERIFY_CHECKSUM_MATCH
        step = self._begin(5)
        try:
            if source_checksum != destination_checksum:
                raise ValueError(
                    f"checksum mismatch: source={source_checksum} destination={destination_checksum}"
                )
            self.injector.maybe_fail(FaultPoint.AFTER_CHECKSUM_COMPUTE_BEFORE_RENAME)
        except Exception as exc:  # noqa: BLE001
            return self._fail(step_id=step.id, step_type="VERIFY_CHECKSUM_MATCH", error=exc, recovery_status="PARTIAL_DESTINATION_SOURCE_INTACT")
        self._complete(step.id)

        # 6. FINAL_RENAME (temp -> final; same-device by construction)
        step = self._begin(6)
        try:
            self.injector.maybe_fail(FaultPoint.BEFORE_FINAL_RENAME)
            fs.finalize_verified_temp_file(str(temp_path), destination_path)
            self.injector.maybe_fail(FaultPoint.AFTER_FINAL_RENAME)
        except Exception as exc:  # noqa: BLE001
            return self._fail(step_id=step.id, step_type="FINAL_RENAME", error=exc, recovery_status="PARTIAL_DESTINATION_SOURCE_INTACT")
        self._complete(step.id)

        # 7. VERIFY_DESTINATION_MEDIA
        step = self._begin(7)
        try:
            self.injector.maybe_fail(FaultPoint.BEFORE_DESTINATION_VERIFY)
            verification, outcome = self._verify_destination(
                destination_path=destination_path,
                expected_size_bytes=execution.source_size_bytes,
                checksum_required=True,
                source_checksum=source_checksum,
                destination_checksum=destination_checksum,
            )
            if verification.status.value == "FAIL":
                raise DestinationVerificationFailedError(verification)
        except Exception as exc:  # noqa: BLE001
            return self._fail(step_id=step.id, step_type="VERIFY_DESTINATION_MEDIA", error=exc, recovery_status="DESTINATION_VERIFIED_SOURCE_NOT_REMOVED")
        self._complete(step.id, detail=verification.to_dict())

        try:
            self.injector.maybe_fail(FaultPoint.AFTER_DESTINATION_VERIFY_BEFORE_SOURCE_REMOVE)
        except Exception as exc:  # noqa: BLE001
            return self._fail(step_id=None, step_type="REMOVE_SOURCE", error=exc, recovery_status="DESTINATION_VERIFIED_SOURCE_NOT_REMOVED")

        # 8. REMOVE_SOURCE
        step = self._begin(8)
        try:
            fs.remove_source_file(plan.source_path)
        except Exception as exc:  # noqa: BLE001
            return self._fail(step_id=step.id, step_type="REMOVE_SOURCE", error=exc, recovery_status="DESTINATION_VERIFIED_SOURCE_NOT_REMOVED")
        with self.connection:
            execution_repository.record_source_removed(self.connection, execution.id)
        self._complete(step.id)

        try:
            self.injector.maybe_fail(FaultPoint.AFTER_SOURCE_REMOVE_BEFORE_INVENTORY_REFRESH)
        except Exception as exc:  # noqa: BLE001
            return self._fail(step_id=None, step_type="REFRESH_INVENTORY", error=exc, recovery_status="INVENTORY_REFRESH_INCOMPLETE")

        # 9. REFRESH_INVENTORY
        step = self._begin(9)
        try:
            scan_run_id = self._refresh_inventory(destination_path=destination_path, outcome=outcome)
        except Exception as exc:  # noqa: BLE001
            return self._fail(step_id=step.id, step_type="REFRESH_INVENTORY", error=exc, recovery_status="INVENTORY_REFRESH_INCOMPLETE")
        with self.connection:
            execution_repository.record_inventory_refresh_completed(self.connection, execution.id)
        self._complete(step.id, detail={"scan_run_id": scan_run_id})

        try:
            self.injector.maybe_fail(FaultPoint.AFTER_INVENTORY_REFRESH_BEFORE_FINALIZE)
        except Exception as exc:  # noqa: BLE001
            return self._fail(step_id=None, step_type="FINALIZE", error=exc, recovery_status="INVENTORY_REFRESH_INCOMPLETE")

        # 10. PLEX_REFRESH
        self._plex_step(10)

        # 11. FINALIZE
        self._finalize_step(
            11,
            destination_size_bytes=fs.stat_size_bytes(destination_path),
            destination_checksum=destination_checksum,
        )

        result = execution_repository.get_execution(self.connection, execution.id)
        assert result is not None
        return result


@dataclass(frozen=True)
class RecoveryGuidance:
    execution_id: int
    plan_id: int
    execution_status: str
    recovery_status: str | None
    source_exists: bool
    destination_exists: bool
    temp_file_exists: bool
    temp_file_paths: tuple[str, ...]
    lock_held: bool
    lock_matches_execution: bool
    recommendation: str

    def to_dict(self) -> dict[str, object]:
        return {
            "execution_id": self.execution_id,
            "plan_id": self.plan_id,
            "execution_status": self.execution_status,
            "recovery_status": self.recovery_status,
            "source_exists": self.source_exists,
            "destination_exists": self.destination_exists,
            "temp_file_exists": self.temp_file_exists,
            "temp_file_paths": list(self.temp_file_paths),
            "lock_held": self.lock_held,
            "lock_matches_execution": self.lock_matches_execution,
            "recommendation": self.recommendation,
        }


_RECOMMENDATIONS: dict[str, str] = {
    "NONE": "No recovery required; nothing was mutated before this execution failed.",
    "PARTIAL_DESTINATION_SOURCE_INTACT": (
        "A partial or unverified temporary file may exist in the destination directory and the source "
        "file is untouched. Inspect and remove the .mams-partial-* file if present, then generate a "
        "fresh plan; do not resume this execution."
    ),
    "DESTINATION_VERIFIED_SOURCE_NOT_REMOVED": (
        "The destination file was copied and checksum-verified, but the source file was not removed. "
        "Confirm the destination is correct, then remove the source manually if appropriate."
    ),
    "DESTINATION_UNVERIFIED_SOURCE_REMOVED": (
        "The source file was moved (same-filesystem hard-link move) but destination media verification "
        "did not complete or did not pass. The destination is byte-identical to the original by "
        "construction; inspect it directly (e.g. in VLC) and re-run inventory reconciliation if it looks correct."
    ),
    "INVENTORY_REFRESH_INCOMPLETE": (
        "The file transfer completed and verified successfully, but canonical inventory was not fully "
        "refreshed. Preserve the destination file; run a targeted inventory reconciliation for this "
        "media file rather than a full library scan."
    ),
    "INTERRUPTED_STATE_UNKNOWN": (
        "This execution was left EXECUTING with no live lock or process -- it was likely interrupted "
        "(crash or kill). Inspect the source, destination, and any temporary file manually before "
        "taking any action; do not assume no mutation occurred."
    ),
    "OTHER_REQUIRES_MANUAL_INSPECTION": "Manual inspection required; the recorded state does not match a known recovery scenario.",
}


def inspect_recovery(
    connection: sqlite3.Connection, config: AppConfig, *, execution_id: int
) -> RecoveryGuidance | None:
    """Strictly read-only: re-derives live filesystem/lock evidence for
    one execution and returns plain-English guidance. Never writes to
    the database or the filesystem, and never removes or repairs
    anything -- see docs/EXECUTION-SAFETY.md for the full recovery
    scenario list."""
    execution = execution_repository.get_execution(connection, execution_id)
    if execution is None:
        return None

    source_exists = fs.path_exists(execution.source_path)
    destination_exists = fs.path_exists(execution.destination_path)
    destination_directory = str(Path(execution.destination_path).parent)
    temp_file_paths: tuple[str, ...] = ()
    if fs.path_exists(destination_directory) and fs.is_directory(destination_directory):
        temp_prefix = f".{Path(execution.destination_path).name}.mams-partial-"
        temp_file_paths = tuple(
            sorted(
                str(Path(destination_directory) / name)
                for name in _list_directory_names(destination_directory)
                if name.startswith(temp_prefix)
            )
        )
    temp_file_exists = bool(temp_file_paths)

    lock_held = False
    lock_matches_execution = False
    state_directory = config.execution_state_directory
    if state_directory is not None:
        lock_info = read_lock(state_directory, execution.ingest_plan_id)
        lock_held = lock_info is not None
        lock_matches_execution = lock_info is not None and lock_info.token == execution.lock_token

    recovery_status = execution.recovery_status
    if execution.status == "EXECUTING" and not (lock_held and lock_matches_execution):
        recovery_status = "INTERRUPTED_STATE_UNKNOWN"

    recommendation = _RECOMMENDATIONS.get(recovery_status or "NONE", _RECOMMENDATIONS["OTHER_REQUIRES_MANUAL_INSPECTION"])
    if temp_file_paths:
        recommendation = f"{recommendation} Discovered temporary file(s): {', '.join(temp_file_paths)}"

    return RecoveryGuidance(
        execution_id=execution.id,
        plan_id=execution.ingest_plan_id,
        execution_status=execution.status,
        recovery_status=recovery_status,
        source_exists=source_exists,
        destination_exists=destination_exists,
        temp_file_exists=temp_file_exists,
        temp_file_paths=temp_file_paths,
        lock_held=lock_held,
        lock_matches_execution=lock_matches_execution,
        recommendation=recommendation,
    )


def _list_directory_names(directory: str) -> list[str]:
    return [entry.name for entry in Path(directory).iterdir()]
