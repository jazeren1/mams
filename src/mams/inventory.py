"""Read-only media inventory scanner.

This module discovers media files under configured NAS category paths and
produces structured, deterministic reports. It never renames, moves,
deletes, checksums, or otherwise modifies anything it discovers — it only
reads directory entries and file metadata (size).

The module has no dependency on the CLI or on `mams.config`; it operates on
plain `dict[str, str]` category-to-path mappings so it can be tested and
reused independently.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Iterator

# Extensions considered playable media. Comparison is case-insensitive.
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mkv",
        ".mp4",
        ".m4v",
        ".mov",
        ".avi",
        ".mpg",
        ".mpeg",
        ".ts",
        ".m2ts",
        ".vob",
        ".wmv",
    }
)

# Categories expected to follow a movie-style layout: a flat file directly
# under the category root, or one folder per movie.
MOVIE_CATEGORIES: frozenset[str] = frozenset({"movies", "kids_movies"})

# Categories expected to follow a TV-style layout: a series folder,
# optionally containing season subfolders.
TV_CATEGORIES: frozenset[str] = frozenset({"tv", "kids_shows", "fitness"})

# Directories produced by macOS Finder or QNAP NAS indexing that never hold
# user media. These are skipped entirely and never descended into.
_IGNORED_DIR_NAMES: frozenset[str] = frozenset(
    {
        "__MACOSX",
        "@eaDir",
        "@Recycle",
        "#recycle",
        ".@__thumb",
        ".Trashes",
        ".Trash-1000",
    }
)

_SEASON_DIR_PATTERN = re.compile(r"^season[\s_-]*\d+$", re.IGNORECASE)


class Layout(StrEnum):
    """Detected on-disk organization for a discovered media file."""

    MOVIE_FLAT = "movie_flat"
    MOVIE_FOLDER = "movie_folder"
    MOVIE_COLLECTION_FOLDER = "movie_collection_folder"
    TV_SERIES_FOLDER = "tv_series_folder"
    TV_SEASON_FOLDER = "tv_season_folder"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ScannedFile:
    """A single discovered media file and its metadata."""

    category: str
    absolute_path: str
    relative_path: str
    filename: str
    extension: str
    parent_directory: str
    size_bytes: int
    layout: Layout

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["layout"] = self.layout.value
        return data


@dataclass(frozen=True)
class CategoryScan:
    """Scan results for a single configured category."""

    category: str
    root_path: str
    exists: bool
    files: tuple[ScannedFile, ...]

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_size_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "root_path": self.root_path,
            "exists": self.exists,
            "file_count": self.file_count,
            "total_size_bytes": self.total_size_bytes,
            "files": [f.to_dict() for f in self.files],
        }


@dataclass(frozen=True)
class InventoryReport:
    """Aggregate scan results across all configured categories."""

    categories: tuple[CategoryScan, ...]

    @property
    def file_count(self) -> int:
        return sum(c.file_count for c in self.categories)

    @property
    def total_size_bytes(self) -> int:
        return sum(c.total_size_bytes for c in self.categories)

    def to_dict(self) -> dict[str, object]:
        return {
            "file_count": self.file_count,
            "total_size_bytes": self.total_size_bytes,
            "categories": [c.to_dict() for c in self.categories],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


def _is_ignored_file(name: str) -> bool:
    """True for hidden files, macOS metadata, and obvious temp/lock files."""
    if name.startswith("."):
        # Hidden files, including .DS_Store and AppleDouble "._*" siblings.
        return True
    if name.startswith("~"):
        # Editor/OS lock files such as ~$Movie.mkv or ~movie.mkv.
        return True
    return False


def _is_ignored_dir(name: str) -> bool:
    """True for hidden directories and known non-media metadata folders."""
    if name.startswith("."):
        return True
    return name in _IGNORED_DIR_NAMES


def detect_layout(category: str, relative_path: Path) -> Layout:
    """Classify how a discovered file is organized relative to its category root.

    Movie-style categories: depth 1 (file directly under root) is a flat
    file; depth 2 (one containing folder) is a folder-per-movie; depth 3
    (a collection/franchise folder containing a per-movie folder, e.g.
    `Star Wars/A New Hope/A New Hope_001.mp4`) is a collection folder.

    TV-style categories: depth 2 (series folder only) is a series-folder
    layout; depth 3 (series folder + season folder) is a season-folder
    layout.

    Anything deeper, or a category this module doesn't recognize, is
    reported as unknown rather than guessed at.
    """
    depth = len(relative_path.parts)
    if category in MOVIE_CATEGORIES:
        if depth == 1:
            return Layout.MOVIE_FLAT
        if depth == 2:
            return Layout.MOVIE_FOLDER
        if depth == 3:
            return Layout.MOVIE_COLLECTION_FOLDER
        return Layout.UNKNOWN
    if category in TV_CATEGORIES:
        if depth == 2:
            return Layout.TV_SERIES_FOLDER
        if depth == 3:
            return Layout.TV_SEASON_FOLDER
        return Layout.UNKNOWN
    return Layout.UNKNOWN


def _walk(root: Path) -> Iterator[Path]:
    """Yield files under root in deterministic (name-sorted) order.

    Ignored directories are skipped and never descended into.
    """
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.is_symlink():
            continue
        if entry.is_dir():
            if _is_ignored_dir(entry.name):
                continue
            yield from _walk(entry)
        elif entry.is_file():
            yield entry


def scan_category(category: str, root: str | Path) -> CategoryScan:
    """Recursively scan a single category root for supported media files.

    Never raises for a missing directory; returns a `CategoryScan` with
    `exists=False` and no files instead, so callers can report incomplete
    configuration without aborting the whole scan.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        return CategoryScan(category=category, root_path=str(root_path), exists=False, files=())

    discovered: list[ScannedFile] = []
    for path in _walk(root_path):
        if _is_ignored_file(path.name):
            continue
        extension = path.suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            continue
        relative = path.relative_to(root_path)
        size_bytes = path.stat().st_size
        discovered.append(
            ScannedFile(
                category=category,
                absolute_path=str(path.resolve()),
                relative_path=str(relative),
                filename=path.name,
                extension=extension,
                parent_directory=str(path.parent),
                size_bytes=size_bytes,
                layout=detect_layout(category, relative),
            )
        )

    # Sort for deterministic output regardless of filesystem iteration order.
    discovered.sort(key=lambda f: f.relative_path)
    return CategoryScan(category=category, root_path=str(root_path), exists=True, files=tuple(discovered))


def scan_categories(categories: dict[str, str]) -> InventoryReport:
    """Scan every configured category and return an aggregate report.

    Categories are scanned in the order given (dict insertion order), which
    matches the order categories appear in `config.yaml`.
    """
    scans = tuple(scan_category(category, root) for category, root in categories.items())
    return InventoryReport(categories=scans)


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def render_summary(report: InventoryReport) -> str:
    """Render a human-readable plain-text summary of an inventory report."""
    lines: list[str] = ["MAMS Inventory Summary", "=" * 23]
    for cat in report.categories:
        status = "OK" if cat.exists else "MISSING"
        lines.append("")
        lines.append(f"{cat.category} [{status}] - {cat.root_path}")
        if not cat.exists:
            lines.append("  (directory not found; skipped)")
            continue
        lines.append(f"  files: {cat.file_count}")
        lines.append(f"  size:  {_human_size(cat.total_size_bytes)}")
        layout_counts: dict[str, int] = {}
        for f in cat.files:
            layout_counts[f.layout.value] = layout_counts.get(f.layout.value, 0) + 1
        for layout_name in sorted(layout_counts):
            lines.append(f"    {layout_name}: {layout_counts[layout_name]}")
    lines.append("")
    lines.append(f"Total files: {report.file_count}")
    lines.append(f"Total size:  {_human_size(report.total_size_bytes)}")
    return "\n".join(lines)
