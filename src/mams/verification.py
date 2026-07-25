"""Deterministic, narrowly-scoped media verification for dry-run ingest
planning (Milestone 7B, Phase H).

Pure module: no SQL, no filesystem access, no external calls. Takes a
plain `VerificationInput` (already-collected canonical inventory/MediaInfo
facts plus a blocking-finding count) and returns a `VerificationResult`
with a structured, itemized `checks` list -- `ingest_service.py` is the
only module that gathers this input from `inventory_repository.py` and
`findings_repository.py` and feeds it here.

Verification is deliberately narrow: it can only say a file is
structurally plausible, has the required tracks, and is free of blocking
findings -- based entirely on MediaInfo metadata already collected by the
inventory scanner. It never decodes, checksums, or otherwise proves a
file plays back correctly (no such capability exists elsewhere in MAMS
today either). Callers must phrase results as "ready for review," never
as "confirmed uncorrupted."
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Container names MediaInfo commonly reports, mapped to the file
# extensions considered plausible for them. Not exhaustive -- an
# unrecognized container is simply not checked (no false WARNING for a
# container this table doesn't know about yet).
_PLAUSIBLE_EXTENSIONS_BY_CONTAINER: dict[str, frozenset[str]] = {
    "Matroska": frozenset({".mkv"}),
    "MPEG-4": frozenset({".mp4", ".m4v"}),
    "QuickTime": frozenset({".mov", ".m4v"}),
    "AVI": frozenset({".avi"}),
    "MPEG-PS": frozenset({".mpg", ".mpeg", ".vob"}),
    "MPEG-TS": frozenset({".ts", ".m2ts"}),
    "Windows Media": frozenset({".wmv"}),
}


class CheckStatus(StrEnum):
    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"


class VerificationStatus(StrEnum):
    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"
    NOT_PROBED = "NOT_PROBED"


@dataclass(frozen=True)
class VerificationInput:
    """Everything `verify_media` needs, already collected elsewhere.
    `source_path_unchanged` defaults to True for a first-time
    verification; ingest plan regeneration (Phase J) sets it False when
    the source path it originally loaded no longer matches."""

    state: str
    candidate_type: str
    size_bytes: int
    duration_seconds: float | None
    media_info_error: str | None
    media_info_probed_at: str | None
    container: str | None
    extension: str
    video_track_count: int
    audio_track_count: int
    blocking_finding_count: int
    source_path_unchanged: bool = True


@dataclass(frozen=True)
class VerificationCheck:
    code: str
    status: CheckStatus
    evidence: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "status": self.status.value, "evidence": self.evidence}


@dataclass(frozen=True)
class VerificationResult:
    status: VerificationStatus
    checks: tuple[VerificationCheck, ...]

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status.value, "checks": [c.to_dict() for c in self.checks]}


def verify_media(data: VerificationInput) -> VerificationResult:
    """Run every check and derive an overall status.

    Overall status precedence: `NOT_PROBED` if MediaInfo never ran at all
    (nothing further can be assessed), else `FAIL` if any check failed,
    else `WARNING` if any check warned, else `PASS`.
    """
    checks: list[VerificationCheck] = []

    checks.append(
        VerificationCheck(
            code="source_active",
            status=CheckStatus.PASS if data.state == "ACTIVE" else CheckStatus.FAIL,
            evidence={"state": data.state},
        )
    )

    never_probed = data.media_info_error is None and data.media_info_probed_at is None
    if never_probed:
        checks.append(
            VerificationCheck(code="metadata_probed", status=CheckStatus.FAIL, evidence={"probed": False})
        )
        return VerificationResult(status=VerificationStatus.NOT_PROBED, checks=tuple(checks))

    checks.append(
        VerificationCheck(
            code="metadata_probed",
            status=CheckStatus.FAIL if data.media_info_error else CheckStatus.PASS,
            evidence={"probed": True, "error": data.media_info_error},
        )
    )

    checks.append(
        VerificationCheck(
            code="non_zero_size",
            status=CheckStatus.PASS if data.size_bytes > 0 else CheckStatus.FAIL,
            evidence={"size_bytes": data.size_bytes},
        )
    )

    duration_ok = data.duration_seconds is not None and data.duration_seconds > 0
    checks.append(
        VerificationCheck(
            code="duration_present",
            status=CheckStatus.PASS if duration_ok else CheckStatus.FAIL,
            evidence={"duration_seconds": data.duration_seconds},
        )
    )

    checks.append(
        VerificationCheck(
            code="video_track_present",
            status=CheckStatus.PASS if data.video_track_count >= 1 else CheckStatus.FAIL,
            evidence={"video_track_count": data.video_track_count},
        )
    )

    # Missing audio is a WARNING, not a FAIL: a soundless archival capture
    # is unusual but not implausible, and this is explicitly a narrow,
    # conservative check -- see the module docstring.
    checks.append(
        VerificationCheck(
            code="audio_track_present",
            status=CheckStatus.PASS if data.audio_track_count >= 1 else CheckStatus.WARNING,
            evidence={"audio_track_count": data.audio_track_count},
        )
    )

    plausible_extensions = _PLAUSIBLE_EXTENSIONS_BY_CONTAINER.get(data.container or "")
    if plausible_extensions is not None:
        checks.append(
            VerificationCheck(
                code="container_extension_plausible",
                status=CheckStatus.PASS if data.extension.lower() in plausible_extensions else CheckStatus.WARNING,
                evidence={"container": data.container, "extension": data.extension},
            )
        )

    checks.append(
        VerificationCheck(
            code="no_blocking_findings",
            status=CheckStatus.PASS if data.blocking_finding_count == 0 else CheckStatus.FAIL,
            evidence={"blocking_finding_count": data.blocking_finding_count},
        )
    )

    checks.append(
        VerificationCheck(
            code="source_path_unchanged",
            status=CheckStatus.PASS if data.source_path_unchanged else CheckStatus.FAIL,
            evidence={"unchanged": data.source_path_unchanged},
        )
    )

    if any(check.status == CheckStatus.FAIL for check in checks):
        overall = VerificationStatus.FAIL
    elif any(check.status == CheckStatus.WARNING for check in checks):
        overall = VerificationStatus.WARNING
    else:
        overall = VerificationStatus.PASS

    return VerificationResult(status=overall, checks=tuple(checks))
