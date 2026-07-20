from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from mams.mediainfo import (
    MediaInfoOutcome,
    MediaInfoProvider,
    render_media_info,
)


def _general_track(**overrides: Any) -> dict[str, Any]:
    track = {
        "@type": "General",
        "Format": "Matroska",
        "Duration": "7384.192",
        "OverallBitRate": "10500000",
    }
    track.update(overrides)
    return track


def _video_track(**overrides: Any) -> dict[str, Any]:
    track = {
        "@type": "Video",
        "Format": "AVC",
        "Width": "1920",
        "Height": "1080",
        "DisplayAspectRatio/String": "16:9",
        "FrameRate": "23.976",
        "HDR_Format": "",
        "BitDepth": "8",
        "ScanType": "Progressive",
    }
    track.update(overrides)
    return track


def _audio_track(**overrides: Any) -> dict[str, Any]:
    track = {
        "@type": "Audio",
        "Format": "DTS",
        "Language": "en",
        "Channels": "6",
        "BitRate": "1509000",
        "Default": "Yes",
    }
    track.update(overrides)
    return track


def _subtitle_track(**overrides: Any) -> dict[str, Any]:
    track = {
        "@type": "Text",
        "Language": "en",
        "Default": "Yes",
        "Forced": "No",
    }
    track.update(overrides)
    return track


def _payload(tracks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"media": {"@ref": "/some/file.mkv", "track": tracks}}


class _FakeCompletedProcess:
    """Mimics the subset of subprocess.CompletedProcess the provider reads.

    Real `subprocess.run` in the fixed implementation writes to the caller's
    stdout file (not a pipe) and only returns `.returncode` / `.stderr`
    (bytes, since the provider no longer passes text=True). `.stdout` on the
    real CompletedProcess is None because stdout was redirected to a file
    object rather than PIPE.
    """

    def __init__(self, returncode: int, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = None
        self.stderr = stderr


def _provider_with_fake_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout_bytes: bytes = b"",
    returncode: int = 0,
    stderr: bytes = b"",
    record_stdout_kwarg: list[object] | None = None,
) -> MediaInfoProvider:
    """Patch subprocess.run to behave like the real stdout-to-file contract.

    Writes `stdout_bytes` into whatever file object is passed as the
    `stdout=` kwarg (exactly what the real mediainfo process would do when
    its stdout fd points at that file), and returns a fake CompletedProcess
    exposing bytes `stderr` and no `.stdout` — matching the fixed provider,
    which never passes stdout=PIPE / capture_output=True.
    """
    provider = MediaInfoProvider(executable="/usr/bin/mediainfo")

    def _fake_run(*args: Any, **kwargs: Any) -> _FakeCompletedProcess:
        if record_stdout_kwarg is not None:
            record_stdout_kwarg.append(kwargs.get("stdout"))
        assert kwargs.get("stdout") is not subprocess.PIPE, "stdout must not be PIPE"
        assert "capture_output" not in kwargs, "capture_output must not be used"
        stdout_file: BinaryIO = kwargs["stdout"]
        stdout_file.write(stdout_bytes)
        stdout_file.flush()
        return _FakeCompletedProcess(returncode=returncode, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return provider


def test_stdout_is_not_pipe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Regression test for the ~54s stall observed with stdout=PIPE on macOS.

    Asserts subprocess.run is invoked with a real (seekable) file object as
    stdout rather than subprocess.PIPE, and without capture_output=True.
    """
    seen_stdout: list[object] = []
    tracks = [_general_track()]
    provider = _provider_with_fake_run(
        monkeypatch, stdout_bytes=json.dumps(_payload(tracks)).encode("utf-8"), record_stdout_kwarg=seen_stdout
    )

    provider.probe(tmp_path / "movie.mkv")

    assert len(seen_stdout) == 1
    stdout_arg = seen_stdout[0]
    assert stdout_arg is not subprocess.PIPE
    assert hasattr(stdout_arg, "fileno")
    assert hasattr(stdout_arg, "seek")


def test_successful_extraction(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tracks = [_general_track(), _video_track(), _audio_track()]
    stdout_bytes = json.dumps(_payload(tracks)).encode("utf-8")
    provider = _provider_with_fake_run(monkeypatch, stdout_bytes=stdout_bytes, returncode=0)

    outcome = provider.probe(tmp_path / "movie.mkv")

    assert outcome.ok is True
    assert outcome.error is None
    assert outcome.media_info is not None
    assert outcome.media_info.container == "Matroska"
    assert outcome.media_info.duration_seconds == pytest.approx(7384.192)
    assert outcome.media_info.overall_bitrate == 10500000
    assert len(outcome.media_info.video_tracks) == 1
    assert outcome.media_info.video_tracks[0].codec == "AVC"
    assert outcome.media_info.video_tracks[0].width == 1920
    assert outcome.media_info.video_tracks[0].height == 1080
    assert len(outcome.media_info.audio_tracks) == 1
    assert outcome.media_info.audio_tracks[0].language == "en"


def test_successful_json_parsing_reads_from_temp_file_after_seek(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Confirms the provider seeks back to 0 and reads the temp file's
    full contents rather than relying on CompletedProcess.stdout (which is
    None when stdout is redirected to a file instead of a pipe)."""
    tracks = [_general_track(Format="AVCHD"), _video_track()]
    stdout_bytes = json.dumps(_payload(tracks)).encode("utf-8")
    provider = _provider_with_fake_run(monkeypatch, stdout_bytes=stdout_bytes, returncode=0)

    outcome = provider.probe(tmp_path / "movie.mkv")

    assert outcome.media_info is not None
    assert outcome.media_info.container == "AVCHD"


def test_missing_executable_is_reported_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    provider = MediaInfoProvider()

    outcome = provider.probe(tmp_path / "movie.mkv")

    assert outcome.ok is False
    assert outcome.media_info is None
    assert outcome.error is not None
    assert "not found" in outcome.error


def test_corrupt_file_nonzero_exit_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = _provider_with_fake_run(
        monkeypatch, stdout_bytes=b"", returncode=1, stderr=b"Corrupt file, cannot open"
    )

    outcome = provider.probe(tmp_path / "corrupt.mkv")

    assert outcome.ok is False
    assert outcome.media_info is None
    assert "Corrupt file" in (outcome.error or "")


def test_stderr_is_decoded_and_included_in_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider = _provider_with_fake_run(
        monkeypatch, stdout_bytes=b"", returncode=2, stderr=b"Unrecognized file format\n"
    )

    outcome = provider.probe(tmp_path / "movie.mkv")

    assert outcome.ok is False
    assert outcome.error is not None
    assert "status 2" in outcome.error
    assert "Unrecognized file format" in outcome.error


def test_stderr_present_but_success_does_not_prevent_parsing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """mediainfo can write warnings to stderr while still exiting 0; that
    must not affect stdout parsing."""
    tracks = [_general_track()]
    stdout_bytes = json.dumps(_payload(tracks)).encode("utf-8")
    provider = _provider_with_fake_run(
        monkeypatch, stdout_bytes=stdout_bytes, returncode=0, stderr=b"warning: deprecated codec hint\n"
    )

    outcome = provider.probe(tmp_path / "movie.mkv")

    assert outcome.ok is True
    assert outcome.media_info is not None
    assert outcome.media_info.container == "Matroska"


def test_malformed_json_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider = _provider_with_fake_run(monkeypatch, stdout_bytes=b"{not valid json", returncode=0)

    outcome = provider.probe(tmp_path / "movie.mkv")

    assert outcome.ok is False
    assert outcome.media_info is None
    assert outcome.error is not None


def test_unexpected_json_structure_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stdout_bytes = json.dumps({"unexpected": "shape"}).encode("utf-8")
    provider = _provider_with_fake_run(monkeypatch, stdout_bytes=stdout_bytes, returncode=0)

    outcome = provider.probe(tmp_path / "movie.mkv")

    assert outcome.ok is False
    assert outcome.media_info is None
    assert outcome.error is not None


def test_no_general_track_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stdout_bytes = json.dumps(_payload([_video_track()])).encode("utf-8")
    provider = _provider_with_fake_run(monkeypatch, stdout_bytes=stdout_bytes, returncode=0)

    outcome = provider.probe(tmp_path / "movie.mkv")

    assert outcome.ok is False
    assert outcome.media_info is None


def test_multiple_audio_tracks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tracks = [
        _general_track(),
        _audio_track(Language="en", Channels="6", Default="Yes"),
        _audio_track(Language="fr", Channels="2", Default="No"),
    ]
    stdout_bytes = json.dumps(_payload(tracks)).encode("utf-8")
    provider = _provider_with_fake_run(monkeypatch, stdout_bytes=stdout_bytes, returncode=0)

    outcome = provider.probe(tmp_path / "movie.mkv")

    assert outcome.media_info is not None
    assert len(outcome.media_info.audio_tracks) == 2
    languages = {t.language: t.default for t in outcome.media_info.audio_tracks}
    assert languages == {"en": True, "fr": False}


def test_multiple_subtitle_tracks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tracks = [
        _general_track(),
        _subtitle_track(Language="en", Default="Yes", Forced="No"),
        _subtitle_track(Language="en", Default="No", Forced="Yes"),
        _subtitle_track(Language="es", Default="No", Forced="No"),
    ]
    stdout_bytes = json.dumps(_payload(tracks)).encode("utf-8")
    provider = _provider_with_fake_run(monkeypatch, stdout_bytes=stdout_bytes, returncode=0)

    outcome = provider.probe(tmp_path / "movie.mkv")

    assert outcome.media_info is not None
    assert len(outcome.media_info.subtitle_tracks) == 3
    forced_flags = [t.forced for t in outcome.media_info.subtitle_tracks]
    assert forced_flags == [False, True, False]


def test_hdr_detection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tracks = [_general_track(), _video_track(HDR_Format="SMPTE ST 2086", BitDepth="10")]
    stdout_bytes = json.dumps(_payload(tracks)).encode("utf-8")
    provider = _provider_with_fake_run(monkeypatch, stdout_bytes=stdout_bytes, returncode=0)

    outcome = provider.probe(tmp_path / "movie.mkv")

    assert outcome.media_info is not None
    video = outcome.media_info.video_tracks[0]
    assert video.hdr_format == "SMPTE ST 2086"
    assert video.bit_depth == 10


def test_no_hdr_is_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tracks = [_general_track(), _video_track(HDR_Format="")]
    stdout_bytes = json.dumps(_payload(tracks)).encode("utf-8")
    provider = _provider_with_fake_run(monkeypatch, stdout_bytes=stdout_bytes, returncode=0)

    outcome = provider.probe(tmp_path / "movie.mkv")

    assert outcome.media_info is not None
    assert outcome.media_info.video_tracks[0].hdr_format is None


def test_missing_fields_default_to_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tracks = [{"@type": "General"}, {"@type": "Video"}, {"@type": "Audio"}, {"@type": "Text"}]
    stdout_bytes = json.dumps(_payload(tracks)).encode("utf-8")
    provider = _provider_with_fake_run(monkeypatch, stdout_bytes=stdout_bytes, returncode=0)

    outcome = provider.probe(tmp_path / "movie.mkv")

    assert outcome.media_info is not None
    assert outcome.media_info.container is None
    assert outcome.media_info.duration_seconds is None
    video = outcome.media_info.video_tracks[0]
    assert video.codec is None
    assert video.width is None
    audio = outcome.media_info.audio_tracks[0]
    assert audio.channels is None
    assert audio.default is False
    subtitle = outcome.media_info.subtitle_tracks[0]
    assert subtitle.language is None
    assert subtitle.default is False
    assert subtitle.forced is False


def test_timeout_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider = MediaInfoProvider(executable="/usr/bin/mediainfo")

    def _fake_run(*args: Any, **kwargs: Any) -> Any:
        assert kwargs.get("stdout") is not subprocess.PIPE
        raise subprocess.TimeoutExpired(cmd="mediainfo", timeout=60)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    outcome = provider.probe(tmp_path / "movie.mkv")

    assert outcome.ok is False
    assert outcome.error is not None
    assert "timed out" in outcome.error


def test_oserror_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider = MediaInfoProvider(executable="/usr/bin/mediainfo")

    def _fake_run(*args: Any, **kwargs: Any) -> Any:
        raise OSError("permission denied")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    outcome = provider.probe(tmp_path / "movie.mkv")

    assert outcome.ok is False
    assert outcome.error is not None
    assert "permission denied" in outcome.error


def test_malformed_utf8_in_stdout_falls_back_to_replacement_char(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Invalid UTF-8 bytes inside a JSON string value must not raise a
    UnicodeDecodeError; decoding falls back to the replacement character and
    parsing continues."""
    tracks = [_general_track(Format="MPEG-XX")]
    valid_json_bytes = json.dumps(_payload(tracks)).encode("utf-8")
    corrupted_bytes = valid_json_bytes.replace(b"MPEG-XX", b"MPEG-\xff\xfe")
    provider = _provider_with_fake_run(monkeypatch, stdout_bytes=corrupted_bytes, returncode=0)

    outcome = provider.probe(tmp_path / "movie.mkv")

    assert outcome.ok is True
    assert outcome.media_info is not None
    assert outcome.media_info.container is not None
    assert outcome.media_info.container.startswith("MPEG-")
    assert "�" in outcome.media_info.container


def test_malformed_utf8_in_stderr_does_not_raise(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider = _provider_with_fake_run(
        monkeypatch, stdout_bytes=b"", returncode=1, stderr=b"bad input \xff\xfe here"
    )

    outcome = provider.probe(tmp_path / "movie.mkv")

    assert outcome.ok is False
    assert outcome.error is not None
    assert "�" in outcome.error


def test_render_media_info_includes_key_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tracks = [_general_track(), _video_track(), _audio_track(), _subtitle_track()]
    stdout_bytes = json.dumps(_payload(tracks)).encode("utf-8")
    provider = _provider_with_fake_run(monkeypatch, stdout_bytes=stdout_bytes, returncode=0)

    outcome = provider.probe(tmp_path / "movie.mkv")
    assert outcome.media_info is not None

    text = render_media_info(outcome.media_info)

    assert "Matroska" in text
    assert "AVC" in text
    assert "1920x1080" in text
    assert "DTS" in text


def test_media_info_to_dict_round_trips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tracks = [_general_track(), _video_track(), _audio_track(), _subtitle_track()]
    stdout_bytes = json.dumps(_payload(tracks)).encode("utf-8")
    provider = _provider_with_fake_run(monkeypatch, stdout_bytes=stdout_bytes, returncode=0)

    outcome = provider.probe(tmp_path / "movie.mkv")
    assert outcome.media_info is not None

    payload = json.loads(outcome.media_info.to_json())

    assert payload["container"] == "Matroska"
    assert payload["video_tracks"][0]["codec"] == "AVC"
    assert payload["audio_tracks"][0]["codec"] == "DTS"
    assert payload["subtitle_tracks"][0]["language"] == "en"


def test_outcome_ok_property() -> None:
    ok_outcome = MediaInfoOutcome(media_info=None, error=None)
    err_outcome = MediaInfoOutcome(media_info=None, error="boom")

    assert ok_outcome.ok is True
    assert err_outcome.ok is False
