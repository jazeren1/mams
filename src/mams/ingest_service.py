"""Orchestrates `mams ingest plan`: eligibility -> verification ->
destination naming -> collision analysis -> dry-run plan persistence.

This is the only module that combines `verification.py` and
`destination.py` (both pure) with `inventory_repository.py`,
`identification_repository.py`, `resolution_repository.py`, and
`findings_repository.py` (read-only lookups) and `ingest_repository.py`
(the primary writer). Mirrors the role `resolution_service.py` plays for
resolution.

`confirm_identity()` (Milestone 8.3) is this module's one deliberate
exception: it writes `media_identity_assignments.confirmed_for_ingest_at`/
`confirmed_by` via `resolution_repository.confirm_assignment_for_ingest`
rather than only reading `resolution_repository`, because "confirmed for
ingest" is an ingest-domain fact whose single source of truth is the
assignment row itself -- duplicating it onto `ingest_plans` would require
re-deriving it on every regeneration instead of surviving one.

This milestone never renames, moves, copies, deletes, replaces media, or
creates a directory -- every proposed action this module writes is
inert, database-only, and explicitly marked PROPOSED / NOT EXECUTED (see
`_build_actions`). The one filesystem touch here (`Path.exists()` in
`_check_collisions`) is a read-only safety check, the same kind Phase J
explicitly allows -- never a write, never a recursive scan (that remains
`inventory.scan_category`'s sole job).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path, PurePosixPath

from . import findings_repository, identification_repository, inventory_repository, resolution_repository
from .config import AppConfig
from .destination import DestinationError, DestinationPlan, episode_destination, movie_destination
from .ingest_repository import PlanActionInput, PlanRecord, approve_plan, get_plan, reconcile_plan
from .inventory_repository import MediaFileRecord
from .readiness import PlanActionSnapshot, ReadinessInput, ReadinessResult, evaluate_readiness
from .resolution_repository import AssignmentRecord, ExternalIdentityRecord
from .verification import VerificationInput, VerificationStatus, verify_media

DESTINATION_CATEGORY_HINTS = frozenset({"movie", "kids_movie", "tv", "kids_tv"})
_KIND_ALLOWED_CATEGORIES = {"movie": {"movie", "kids_movie"}, "tv": {"tv", "kids_tv"}}
_PROPOSED_NOT_EXECUTED = {"execution_state": "PROPOSED_NOT_EXECUTED"}


class IngestPlanError(Exception):
    """Raised for a usage-level rejection *before* any plan row is
    written -- an unknown media_file_id, a file outside every configured
    incoming root, or an invalid --destination-category value. These are
    caller mistakes, not planning outcomes (contrast with BLOCKED/
    REVIEW_REQUIRED, which are legitimate persisted plan states)."""


def _is_under_incoming_root(absolute_path: str, config: AppConfig) -> bool:
    for root in config.ingest_incoming_roots:
        normalized_root = root.rstrip("/")
        if absolute_path == normalized_root or absolute_path.startswith(normalized_root + "/"):
            return True
    return False


def _destination_kind(media_type: str) -> str:
    return "movie" if media_type == "MOVIE" else "tv"


def _compute_destination(
    config: AppConfig,
    identity: ExternalIdentityRecord,
    media_file: MediaFileRecord,
    destination_category: str,
) -> tuple[DestinationPlan | None, str | None]:
    kind = _destination_kind(identity.media_type)
    if destination_category not in _KIND_ALLOWED_CATEGORIES[kind]:
        return None, f"destination category {destination_category!r} is not valid for a {identity.media_type} identity"

    category_key = config.ingest_destination_categories[destination_category]
    destination_root = config.nas_categories.get(category_key)
    if not destination_root:
        return None, f"no NAS root configured for destination category {category_key!r}"

    try:
        if kind == "movie":
            if identity.release_year is None:
                return None, "resolved identity has no release year; cannot build a movie destination"
            plan = movie_destination(
                title=identity.title or "", year=identity.release_year,
                extension=media_file.extension, destination_root=destination_root,
            )
        else:
            if identity.season_number is None or identity.episode_number is None:
                return None, "resolved identity has no season/episode number; cannot build an episode destination"
            plan = episode_destination(
                series_title=identity.title or "", season_number=identity.season_number,
                episode_numbers=(identity.episode_number,), extension=media_file.extension,
                destination_root=destination_root, episode_title=identity.episode_title,
            )
    except DestinationError as exc:
        return None, f"destination naming failed: {exc}"
    return plan, None


def _check_collisions(
    connection: sqlite3.Connection, media_file: MediaFileRecord, plan: DestinationPlan
) -> list[str]:
    """Read-only safety checks against canonical inventory and the
    filesystem (Phase J). Every collision detected here is treated as
    blocking -- this milestone does not attempt fuzzy "possible edition
    collision" detection."""
    reasons: list[str] = []

    if media_file.absolute_path == plan.full_path:
        reasons.append("source and destination paths are identical")

    if Path(plan.full_path).exists():
        reasons.append(f"destination path already exists on disk: {plan.full_path}")
    elif Path(plan.directory).exists() and not Path(plan.directory).is_dir():
        reasons.append(f"destination directory path is occupied by a file: {plan.directory}")

    if connection.execute("SELECT 1 FROM media_files WHERE absolute_path = ?", (plan.full_path,)).fetchone():
        reasons.append("destination path already exists in canonical inventory")

    conflicting = connection.execute(
        "SELECT id FROM ingest_plans WHERE destination_directory = ? AND destination_filename = ? "
        "AND status != 'SUPERSEDED' AND media_file_id != ?",
        (plan.directory, plan.filename, media_file.id),
    ).fetchone()
    if conflicting is not None:
        reasons.append(f"another active plan (#{conflicting['id']}) targets the same destination")

    return reasons


def _build_actions(plan: DestinationPlan | None, media_file: MediaFileRecord) -> list[PlanActionInput]:
    actions = [
        PlanActionInput(
            action_type="VALIDATE_SOURCE", source_path=media_file.absolute_path, destination_path=None,
            details=dict(_PROPOSED_NOT_EXECUTED),
        ),
        PlanActionInput(
            action_type="VERIFY_MEDIA", source_path=media_file.absolute_path, destination_path=None,
            details=dict(_PROPOSED_NOT_EXECUTED),
        ),
    ]
    if plan is None:
        return actions

    actions.append(
        PlanActionInput(
            action_type="CREATE_DIRECTORY", source_path=None, destination_path=plan.directory,
            details=dict(_PROPOSED_NOT_EXECUTED),
        )
    )
    source_directory = str(PurePosixPath(media_file.absolute_path).parent)
    move_type = "RENAME" if source_directory == plan.directory else "MOVE"
    actions.append(
        PlanActionInput(
            action_type=move_type, source_path=media_file.absolute_path, destination_path=plan.full_path,
            details=dict(_PROPOSED_NOT_EXECUTED),
        )
    )
    actions.append(
        PlanActionInput(action_type="REFRESH_INVENTORY", source_path=None, destination_path=None, details=dict(_PROPOSED_NOT_EXECUTED))
    )
    actions.append(
        PlanActionInput(
            action_type="REQUEST_PLEX_REFRESH", source_path=None, destination_path=None, details=dict(_PROPOSED_NOT_EXECUTED)
        )
    )
    return actions


def _build_summary(status: str, blocking_reasons: list[str], review_reasons: list[str]) -> str:
    if status == "BLOCKED":
        return "Blocked: " + "; ".join(blocking_reasons)
    if status == "REVIEW_REQUIRED":
        return "Review required: " + "; ".join(review_reasons)
    return "Ready for review; no blocking issues found."


def generate_plan(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    media_file_id: int,
    destination_category: str | None = None,
) -> PlanRecord:
    """Generate (or reconcile) the current dry-run ingest plan for one
    media file. Raises `IngestPlanError` for a usage-level problem (see
    that class's docstring); otherwise always returns a persisted
    `PlanRecord` whose `status` reflects whatever was found -- including
    BLOCKED. No media file is ever read beyond its already-collected
    canonical metadata; no directory is created; no Plex call is made.
    """
    media_file = inventory_repository.get_media_file(connection, media_file_id)
    if media_file is None:
        raise IngestPlanError(f"No media file with id {media_file_id}")
    if not _is_under_incoming_root(media_file.absolute_path, config):
        raise IngestPlanError(
            f"Media file {media_file_id} ({media_file.absolute_path}) is not under a configured ingest root"
        )
    if destination_category is not None and destination_category not in DESTINATION_CATEGORY_HINTS:
        raise IngestPlanError(
            f"Unknown destination category {destination_category!r}; expected one of {sorted(DESTINATION_CATEGORY_HINTS)}"
        )

    candidates = identification_repository.list_candidates(connection, media_file_id=media_file_id)
    candidate = candidates[0] if candidates else None

    assignment: AssignmentRecord | None = resolution_repository.get_active_assignment(connection, media_file_id)
    identity: ExternalIdentityRecord | None = None
    if assignment is not None:
        identity = resolution_repository.get_external_identity(connection, assignment.external_identity_id)

    blocking_reasons: list[str] = []
    review_reasons: list[str] = []

    if candidate is None:
        blocking_reasons.append("no local identification candidate exists for this file")
    if assignment is None or identity is None:
        blocking_reasons.append("no resolved ACTIVE external identity assignment exists for this file")
    elif assignment.assignment_method == "MANUAL" and assignment.confirmed_for_ingest_at is None:
        review_reasons.append("identity was manually selected and has not yet been confirmed for ingest")

    error_count = len(findings_repository.list_findings(connection, media_file_id=media_file_id, status="ACTIVE", severity="ERROR"))
    critical_count = len(
        findings_repository.list_findings(connection, media_file_id=media_file_id, status="ACTIVE", severity="CRITICAL")
    )
    verification_input = VerificationInput(
        state=media_file.state,
        candidate_type=candidate.candidate_type if candidate is not None else "UNKNOWN",
        size_bytes=media_file.size_bytes,
        duration_seconds=media_file.duration_seconds,
        media_info_error=media_file.media_info_error,
        media_info_probed_at=media_file.media_info_probed_at,
        container=media_file.container,
        extension=media_file.extension,
        video_track_count=media_file.video_track_count,
        audio_track_count=media_file.audio_track_count,
        blocking_finding_count=error_count + critical_count,
    )
    verification_result = verify_media(verification_input)
    if verification_result.status in (VerificationStatus.FAIL, VerificationStatus.NOT_PROBED):
        blocking_reasons.append(f"media verification did not pass (status={verification_result.status.value})")
    elif verification_result.status == VerificationStatus.WARNING:
        review_reasons.append("media verification produced warnings")

    destination_plan: DestinationPlan | None = None
    if not blocking_reasons:
        assert identity is not None
        if destination_category is None:
            review_reasons.append("destination category not specified")
        else:
            destination_plan, destination_error = _compute_destination(config, identity, media_file, destination_category)
            if destination_error is not None:
                blocking_reasons.append(destination_error)

    if destination_plan is not None:
        blocking_reasons.extend(_check_collisions(connection, media_file, destination_plan))

    if blocking_reasons:
        status = "BLOCKED"
    elif review_reasons:
        status = "REVIEW_REQUIRED"
    else:
        status = "READY_FOR_REVIEW"

    actions = _build_actions(destination_plan, media_file)
    summary = _build_summary(status, blocking_reasons, review_reasons)

    with connection:
        return reconcile_plan(
            connection,
            media_file_id=media_file_id,
            identification_candidate_id=candidate.id if candidate is not None else None,
            media_identity_assignment_id=assignment.id if assignment is not None else None,
            status=status,
            source_path=media_file.absolute_path,
            source_size_bytes=media_file.size_bytes,
            source_mtime=media_file.mtime,
            destination_library=destination_plan.library_root if destination_plan is not None else None,
            destination_directory=destination_plan.directory if destination_plan is not None else None,
            destination_filename=destination_plan.filename if destination_plan is not None else None,
            verification_status=verification_result.status.value,
            verification=verification_result.to_dict(),
            blocking_reasons=blocking_reasons + review_reasons,
            summary=summary,
            actions=actions,
        )


def approve(connection: sqlite3.Connection, plan_id: int) -> PlanRecord:
    """Flip a READY_FOR_REVIEW plan to APPROVED. Database state only --
    see ingest_repository.approve_plan. Raises PlanNotApprovableError for
    anything else."""
    with connection:
        return approve_plan(connection, plan_id)


def confirm_identity(connection: sqlite3.Connection, plan_id: int) -> AssignmentRecord:
    """Explicitly confirm the manually selected identity behind plan_id
    for ingest -- the operator action `docs/INGEST-WORKFLOW.md` documents
    between `resolve select` and regenerating the plan. Does not itself
    change `plan_id`'s status: the operator must re-run `mams ingest plan
    MEDIA_FILE_ID ...` afterward so `generate_plan` recomputes and
    reconciles the plan in place (the same discipline every other
    blocking/review reason already follows -- "the audit reports state;
    explicit commands perform changes").

    Confirms only the exact assignment `plan_id` was generated against --
    if the file's current ACTIVE assignment has since changed (a new
    `resolve select`/`resolve evaluate` superseded it), this raises
    `IngestPlanError` rather than silently confirming a different
    identity than the one the operator reviewed on this plan; regenerate
    the plan first. Raises `IngestPlanError` for an unknown plan, a plan
    with no resolved assignment at all, or an assignment that doesn't
    need/support confirmation (already AUTO-resolved, or no longer
    ACTIVE) -- `resolution_repository.confirm_assignment_for_ingest`'s
    own errors are re-raised as this module's usage-level exception so
    every `mams ingest` CLI handler can catch one error type.

    Writes only `media_identity_assignments.confirmed_for_ingest_at`/
    `confirmed_by`, via `resolution_repository.confirm_assignment_for_ingest`
    -- the one write this module makes outside `ingest_repository.py`,
    because "confirmed for ingest" is recorded on the assignment row
    (the concept's natural, single source of truth -- see
    docs/DECISIONS.md) rather than duplicated onto the plan. No
    filesystem access."""
    plan = get_plan(connection, plan_id)
    if plan is None:
        raise IngestPlanError(f"No ingest plan with id {plan_id}")
    if plan.media_identity_assignment_id is None:
        raise IngestPlanError(f"Plan {plan_id} has no resolved identity assignment to confirm")

    current_assignment = resolution_repository.get_active_assignment(connection, plan.media_file_id)
    if current_assignment is None or current_assignment.id != plan.media_identity_assignment_id:
        raise IngestPlanError(
            f"Plan {plan_id}'s identity assignment is no longer the current one for media file "
            f"{plan.media_file_id} -- regenerate the plan (mams ingest plan {plan.media_file_id} ...) before confirming"
        )

    with connection:
        try:
            return resolution_repository.confirm_assignment_for_ingest(connection, assignment_id=current_assignment.id)
        except resolution_repository.AssignmentNotConfirmableError as exc:
            raise IngestPlanError(str(exc)) from exc


def _path_is_inside(path: str, root: str) -> bool:
    normalized_root = root.rstrip("/")
    return path == normalized_root or path.startswith(normalized_root + "/")


def audit_plan(connection: sqlite3.Connection, config: AppConfig, *, plan_id: int) -> ReadinessResult:
    """Read-only execution-readiness audit for one dry-run ingest plan
    (Milestone 7C, Phase F) -- the formal contract a future Milestone 8
    executor is expected to rely on. Gathers a fresh `readiness.ReadinessInput`
    from the database/config and a read-only `Path.exists()` check (the
    same kind `_check_collisions` above already performs), then hands it
    to the pure `readiness.evaluate_readiness()`. Writes nothing: this
    function contains no INSERT/UPDATE/DELETE anywhere. Raises
    `IngestPlanError` for an unknown plan_id, the same usage-level
    rejection `generate_plan` uses for an unknown media_file_id.
    """
    plan = get_plan(connection, plan_id)
    if plan is None:
        raise IngestPlanError(f"No ingest plan with id {plan_id}")

    media_file = inventory_repository.get_media_file(connection, plan.media_file_id)

    current_candidates = identification_repository.list_candidates(connection, media_file_id=plan.media_file_id)
    current_candidate_id = current_candidates[0].id if current_candidates else None
    current_override = identification_repository.get_active_override(connection, plan.media_file_id)

    plan_assignment = (
        resolution_repository.get_assignment(connection, plan.media_identity_assignment_id)
        if plan.media_identity_assignment_id is not None
        else None
    )
    plan_override_id: int | None = None
    if plan_assignment is not None and plan_assignment.resolution_attempt_id is not None:
        plan_attempt = resolution_repository.get_attempt(connection, plan_assignment.resolution_attempt_id)
        if plan_attempt is not None:
            plan_override_id = plan_attempt.identification_override_id

    external_identity_exists = (
        plan_assignment is not None
        and resolution_repository.get_external_identity(connection, plan_assignment.external_identity_id) is not None
    )

    current_assignment = resolution_repository.get_active_assignment(connection, plan.media_file_id)

    blocking_finding_count = len(
        findings_repository.list_findings(connection, media_file_id=plan.media_file_id, status="ACTIVE", severity="ERROR")
    ) + len(
        findings_repository.list_findings(
            connection, media_file_id=plan.media_file_id, status="ACTIVE", severity="CRITICAL"
        )
    )

    destination_root_configured = (
        plan.destination_library is not None and plan.destination_library in config.nas_categories.values()
    )
    destination_path_inside_root = (
        plan.destination_library is not None
        and plan.destination_directory is not None
        and _path_is_inside(plan.destination_directory, plan.destination_library)
    )

    destination_still_unoccupied = True
    competing_plan_id: int | None = None
    if plan.destination_directory is not None and plan.destination_filename is not None:
        full_path = str(PurePosixPath(plan.destination_directory) / plan.destination_filename)
        if Path(full_path).exists():
            destination_still_unoccupied = False
        elif connection.execute("SELECT 1 FROM media_files WHERE absolute_path = ?", (full_path,)).fetchone():
            destination_still_unoccupied = False
        competing = connection.execute(
            "SELECT id FROM ingest_plans WHERE destination_directory = ? AND destination_filename = ? "
            "AND status != 'SUPERSEDED' AND media_file_id != ?",
            (plan.destination_directory, plan.destination_filename, plan.media_file_id),
        ).fetchone()
        competing_plan_id = competing["id"] if competing is not None else None

    def _execution_state(details: dict[str, object] | None) -> str | None:
        value = (details or {}).get("execution_state")
        return value if isinstance(value, str) else None

    action_snapshots = tuple(
        PlanActionSnapshot(
            action_order=action.action_order,
            action_type=action.action_type,
            execution_state=_execution_state(action.details),
            overwrite_requested=bool((action.details or {}).get("overwrite")),
        )
        for action in plan.actions
    )

    readiness_input = ReadinessInput(
        plan_status=plan.status,
        plan_source_path=plan.source_path,
        plan_source_size_bytes=plan.source_size_bytes,
        plan_source_mtime=plan.source_mtime,
        plan_identification_candidate_id=plan.identification_candidate_id,
        plan_identification_override_id=plan_override_id,
        plan_media_identity_assignment_id=plan.media_identity_assignment_id,
        plan_destination_library=plan.destination_library,
        plan_destination_directory=plan.destination_directory,
        plan_destination_filename=plan.destination_filename,
        plan_verification_status=plan.verification_status,
        plan_actions=action_snapshots,
        source_media_file_exists=media_file is not None,
        source_state=media_file.state if media_file is not None else None,
        source_absolute_path=media_file.absolute_path if media_file is not None else None,
        source_size_bytes=media_file.size_bytes if media_file is not None else None,
        source_mtime=media_file.mtime if media_file is not None else None,
        current_identification_candidate_id=current_candidate_id,
        current_active_override_id=current_override.id if current_override is not None else None,
        current_active_assignment_id=current_assignment.id if current_assignment is not None else None,
        external_identity_exists=external_identity_exists,
        current_blocking_finding_count=blocking_finding_count,
        destination_root_configured=destination_root_configured,
        destination_path_inside_root=destination_path_inside_root,
        destination_still_unoccupied=destination_still_unoccupied,
        competing_plan_id=competing_plan_id,
    )
    return evaluate_readiness(readiness_input)
