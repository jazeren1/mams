"""Pure, DB-unaware local parsing domain.

Converts canonical inventory fields (path, filename, category, layout)
into structured `IdentificationCandidate` objects -- deterministic local
interpretations of evidence, never confirmed media identities. Nothing in
this module calls TMDb, TVDB, Plex, or any other external service, touches
sqlite, or touches the filesystem; it is a pure function of the plain
values on `IdentificationInput`. See docs/DATABASE.md
("identification_candidates") for the persisted schema this domain object
maps onto.

`identification_repository.py` owns all identification-related SQL;
`identification_service.py` wires this module's `evaluate_file()` to
persistence -- neither this module depends on the other, matching the
findings.py / findings_repository.py / findings_service.py split.

## Determinism

Every function here is a pure function of its input: the same
`IdentificationInput` always produces byte-for-byte the same
`IdentificationCandidate` (including `evidence`), independent of call
order or any global state -- required for
`identification_repository.reconcile_candidates()` to avoid spurious
`updated_at` churn on a repeated evaluation of unchanged inventory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# Bumped whenever parsing logic changes meaningfully. Every `mams identify
# evaluate` run re-parses every ACTIVE file unconditionally (see
# identification_service.py), so a version bump takes effect on the very
# next run without any special re-evaluation trigger; the stored value is
# purely an audit trail on each row ("which parser produced this"). See
# docs/DATABASE.md.
PARSER_VERSION = 1


class CandidateType(StrEnum):
    MOVIE = "MOVIE"
    EPISODE = "EPISODE"
    SPECIAL = "SPECIAL"
    EXTRA = "EXTRA"
    UNKNOWN = "UNKNOWN"


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class IdentificationInput:
    """Canonical inventory fields a parse needs -- nothing DB-specific.

    Deliberately narrower than `inventory_repository.MediaFileRecord`: a
    parser has no business seeing size, tracks, or scan bookkeeping, only
    the path/name/classification evidence it actually parses.
    """

    media_file_id: int
    category: str
    layout: str
    filename: str
    relative_path: str
    parent_directory: str
    extension: str


@dataclass(frozen=True)
class IdentificationCandidate:
    """One file's current local interpretation.

    Mirrors the `identification_candidates` schema field-for-field, except
    `evidence` (maps onto `evidence_json`) and `episode_numbers` (maps onto
    `episode_numbers_json`) which `identification_repository.py`
    serializes -- this module only ever produces plain Python values.
    """

    media_file_id: int
    candidate_type: CandidateType
    confidence: Confidence
    parser_version: int
    evidence: dict[str, object]
    parsed_title: str | None = None
    parsed_year: int | None = None
    parsed_series_title: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    episode_numbers: tuple[int, ...] = ()
    episode_title: str | None = None
    edition: str | None = None
    part_number: int | None = None
    special_type: str | None = None


# --- shared parsing utilities ------------------------------------------------
#
# Used by both the movie parser and the television parser to strip release-
# name noise from a raw filename/directory segment down to a clean title.

# Recognized, conservatively -- removed from the clean parsed title but
# retained verbatim (as actually matched) in evidence["removed_tokens"].
# Not an attempt at exhaustive release-name parsing (see docs/AUTOMATION-
# ROADMAP.md's scope for this milestone).
TECHNICAL_TOKENS: tuple[str, ...] = (
    "2160p",
    "1080p",
    "720p",
    "480p",
    "4K",
    "BluRay",
    "BRRip",
    "WEB-DL",
    "WEBRip",
    "HDTV",
    "DVD",
    "Dolby Vision",
    "DV",
    "HDR",
    "x264",
    "x265",
    "HEVC",
    "AVC",
    "TrueHD",
    "Atmos",
    "DTS",
    "AAC",
    "Remux",
)

_TECHNICAL_TOKEN_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(token) for token in TECHNICAL_TOKENS) + r")\b",
    re.IGNORECASE,
)

# Canonical edition label -> matched text (lowercased) recognized for it.
# Deliberately conservative: exact known phrases only, not a fuzzy match.
_EDITION_CANONICAL: dict[str, str] = {
    "director's cut": "Director's Cut",
    "directors cut": "Director's Cut",
    "extended edition": "Extended Edition",
    "final cut": "Final Cut",
    "theatrical cut": "Theatrical Cut",
    "unrated": "Unrated",
    "special edition": "Special Edition",
    "collector's edition": "Collector's Edition",
    "collectors edition": "Collector's Edition",
    "remastered": "Remastered",
}

_EDITION_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(label) for label in _EDITION_CANONICAL) + r")\b",
    re.IGNORECASE,
)

# "Part 1"/"Part One"-style ordinals aren't handled -- only clear numeric
# part/disc markers, per the milestone's "recognize where clear" scope.
_PART_PATTERN = re.compile(
    r"\b(?:part|pt)\.?\s*(\d{1,2})\b|\bcd\s*(\d{1,2})\b|\bdisc\s*(\d{1,2})\b",
    re.IGNORECASE,
)

# A year token is exactly four digits starting with 19 or 20 (1900-2099).
# This single constraint is what "use a plausible year range and avoid
# interpreting arbitrary four-digit values as years" resolves to in
# practice: it excludes every technical token that happens to be
# four-digit-shaped (2160p, most bitrate/resolution values) for free,
# without a separate range check.
_PAREN_YEAR = re.compile(r"\(((?:19|20)\d{2})\)")
_BRACKET_YEAR = re.compile(r"\[((?:19|20)\d{2})\]")
_BARE_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def _strip_extension(filename: str) -> str:
    return Path(filename).stem


def _clean_separators(text: str) -> str:
    """Collapse dot/underscore-heavy release-name separators and repeated
    whitespace into single spaces, trimming stray separator/dash debris
    left at either end after a token was removed from the middle."""
    collapsed = re.sub(r"[._]+", " ", text)
    collapsed = re.sub(r"\s+", " ", collapsed)
    return collapsed.strip(" -")


def _extract_technical_tokens(text: str) -> tuple[str, list[str]]:
    """Remove recognized technical tokens from `text`.

    Returns (remainder, matched_tokens) with matched tokens in the order
    they appeared, in their original casing (for evidence).
    """
    matched: list[str] = []

    def _record(match: re.Match[str]) -> str:
        matched.append(match.group(0))
        return " "

    remainder = _TECHNICAL_TOKEN_PATTERN.sub(_record, text)
    return remainder, matched


def _extract_edition(text: str) -> tuple[str, str | None]:
    match = _EDITION_PATTERN.search(text)
    if match is None:
        return text, None
    canonical = _EDITION_CANONICAL[match.group(0).lower()]
    remainder = text[: match.start()] + " " + text[match.end() :]
    return remainder, canonical


def _extract_part_number(text: str) -> tuple[str, int | None]:
    match = _PART_PATTERN.search(text)
    if match is None:
        return text, None
    number = int(next(group for group in match.groups() if group is not None))
    remainder = text[: match.start()] + " " + text[match.end() :]
    return remainder, number


def _extract_year(text: str) -> tuple[str, int | None, str | None]:
    """Prefer a parenthesized year, then a bracketed year, then a bare
    token. Returns (remainder, year, the exact substring matched)."""
    for pattern in (_PAREN_YEAR, _BRACKET_YEAR, _BARE_YEAR):
        match = pattern.search(text)
        if match is not None:
            remainder = text[: match.start()] + " " + text[match.end() :]
            return remainder, int(match.group(1)), match.group(0)
    return text, None, None


@dataclass(frozen=True)
class _CleanedRelease:
    """Result of stripping year/edition/part/technical-token noise from a
    raw filename or directory segment down to a clean title."""

    title: str
    year: int | None
    year_match: str | None
    edition: str | None
    part_number: int | None
    removed_tokens: tuple[str, ...]


def _clean_release_text(raw: str) -> _CleanedRelease:
    text = _clean_separators(raw)
    text, year, year_match = _extract_year(text)
    text, edition = _extract_edition(text)
    text, part_number = _extract_part_number(text)
    text, tokens = _extract_technical_tokens(text)
    title = _clean_separators(text)
    return _CleanedRelease(
        title=title,
        year=year,
        year_match=year_match,
        edition=edition,
        part_number=part_number,
        removed_tokens=tuple(tokens),
    )
