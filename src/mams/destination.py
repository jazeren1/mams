"""Deterministic canonical destination naming (Milestone 7B, Phase I).

Pure module: no SQL, no filesystem access (never creates a directory or
checks whether one exists -- that is Phase J's read-only collision check,
done separately in `ingest_service.py`). Given a confirmed external
identity's fields, computes the exact destination directory/filename per
`docs/NAMING-STANDARDS.md`:

    Movies/Title (Year)/Title (Year).ext
    TV/Series Title/Season 01/Series Title - S01E02 - Episode Title.ext

`ingest_service.py` is the only caller; it decides *which* destination
category/root applies (movies vs kids_movies, tv vs kids_shows) before
calling into this module -- this module only ever names a path within an
already-chosen root.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

from .inventory import SUPPORTED_EXTENSIONS

_RESERVED_WINDOWS_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)


class DestinationError(Exception):
    """Raised when a safe destination path cannot be constructed --
    missing required identity fields, an unsupported extension, or a
    path component that sanitizes to nothing/a reserved value."""


@dataclass(frozen=True)
class DestinationPlan:
    library_root: str
    directory: str
    filename: str
    full_path: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def sanitize_path_component(raw: str) -> str:
    """Normalize and sanitize one path segment (a directory or filename
    stem, without extension) for safe use on the destination filesystem.

    - Unicode-normalizes to NFC (a single, deterministic form regardless
      of how the source string was composed).
    - Replaces path separators so a component can never introduce an
      extra directory level or escape its intended directory.
    - Strips non-printable/control characters.
    - Collapses internal whitespace and trims trailing dots/spaces
      (both are silently stripped by Windows/SMB and can otherwise
      produce a name that doesn't round-trip).
    - Rejects a component that sanitizes to empty, `.`, or `..`
      (path-traversal / no-op segments) by raising `DestinationError`.
    - Prefixes an underscore onto a reserved Windows device name or a
      component that would otherwise start with `.` (which would create
      a hidden file on POSIX filesystems).
    """
    text = unicodedata.normalize("NFC", raw)
    text = text.replace("/", "-").replace("\\", "-")
    text = "".join(ch for ch in text if ch.isprintable())
    text = re.sub(r"\s+", " ", text).strip()
    text = text.rstrip(". ")
    if not text or text in (".", ".."):
        raise DestinationError(f"path component sanitizes to empty or reserved value: {raw!r}")
    if text.upper() in _RESERVED_WINDOWS_NAMES:
        text = f"_{text}"
    if text.startswith("."):
        text = f"_{text}"
    return text


def _validate_extension(extension: str) -> str:
    ext = extension.lower()
    if not ext.startswith("."):
        ext = f".{ext}"
    if ext not in SUPPORTED_EXTENSIONS:
        raise DestinationError(f"unsupported extension for a destination path: {extension!r}")
    return ext


def movie_destination(
    *,
    title: str,
    year: int,
    extension: str,
    destination_root: str,
    edition: str | None = None,
) -> DestinationPlan:
    """`Movies/Title (Year)/Title (Year)[ - Edition].ext`. `edition` is
    only ever the *confirmed* edition a human has reviewed -- never a
    locally-parsed guess (see docs/NAMING-STANDARDS.md and Phase I)."""
    if not title.strip():
        raise DestinationError("movie destination requires a non-empty title")
    if year <= 0:
        raise DestinationError("movie destination requires a positive release year")
    ext = _validate_extension(extension)

    folder_name = sanitize_path_component(f"{title} ({year})")
    stem = f"{title} ({year})"
    if edition:
        stem = f"{stem} - {edition}"
    file_stem = sanitize_path_component(stem)

    directory = str(PurePosixPath(destination_root) / folder_name)
    filename = f"{file_stem}{ext}"
    return DestinationPlan(
        library_root=destination_root, directory=directory, filename=filename,
        full_path=str(PurePosixPath(directory) / filename),
    )


def _episode_tag(season_number: int, episode_numbers: tuple[int, ...]) -> str:
    if len(episode_numbers) == 1:
        return f"S{season_number:02d}E{episode_numbers[0]:02d}"
    return f"S{season_number:02d}E{episode_numbers[0]:02d}-E{episode_numbers[-1]:02d}"


def episode_destination(
    *,
    series_title: str,
    season_number: int,
    episode_numbers: tuple[int, ...],
    extension: str,
    destination_root: str,
    episode_title: str | None = None,
) -> DestinationPlan:
    """`TV/Series Title/Season NN/Series Title - SNNEnn[-Enn][ - Episode Title].ext`.
    `season_number=0` and a single episode number produces the documented
    special-episode layout (`Season 00/Series Title - S00E01 - ...`) --
    no separate code path is needed since it's the same pattern with
    season 0. `episode_numbers` with more than one entry produces the
    documented multi-episode tag (`S01E02-E03`)."""
    if not series_title.strip():
        raise DestinationError("episode destination requires a non-empty series title")
    if not episode_numbers:
        raise DestinationError("episode destination requires at least one episode number")
    ext = _validate_extension(extension)

    safe_series = sanitize_path_component(series_title)
    season_folder = f"Season {season_number:02d}"
    tag = _episode_tag(season_number, episode_numbers)
    stem = f"{series_title} - {tag}"
    if episode_title:
        stem = f"{stem} - {episode_title}"
    file_stem = sanitize_path_component(stem)

    directory = str(PurePosixPath(destination_root) / safe_series / season_folder)
    filename = f"{file_stem}{ext}"
    return DestinationPlan(
        library_root=destination_root, directory=directory, filename=filename,
        full_path=str(PurePosixPath(directory) / filename),
    )
