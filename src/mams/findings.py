"""Pure, DB-unaware findings domain model and rule engine.

This module evaluates canonical inventory records (`inventory_repository.
MediaFileRecord`) and produces `FindingCandidate` objects describing
conditions that may need attention. It never touches SQLite or the
filesystem -- `findings_repository.py` owns all findings-related SQL, and
`findings_service.py` wires the two together inside one atomic
transaction. Rules here are simple, pure functions; each is independently
callable and testable with a hand-built `MediaFileRecord`.

## Rule scoping: ACTIVE vs MISSING

`missing_file` is the only rule evaluated against a `MISSING` row -- once a
file is missing, every other rule is skipped for it. A `MISSING` row's
metadata/track columns hold whatever was last recorded while the file was
still `ACTIVE` (see inventory_repository.py's write path); re-evaluating
`metadata_error`/`unknown_layout`/`no_video_track`/etc. against that stale
snapshot would produce findings about data that no longer describes
anything real. All other rules require `state == 'ACTIVE'`.

## suspiciously_small_media vs zero_byte_file

These two rules are mutually exclusive by construction:
`suspiciously_small_media` requires `size_bytes > 0`, so a zero-byte file
is reported exactly once, by `zero_byte_file` -- never doubly reported by
two rules describing what is really the same underlying problem.

## Determinism

`evaluate_all()` sorts its output by `(rule_code, media_file_id)` before
returning, independent of whatever order the input records arrived in.
Combined with each rule being a pure function of its input record, this
means the same inventory state always produces the same candidate list,
byte-for-byte -- required for `findings_repository.reconcile_findings()`
to avoid spurious `updated_at` churn on an unchanged re-evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Iterable

from . import inventory
from .inventory_repository import MediaFileRecord

# Conservative default: real feature-length movies/episodes, even at low
# bitrate, are essentially never this small. A low threshold means fewer
# false positives (a legitimately compact rip won't be flagged) at the
# cost of not catching every marginal case -- consistent with this rule
# only ever claiming "unusually small," never "corrupt."
SUSPICIOUSLY_SMALL_MEDIA_THRESHOLD_BYTES = 10 * 1024 * 1024  # 10 MB


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class FindingStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"
    IGNORED = "IGNORED"


@dataclass(frozen=True)
class FindingCandidate:
    """A rule's deterministic judgment that a condition exists right now.

    `evidence` must contain only structured facts supporting the finding
    (e.g. a byte count, an error string, a threshold) -- never a duplicate
    of the media_files record itself. See docs/DATABASE.md ("findings").
    """

    rule_code: str
    severity: Severity
    media_file_id: int
    library_id: int
    summary: str
    evidence: dict[str, object]
    recommendation: str


@dataclass(frozen=True)
class Rule:
    """One findings rule: a stable code paired with its evaluation function."""

    code: str
    evaluate: Callable[[MediaFileRecord], "FindingCandidate | None"]


def _rule_missing_file(record: MediaFileRecord) -> FindingCandidate | None:
    if record.state != "MISSING":
        return None
    return FindingCandidate(
        rule_code="missing_file",
        severity=Severity.ERROR,
        media_file_id=record.id,
        library_id=record.library_id,
        summary=f"File is no longer found on the NAS: {record.relative_path}",
        evidence={
            "last_seen_scan_id": record.last_seen_scan_id,
            "missing_since_scan_id": record.missing_since_scan_id,
        },
        recommendation=(
            "Confirm whether this file was intentionally removed. If not, check for a "
            "NAS mount or connectivity issue before assuming data loss."
        ),
    )


def _rule_metadata_error(record: MediaFileRecord) -> FindingCandidate | None:
    if record.state != "ACTIVE" or record.media_info_error is None:
        return None
    return FindingCandidate(
        rule_code="metadata_error",
        severity=Severity.ERROR,
        media_file_id=record.id,
        library_id=record.library_id,
        summary="MediaInfo failed to probe this file.",
        evidence={"error": record.media_info_error},
        recommendation=(
            "Investigate the MediaInfo error; the file may be corrupt, unreadable, "
            "or in an unsupported container."
        ),
    )


def _rule_metadata_not_probed(record: MediaFileRecord) -> FindingCandidate | None:
    if record.state != "ACTIVE":
        return None
    if record.media_info_probed_at is not None or record.media_info_error is not None:
        return None
    return FindingCandidate(
        rule_code="metadata_not_probed",
        severity=Severity.WARNING,
        media_file_id=record.id,
        library_id=record.library_id,
        summary="Technical metadata has not been extracted for this file yet.",
        evidence={},
        recommendation="Run `mams inventory scan --metadata` to extract MediaInfo for this file.",
    )


def _rule_unknown_layout(record: MediaFileRecord) -> FindingCandidate | None:
    if record.state != "ACTIVE" or record.layout != "unknown":
        return None
    return FindingCandidate(
        rule_code="unknown_layout",
        severity=Severity.WARNING,
        media_file_id=record.id,
        library_id=record.library_id,
        summary=f"File does not match a recognized layout for its category ({record.category}).",
        evidence={"layout": record.layout},
        recommendation="Verify the file's directory structure matches the expected movie/TV layout for its category.",
    )


def _rule_zero_byte_file(record: MediaFileRecord) -> FindingCandidate | None:
    if record.state != "ACTIVE" or record.size_bytes != 0:
        return None
    return FindingCandidate(
        rule_code="zero_byte_file",
        severity=Severity.ERROR,
        media_file_id=record.id,
        library_id=record.library_id,
        summary="File is zero bytes.",
        evidence={"size_bytes": 0},
        recommendation="Investigate the source of this file; it may have failed to copy. Do not delete without verification.",
    )


def _rule_suspiciously_small_media(record: MediaFileRecord) -> FindingCandidate | None:
    if record.state != "ACTIVE" or record.size_bytes <= 0:
        return None
    if record.extension not in inventory.SUPPORTED_EXTENSIONS:
        return None
    if record.size_bytes >= SUSPICIOUSLY_SMALL_MEDIA_THRESHOLD_BYTES:
        return None
    return FindingCandidate(
        rule_code="suspiciously_small_media",
        severity=Severity.WARNING,
        media_file_id=record.id,
        library_id=record.library_id,
        summary=f"File is unusually small for a media file ({record.size_bytes} bytes).",
        evidence={
            "size_bytes": record.size_bytes,
            "threshold_bytes": SUSPICIOUSLY_SMALL_MEDIA_THRESHOLD_BYTES,
        },
        recommendation=(
            "Confirm this is a complete, valid copy -- unusually small size may indicate a "
            "partial rip, sample, or trailer, not necessarily corruption."
        ),
    )


def _rule_no_video_track(record: MediaFileRecord) -> FindingCandidate | None:
    if record.state != "ACTIVE":
        return None
    if record.media_info_probed_at is None or record.media_info_error is not None:
        return None
    if record.video_track_count != 0:
        return None
    return FindingCandidate(
        rule_code="no_video_track",
        severity=Severity.ERROR,
        media_file_id=record.id,
        library_id=record.library_id,
        summary="No video track detected despite a successful MediaInfo probe.",
        evidence={"video_track_count": 0},
        recommendation="Verify the file actually contains video; investigate the ripping or encoding output.",
    )


def _rule_no_audio_track(record: MediaFileRecord) -> FindingCandidate | None:
    if record.state != "ACTIVE":
        return None
    if record.media_info_probed_at is None or record.media_info_error is not None:
        return None
    if record.audio_track_count != 0:
        return None
    return FindingCandidate(
        rule_code="no_audio_track",
        severity=Severity.WARNING,
        media_file_id=record.id,
        library_id=record.library_id,
        summary="No audio track detected despite a successful MediaInfo probe.",
        evidence={"audio_track_count": 0},
        recommendation="Verify the file actually contains audio; investigate the ripping or encoding output.",
    )


def _rule_unexpected_extension(record: MediaFileRecord) -> FindingCandidate | None:
    if record.state != "ACTIVE" or record.extension in inventory.SUPPORTED_EXTENSIONS:
        return None
    return FindingCandidate(
        rule_code="unexpected_extension",
        severity=Severity.WARNING,
        media_file_id=record.id,
        library_id=record.library_id,
        summary=f"File extension '{record.extension}' is outside the configured supported-media extension set.",
        evidence={
            "extension": record.extension,
            "supported_extensions": sorted(inventory.SUPPORTED_EXTENSIONS),
        },
        recommendation="Confirm this file is a legitimate media asset; unsupported extensions are not scanned for new discovery.",
    )


# Order here is the tie-break rank findings_repository.py uses to order a
# same-file, same-severity group of findings deterministically (see
# findings_repository._RULE_RANK_SQL). Adding a new rule appends to the
# end; reordering existing rules changes that tie-break order.
ALL_RULES: tuple[Rule, ...] = (
    Rule("missing_file", _rule_missing_file),
    Rule("metadata_error", _rule_metadata_error),
    Rule("metadata_not_probed", _rule_metadata_not_probed),
    Rule("unknown_layout", _rule_unknown_layout),
    Rule("zero_byte_file", _rule_zero_byte_file),
    Rule("suspiciously_small_media", _rule_suspiciously_small_media),
    Rule("no_video_track", _rule_no_video_track),
    Rule("no_audio_track", _rule_no_audio_track),
    Rule("unexpected_extension", _rule_unexpected_extension),
)


def evaluate_all(records: Iterable[MediaFileRecord]) -> list[FindingCandidate]:
    """Evaluate every rule against every record, returning all candidates.

    Deterministically ordered by (rule_code, media_file_id), independent of
    input order -- see the module docstring's "Determinism" section.
    """
    candidates = [
        candidate
        for record in records
        for rule in ALL_RULES
        if (candidate := rule.evaluate(record)) is not None
    ]
    candidates.sort(key=lambda c: (c.rule_code, c.media_file_id))
    return candidates
