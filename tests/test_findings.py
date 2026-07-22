"""Tests for the findings domain/rule engine (src/mams/findings.py).

Rules are pure functions of a MediaFileRecord, so these tests hand-build
records via `_record(**overrides)` rather than going through a real scan --
each rule is exercised in isolation, both triggering and not triggering.
"""

from __future__ import annotations

from mams import findings
from mams.inventory_repository import MediaFileRecord


def _record(**overrides: object) -> MediaFileRecord:
    """A record representing a healthy, fully-probed ACTIVE movie file.

    Individual tests override just the field(s) relevant to the rule under
    test, so each test's intent stays obvious from its overrides alone.
    """
    defaults: dict[str, object] = dict(
        id=1,
        library_id=10,
        category="movies",
        absolute_path="/Volumes/movies/Movie (2001).mkv",
        relative_path="Movie (2001).mkv",
        filename="Movie (2001).mkv",
        extension=".mkv",
        parent_directory="/Volumes/movies",
        layout="movie_flat",
        size_bytes=50_000_000_000,
        mtime=1234567890.0,
        state="ACTIVE",
        container="Matroska",
        duration_seconds=7200.0,
        overall_bitrate=8_000_000,
        media_info_error=None,
        media_info_probed_at="2026-07-21T00:00:00",
        video_track_count=1,
        audio_track_count=1,
        subtitle_track_count=1,
        first_seen_scan_id=1,
        last_seen_scan_id=1,
        missing_since_scan_id=None,
    )
    defaults.update(overrides)
    return MediaFileRecord(**defaults)  # type: ignore[arg-type]


# --- missing_file ------------------------------------------------------------


def test_missing_file_triggers_on_missing_state() -> None:
    record = _record(state="MISSING", missing_since_scan_id=5, last_seen_scan_id=4)
    candidate = findings._rule_missing_file(record)
    assert candidate is not None
    assert candidate.rule_code == "missing_file"
    assert candidate.severity == findings.Severity.ERROR
    assert candidate.media_file_id == record.id
    assert candidate.library_id == record.library_id
    assert candidate.evidence == {"last_seen_scan_id": 4, "missing_since_scan_id": 5}


def test_missing_file_does_not_trigger_on_active_state() -> None:
    record = _record(state="ACTIVE")
    assert findings._rule_missing_file(record) is None


# --- metadata_error ------------------------------------------------------------


def test_metadata_error_triggers_when_error_recorded() -> None:
    record = _record(media_info_error="ffprobe failed")
    candidate = findings._rule_metadata_error(record)
    assert candidate is not None
    assert candidate.rule_code == "metadata_error"
    assert candidate.severity == findings.Severity.ERROR
    assert candidate.evidence == {"error": "ffprobe failed"}


def test_metadata_error_does_not_trigger_without_error() -> None:
    record = _record(media_info_error=None)
    assert findings._rule_metadata_error(record) is None


def test_metadata_error_does_not_trigger_on_missing_file() -> None:
    # A MISSING file's stale media_info_error (from before it went missing)
    # must not also produce a metadata_error finding -- missing_file alone
    # covers it.
    record = _record(state="MISSING", media_info_error="stale error")
    assert findings._rule_metadata_error(record) is None


# --- metadata_not_probed -------------------------------------------------------


def test_metadata_not_probed_triggers_when_never_probed() -> None:
    record = _record(media_info_probed_at=None, media_info_error=None)
    candidate = findings._rule_metadata_not_probed(record)
    assert candidate is not None
    assert candidate.rule_code == "metadata_not_probed"
    assert candidate.severity == findings.Severity.WARNING
    assert candidate.evidence == {}


def test_metadata_not_probed_does_not_trigger_once_probed() -> None:
    record = _record(media_info_probed_at="2026-07-21T00:00:00", media_info_error=None)
    assert findings._rule_metadata_not_probed(record) is None


def test_metadata_not_probed_is_distinct_from_metadata_error() -> None:
    """A probe that ran and failed is metadata_error, not metadata_not_probed --
    the two rules must never both fire for the same record."""
    record = _record(media_info_probed_at="2026-07-21T00:00:00", media_info_error="boom")
    assert findings._rule_metadata_not_probed(record) is None
    assert findings._rule_metadata_error(record) is not None


# --- unknown_layout ------------------------------------------------------------


def test_unknown_layout_triggers_on_unknown_layout() -> None:
    record = _record(layout="unknown")
    candidate = findings._rule_unknown_layout(record)
    assert candidate is not None
    assert candidate.rule_code == "unknown_layout"
    assert candidate.severity == findings.Severity.WARNING
    assert candidate.evidence == {"layout": "unknown"}


def test_unknown_layout_does_not_trigger_on_recognized_layout() -> None:
    record = _record(layout="movie_flat")
    assert findings._rule_unknown_layout(record) is None


# --- zero_byte_file ------------------------------------------------------------


def test_zero_byte_file_triggers_on_zero_size() -> None:
    record = _record(size_bytes=0)
    candidate = findings._rule_zero_byte_file(record)
    assert candidate is not None
    assert candidate.rule_code == "zero_byte_file"
    assert candidate.severity == findings.Severity.ERROR
    assert candidate.evidence == {"size_bytes": 0}


def test_zero_byte_file_does_not_trigger_on_nonzero_size() -> None:
    record = _record(size_bytes=1)
    assert findings._rule_zero_byte_file(record) is None


# --- suspiciously_small_media ---------------------------------------------------


def test_suspiciously_small_media_triggers_below_threshold() -> None:
    record = _record(size_bytes=findings.SUSPICIOUSLY_SMALL_MEDIA_THRESHOLD_BYTES - 1)
    candidate = findings._rule_suspiciously_small_media(record)
    assert candidate is not None
    assert candidate.rule_code == "suspiciously_small_media"
    assert candidate.severity == findings.Severity.WARNING
    assert candidate.evidence == {
        "size_bytes": findings.SUSPICIOUSLY_SMALL_MEDIA_THRESHOLD_BYTES - 1,
        "threshold_bytes": findings.SUSPICIOUSLY_SMALL_MEDIA_THRESHOLD_BYTES,
    }
    assert "corrupt" not in candidate.summary.lower()


def test_suspiciously_small_media_boundary_at_threshold_does_not_trigger() -> None:
    record = _record(size_bytes=findings.SUSPICIOUSLY_SMALL_MEDIA_THRESHOLD_BYTES)
    assert findings._rule_suspiciously_small_media(record) is None


def test_suspiciously_small_media_does_not_trigger_above_threshold() -> None:
    record = _record(size_bytes=findings.SUSPICIOUSLY_SMALL_MEDIA_THRESHOLD_BYTES + 1)
    assert findings._rule_suspiciously_small_media(record) is None


def test_suspiciously_small_media_does_not_trigger_on_zero_bytes() -> None:
    # zero_byte_file covers this case exclusively -- no double reporting.
    record = _record(size_bytes=0)
    assert findings._rule_suspiciously_small_media(record) is None


def test_suspiciously_small_media_ignores_unsupported_extension() -> None:
    record = _record(size_bytes=1024, extension=".txt")
    assert findings._rule_suspiciously_small_media(record) is None


# --- no_video_track / no_audio_track --------------------------------------------


def test_no_video_track_triggers_after_successful_probe_with_zero_tracks() -> None:
    record = _record(video_track_count=0)
    candidate = findings._rule_no_video_track(record)
    assert candidate is not None
    assert candidate.rule_code == "no_video_track"
    assert candidate.severity == findings.Severity.ERROR
    assert candidate.evidence == {"video_track_count": 0}


def test_no_video_track_does_not_trigger_with_a_video_track() -> None:
    record = _record(video_track_count=1)
    assert findings._rule_no_video_track(record) is None


def test_no_video_track_does_not_trigger_before_a_successful_probe() -> None:
    record = _record(video_track_count=0, media_info_probed_at=None, media_info_error=None)
    assert findings._rule_no_video_track(record) is None


def test_no_video_track_does_not_trigger_after_a_failed_probe() -> None:
    record = _record(video_track_count=0, media_info_error="boom")
    assert findings._rule_no_video_track(record) is None


def test_no_audio_track_triggers_after_successful_probe_with_zero_tracks() -> None:
    record = _record(audio_track_count=0)
    candidate = findings._rule_no_audio_track(record)
    assert candidate is not None
    assert candidate.rule_code == "no_audio_track"
    assert candidate.severity == findings.Severity.WARNING
    assert candidate.evidence == {"audio_track_count": 0}


def test_no_audio_track_does_not_trigger_with_an_audio_track() -> None:
    record = _record(audio_track_count=1)
    assert findings._rule_no_audio_track(record) is None


def test_no_audio_track_does_not_trigger_before_a_successful_probe() -> None:
    record = _record(audio_track_count=0, media_info_probed_at=None, media_info_error=None)
    assert findings._rule_no_audio_track(record) is None


def test_no_audio_track_does_not_trigger_after_a_failed_probe() -> None:
    record = _record(audio_track_count=0, media_info_error="boom")
    assert findings._rule_no_audio_track(record) is None


# --- unexpected_extension --------------------------------------------------------


def test_unexpected_extension_triggers_outside_supported_set() -> None:
    record = _record(extension=".rmvb")
    candidate = findings._rule_unexpected_extension(record)
    assert candidate is not None
    assert candidate.rule_code == "unexpected_extension"
    assert candidate.severity == findings.Severity.WARNING
    assert candidate.evidence["extension"] == ".rmvb"


def test_unexpected_extension_does_not_trigger_for_supported_extension() -> None:
    record = _record(extension=".mkv")
    assert findings._rule_unexpected_extension(record) is None


# --- state scoping across all non-missing_file rules ----------------------------


def test_no_rule_but_missing_file_triggers_on_a_missing_record() -> None:
    record = _record(
        state="MISSING",
        media_info_error="stale",
        layout="unknown",
        size_bytes=0,
        video_track_count=0,
        audio_track_count=0,
        extension=".rmvb",
    )
    candidates = findings.evaluate_all([record])
    assert [c.rule_code for c in candidates] == ["missing_file"]


# --- evaluate_all determinism ----------------------------------------------------


def test_evaluate_all_is_deterministically_ordered() -> None:
    broken = _record(id=2, size_bytes=0, video_track_count=0, audio_track_count=0)
    record = _record(id=1, layout="unknown")
    candidates = findings.evaluate_all([broken, record])
    keys = [(c.rule_code, c.media_file_id) for c in candidates]
    assert keys == sorted(keys)


def test_evaluate_all_is_stable_across_independent_calls() -> None:
    record = _record(size_bytes=0, video_track_count=0, audio_track_count=0, layout="unknown")
    first = findings.evaluate_all([record])
    second = findings.evaluate_all([record])
    assert first == second


def test_evaluate_all_produces_no_candidates_for_a_clean_file() -> None:
    record = _record()
    assert findings.evaluate_all([record]) == []
