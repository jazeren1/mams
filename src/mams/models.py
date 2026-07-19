from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum

class AssetStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    RIPPING = "RIPPING"
    RIPPED = "RIPPED"
    IDENTIFIED = "IDENTIFIED"
    VERIFIED = "VERIFIED"
    READY_TO_COPY = "READY_TO_COPY"
    COPYING = "COPYING"
    COPIED = "COPIED"
    DESTINATION_VERIFIED = "DESTINATION_VERIFIED"
    PLEX_SCAN_REQUESTED = "PLEX_SCAN_REQUESTED"
    PLEX_VERIFIED = "PLEX_VERIFIED"
    COMPLETE = "COMPLETE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"

@dataclass(frozen=True)
class MovieIdentity:
    title: str
    year: int
    @property
    def canonical_stem(self) -> str:
        return f"{self.title} ({self.year})"

@dataclass(frozen=True)
class EpisodeIdentity:
    series: str
    season: int
    episode: int
    episode_title: str | None = None
    @property
    def canonical_stem(self) -> str:
        base = f"{self.series} - S{self.season:02d}E{self.episode:02d}"
        return f"{base} - {self.episode_title}" if self.episode_title else base
