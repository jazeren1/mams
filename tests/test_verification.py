"""Tests for verification.py: deterministic media verification checks."""

from __future__ import annotations

from mams.verification import VerificationInput, VerificationStatus, verify_media


def _healthy_movie(**overrides: object) -> VerificationInput:
    defaults: dict[str, object] = dict(
        state="ACTIVE",
        candidate_type="MOVIE",
        size_bytes=1_000_000,
        duration_seconds=7020.0,
        media_info_error=None,
        media_info_probed_at="2024-01-01T00:00:00",
        container="Matroska",
        extension=".mkv",
        video_track_count=1,
        audio_track_count=1,
        blocking_finding_count=0,
    )
    defaults.update(overrides)
    return VerificationInput(**defaults)  # type: ignore[arg-type]


def test_healthy_movie_passes() -> None:
    result = verify_media(_healthy_movie())
    assert result.status == VerificationStatus.PASS
    assert all(c.status == "PASS" for c in result.checks)


def test_no_video_track_blocks() -> None:
    result = verify_media(_healthy_movie(video_track_count=0))
    assert result.status == VerificationStatus.FAIL
    video_check = next(c for c in result.checks if c.code == "video_track_present")
    assert video_check.status == "FAIL"


def test_no_audio_track_warns_not_blocks() -> None:
    result = verify_media(_healthy_movie(audio_track_count=0))
    assert result.status == VerificationStatus.WARNING
    audio_check = next(c for c in result.checks if c.code == "audio_track_present")
    assert audio_check.status == "WARNING"


def test_zero_byte_file_blocks() -> None:
    result = verify_media(_healthy_movie(size_bytes=0))
    assert result.status == VerificationStatus.FAIL
    size_check = next(c for c in result.checks if c.code == "non_zero_size")
    assert size_check.status == "FAIL"


def test_metadata_error_blocks() -> None:
    result = verify_media(_healthy_movie(media_info_error="mediainfo: corrupt file"))
    assert result.status == VerificationStatus.FAIL
    metadata_check = next(c for c in result.checks if c.code == "metadata_probed")
    assert metadata_check.status == "FAIL"


def test_never_probed_is_not_probed_overall() -> None:
    result = verify_media(_healthy_movie(media_info_error=None, media_info_probed_at=None))
    assert result.status == VerificationStatus.NOT_PROBED


def test_missing_duration_blocks() -> None:
    result = verify_media(_healthy_movie(duration_seconds=None))
    assert result.status == VerificationStatus.FAIL


def test_zero_duration_blocks() -> None:
    result = verify_media(_healthy_movie(duration_seconds=0.0))
    assert result.status == VerificationStatus.FAIL


def test_active_error_or_critical_finding_blocks() -> None:
    result = verify_media(_healthy_movie(blocking_finding_count=1))
    assert result.status == VerificationStatus.FAIL
    finding_check = next(c for c in result.checks if c.code == "no_blocking_findings")
    assert finding_check.status == "FAIL"


def test_source_missing_blocks() -> None:
    result = verify_media(_healthy_movie(state="MISSING"))
    assert result.status == VerificationStatus.FAIL
    source_check = next(c for c in result.checks if c.code == "source_active")
    assert source_check.status == "FAIL"


def test_source_path_changed_blocks() -> None:
    result = verify_media(_healthy_movie(source_path_unchanged=False))
    assert result.status == VerificationStatus.FAIL


def test_implausible_container_extension_warns() -> None:
    result = verify_media(_healthy_movie(container="Matroska", extension=".mp4"))
    assert result.status == VerificationStatus.WARNING
    container_check = next(c for c in result.checks if c.code == "container_extension_plausible")
    assert container_check.status == "WARNING"


def test_unrecognized_container_is_not_checked() -> None:
    result = verify_media(_healthy_movie(container="SomeFutureContainer", extension=".mkv"))
    assert result.status == VerificationStatus.PASS
    assert not any(c.code == "container_extension_plausible" for c in result.checks)


def test_verification_output_is_deterministic() -> None:
    results = [verify_media(_healthy_movie()).to_dict() for _ in range(5)]
    assert all(r == results[0] for r in results)


def test_to_dict_matches_documented_shape() -> None:
    result = verify_media(_healthy_movie())
    data = result.to_dict()
    assert set(data.keys()) == {"status", "checks"}
    assert isinstance(data["checks"], list)
    check = data["checks"][0]
    assert set(check.keys()) == {"code", "status", "evidence"}
