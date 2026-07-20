"""Read-only MediaInfo metadata extraction.

This module runs the `mediainfo` CLI tool against a media file, parses its
JSON output, and converts the result into typed dataclasses. It never
renames, moves, deletes, checksums, or otherwise modifies the file it
inspects — it only invokes a read-only external tool and reads its stdout.

The scanner talks to this module through the `MetadataProvider` protocol
rather than calling `MediaInfoProvider` directly, so a future metadata
provider (e.g. ffprobe) can be substituted without changing `inventory.py`.

Every discovered file is probed at most once per scan, since `inventory`
walks the filesystem once and calls `probe()` once per file it visits; this
module has no caching layer because none is needed for that access pattern.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

# Seconds to allow a single `mediainfo` invocation before giving up on it.
_PROBE_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class VideoTrack:
    """A single video stream reported by MediaInfo."""

    codec: str | None
    width: int | None
    height: int | None
    aspect_ratio: str | None
    frame_rate: float | None
    hdr_format: str | None
    bit_depth: int | None
    scan_type: str | None


@dataclass(frozen=True)
class AudioTrack:
    """A single audio stream reported by MediaInfo."""

    codec: str | None
    language: str | None
    channels: int | None
    bitrate: int | None
    default: bool


@dataclass(frozen=True)
class SubtitleTrack:
    """A single subtitle stream reported by MediaInfo."""

    language: str | None
    default: bool
    forced: bool


@dataclass(frozen=True)
class MediaInfo:
    """Technical metadata for one media file, parsed from MediaInfo JSON."""

    container: str | None
    duration_seconds: float | None
    overall_bitrate: int | None
    video_tracks: tuple[VideoTrack, ...]
    audio_tracks: tuple[AudioTrack, ...]
    subtitle_tracks: tuple[SubtitleTrack, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


@dataclass(frozen=True)
class MediaInfoOutcome:
    """Result of probing one file: either metadata or a recorded error.

    `probe()` never raises for expected failure modes (missing executable,
    a corrupt file, malformed JSON) so callers such as the inventory
    scanner can continue past a single bad file.
    """

    media_info: MediaInfo | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None


class MetadataProvider(Protocol):
    """Interface the inventory scanner uses to enrich a file with metadata."""

    def probe(self, path: Path) -> MediaInfoOutcome: ...


class MediaInfoProvider:
    """Extracts metadata by shelling out to the `mediainfo` executable."""

    def __init__(self, executable: str | Path | None = None) -> None:
        self._executable = str(executable) if executable else None

    def _resolve_executable(self) -> str | None:
        if self._executable:
            return self._executable
        return shutil.which("mediainfo")

    def probe(self, path: Path) -> MediaInfoOutcome:
        executable = self._resolve_executable()
        if executable is None:
            return MediaInfoOutcome(
                media_info=None,
                error="mediainfo executable not found on PATH",
            )

        # stdout is directed to a real (anonymous, unlinked) temp file rather
        # than a pipe. On this project's dev Mac, capturing mediainfo's
        # stdout through subprocess.PIPE (via capture_output=True) measured
        # ~54s for output that mediainfo itself produces in ~0.4s at the
        # terminal; writing to a temp file measured ~0.35s for the same
        # output. Do not change this back to a pipe without re-verifying
        # that regression is gone.
        try:
            with tempfile.TemporaryFile(mode="w+b") as stdout_file:
                result = subprocess.run(
                    [executable, "--Output=JSON", str(path)],
                    stdout=stdout_file,
                    stderr=subprocess.PIPE,
                    timeout=_PROBE_TIMEOUT_SECONDS,
                    check=False,
                )
                stdout_file.seek(0)
                stdout_bytes = stdout_file.read()
        except OSError as exc:
            return MediaInfoOutcome(media_info=None, error=f"failed to run mediainfo: {exc}")
        except subprocess.TimeoutExpired:
            return MediaInfoOutcome(
                media_info=None,
                error=f"mediainfo timed out after {_PROBE_TIMEOUT_SECONDS}s",
            )

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")

        if result.returncode != 0:
            stderr_stripped = stderr_text.strip()
            detail = f": {stderr_stripped}" if stderr_stripped else ""
            return MediaInfoOutcome(
                media_info=None,
                error=f"mediainfo exited with status {result.returncode}{detail}",
            )

        try:
            payload = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            return MediaInfoOutcome(media_info=None, error=f"malformed mediainfo JSON output: {exc}")

        try:
            media_info = _parse_media_info(payload)
        except (KeyError, TypeError, ValueError) as exc:
            return MediaInfoOutcome(media_info=None, error=f"unexpected mediainfo JSON structure: {exc}")

        return MediaInfoOutcome(media_info=media_info, error=None)


def _get_str(track: dict[str, object], key: str) -> str | None:
    value = track.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _get_int(track: dict[str, object], key: str) -> int | None:
    value = track.get(key)
    if value is None:
        return None
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _get_float(track: dict[str, object], key: str) -> float | None:
    value = track.get(key)
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _get_bool(track: dict[str, object], key: str) -> bool:
    value = track.get(key)
    if value is None:
        return False
    return str(value).strip().lower() == "yes"


def _parse_video_track(track: dict[str, object]) -> VideoTrack:
    return VideoTrack(
        codec=_get_str(track, "Format"),
        width=_get_int(track, "Width"),
        height=_get_int(track, "Height"),
        aspect_ratio=_get_str(track, "DisplayAspectRatio/String") or _get_str(track, "DisplayAspectRatio"),
        frame_rate=_get_float(track, "FrameRate"),
        hdr_format=_get_str(track, "HDR_Format"),
        bit_depth=_get_int(track, "BitDepth"),
        scan_type=_get_str(track, "ScanType"),
    )


def _parse_audio_track(track: dict[str, object]) -> AudioTrack:
    return AudioTrack(
        codec=_get_str(track, "Format"),
        language=_get_str(track, "Language"),
        channels=_get_int(track, "Channels"),
        bitrate=_get_int(track, "BitRate"),
        default=_get_bool(track, "Default"),
    )


def _parse_subtitle_track(track: dict[str, object]) -> SubtitleTrack:
    return SubtitleTrack(
        language=_get_str(track, "Language"),
        default=_get_bool(track, "Default"),
        forced=_get_bool(track, "Forced"),
    )


def _parse_media_info(payload: object) -> MediaInfo:
    if not isinstance(payload, dict):
        raise TypeError("mediainfo JSON root must be an object")

    media = payload["media"]
    if not isinstance(media, dict):
        raise TypeError("mediainfo JSON 'media' must be an object")

    tracks = media["track"]
    if isinstance(tracks, dict):
        tracks = [tracks]
    if not isinstance(tracks, list):
        raise TypeError("mediainfo JSON 'media.track' must be a list")

    general = next((t for t in tracks if isinstance(t, dict) and t.get("@type") == "General"), None)
    if general is None:
        raise ValueError("no General track present in mediainfo output")

    video_tracks = tuple(
        _parse_video_track(t) for t in tracks if isinstance(t, dict) and t.get("@type") == "Video"
    )
    audio_tracks = tuple(
        _parse_audio_track(t) for t in tracks if isinstance(t, dict) and t.get("@type") == "Audio"
    )
    subtitle_tracks = tuple(
        _parse_subtitle_track(t) for t in tracks if isinstance(t, dict) and t.get("@type") == "Text"
    )

    return MediaInfo(
        container=_get_str(general, "Format"),
        duration_seconds=_get_float(general, "Duration"),
        overall_bitrate=_get_int(general, "OverallBitRate"),
        video_tracks=video_tracks,
        audio_tracks=audio_tracks,
        subtitle_tracks=subtitle_tracks,
    )


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d} ({seconds:.1f}s)"


def _format_bitrate(bitrate: int | None) -> str:
    if bitrate is None:
        return "unknown"
    return f"{bitrate:,} bps"


def render_media_info(info: MediaInfo) -> str:
    """Render a human-readable plain-text summary of parsed MediaInfo."""
    lines: list[str] = []
    lines.append(f"Container:       {info.container or 'unknown'}")
    lines.append(f"Duration:        {_format_duration(info.duration_seconds)}")
    lines.append(f"Overall bitrate: {_format_bitrate(info.overall_bitrate)}")

    lines.append("")
    lines.append(f"Video tracks: {len(info.video_tracks)}")
    for i, video_track in enumerate(info.video_tracks, start=1):
        video_details: list[str | None] = [
            video_track.codec or "unknown codec",
            f"{video_track.width}x{video_track.height}" if video_track.width and video_track.height else None,
            video_track.aspect_ratio,
            f"{video_track.frame_rate:.3f} fps" if video_track.frame_rate else None,
            f"{video_track.bit_depth}-bit" if video_track.bit_depth else None,
            video_track.scan_type,
            f"HDR: {video_track.hdr_format}" if video_track.hdr_format else None,
        ]
        lines.append(f"  {i}. " + "  ".join(d for d in video_details if d is not None))

    lines.append("")
    lines.append(f"Audio tracks: {len(info.audio_tracks)}")
    for i, audio_track in enumerate(info.audio_tracks, start=1):
        audio_details: list[str | None] = [
            audio_track.codec or "unknown codec",
            audio_track.language,
            f"{audio_track.channels}ch" if audio_track.channels else None,
            f"{audio_track.bitrate:,} bps" if audio_track.bitrate else None,
            "default" if audio_track.default else None,
        ]
        lines.append(f"  {i}. " + "  ".join(d for d in audio_details if d is not None))

    lines.append("")
    lines.append(f"Subtitle tracks: {len(info.subtitle_tracks)}")
    for i, subtitle_track in enumerate(info.subtitle_tracks, start=1):
        flag_details: list[str | None] = [
            "default" if subtitle_track.default else None,
            "forced" if subtitle_track.forced else None,
        ]
        flags = "  ".join(f for f in flag_details if f is not None)
        suffix = f"  {flags}" if flags else ""
        lines.append(f"  {i}. {subtitle_track.language or 'unknown language'}{suffix}")

    return "\n".join(lines)
