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

from . import inventory

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


# --- movie parsing ------------------------------------------------------------


def _titles_similar(a: str, b: str) -> bool:
    """Conservative equivalence check for two candidate titles: exact match
    (case-insensitive) or one containing the other as a substring. Anything
    looser risks false negatives on genuinely different titles; anything
    stricter would flag "Alien" vs "alien" -- a mere casing difference --
    as conflicting evidence, which it isn't."""
    a_norm, b_norm = a.strip().lower(), b.strip().lower()
    if not a_norm or not b_norm:
        return True
    return a_norm == b_norm or a_norm in b_norm or b_norm in a_norm


def _is_ambiguous_title(title: str) -> bool:
    """True for a "title" too weak to mean anything -- a single character,
    or a bare number left over after year/token stripping (e.g. a filename
    that was only a technical release tag)."""
    stripped = title.strip()
    return len(stripped) < 2 or stripped.isdigit()


# Generic placeholder filenames that carry no title evidence of their own
# -- common when a rip is properly named at the folder level ("Alien
# (1979)/movie.mkv") but left generic at the file level. Treated as if the
# filename had no title at all, so folder evidence isn't spuriously
# flagged as "conflicting" with a word that was never really a title.
_GENERIC_FILENAME_TITLES = frozenset({"movie", "movies", "video", "film", "untitled", "download"})


def _has_real_title(cleaned: _CleanedRelease) -> bool:
    return bool(cleaned.title) and cleaned.title.strip().lower() not in _GENERIC_FILENAME_TITLES


def _parse_movie(input_data: IdentificationInput) -> IdentificationCandidate:
    """Conservative movie title/year parsing.

    Evidence preference order: filename title+year, parent directory
    title+year, filename title without year, parent directory title
    without year (this milestone's documented default order). Confidence
    reflects the strength of what was actually parsed -- never HIGH merely
    because the file lives in a movie category/layout. See "Confidence
    rubric" below.

    Confidence rubric:
    - HIGH: a title and year were both parsed, with no conflict between
      filename and parent-directory evidence.
    - MEDIUM: a title was parsed but no year was found anywhere.
    - LOW: filename and parent-directory evidence conflict (different
      years, or clearly different titles), or the parsed title is too
      weak to be meaningful (e.g. a bare number).
    - UNKNOWN: no usable title could be parsed from either the filename or
      the parent directory.
    """
    filename_clean = _clean_release_text(_strip_extension(input_data.filename))
    # Only movie_folder/movie_collection_folder layouts have a per-movie
    # parent directory; movie_flat's parent_directory is the category root
    # itself (e.g. "/Volumes/movies"), which is never title evidence.
    if input_data.layout in ("movie_folder", "movie_collection_folder"):
        folder_clean = _clean_release_text(Path(input_data.parent_directory).name)
    else:
        folder_clean = _CleanedRelease(
            title="", year=None, year_match=None, edition=None, part_number=None, removed_tokens=()
        )

    filename_has_title = _has_real_title(filename_clean)
    folder_has_title = _has_real_title(folder_clean)

    title_source: str | None
    year_source: str | None
    if filename_has_title and filename_clean.year is not None:
        primary, title_source, year_source = filename_clean, "filename", "filename"
    elif folder_has_title and folder_clean.year is not None:
        primary, title_source, year_source = folder_clean, "parent_directory", "parent_directory"
    elif filename_has_title:
        primary, title_source, year_source = filename_clean, "filename", None
    elif folder_has_title:
        primary, title_source, year_source = folder_clean, "parent_directory", None
    else:
        primary, title_source, year_source = filename_clean, None, None

    years_conflict = (
        filename_clean.year is not None
        and folder_clean.year is not None
        and filename_clean.year != folder_clean.year
    )
    titles_conflict = (
        filename_has_title and folder_has_title and not _titles_similar(filename_clean.title, folder_clean.title)
    )
    conflicting = years_conflict or titles_conflict

    title = primary.title if title_source is not None else None
    year = primary.year if year_source is not None else None
    edition = filename_clean.edition or folder_clean.edition
    part_number = filename_clean.part_number or folder_clean.part_number

    if title is None:
        candidate_type = CandidateType.UNKNOWN
        confidence = Confidence.UNKNOWN
    elif conflicting or _is_ambiguous_title(title):
        candidate_type = CandidateType.MOVIE
        confidence = Confidence.LOW
    elif year is not None:
        candidate_type = CandidateType.MOVIE
        confidence = Confidence.HIGH
    else:
        candidate_type = CandidateType.MOVIE
        confidence = Confidence.MEDIUM

    removed_tokens = sorted(set(filename_clean.removed_tokens) | set(folder_clean.removed_tokens))
    evidence: dict[str, object] = {
        "title_source": title_source,
        "year_source": year_source,
        "filename_title": filename_clean.title or None,
        "filename_year": filename_clean.year,
        "folder_title": folder_clean.title or None,
        "folder_year": folder_clean.year,
        "removed_tokens": removed_tokens,
    }
    if year_source is not None and primary.year_match is not None:
        evidence["year_match"] = primary.year_match
    if conflicting:
        evidence["conflict"] = "title_or_year_mismatch_between_filename_and_folder"

    return IdentificationCandidate(
        media_file_id=input_data.media_file_id,
        candidate_type=candidate_type,
        confidence=confidence,
        parser_version=PARSER_VERSION,
        evidence=evidence,
        parsed_title=title,
        parsed_year=year,
        edition=edition,
        part_number=part_number,
    )


# --- television parsing --------------------------------------------------------

# S01E02, s01e02 (IGNORECASE), S01E02E03, S01E02-E03 -- the trailing group
# accepts zero or more "[-]?E\d+" repeats, so both the back-to-back and
# hyphenated multi-episode forms match as one span.
_SXXEXX_PATTERN = re.compile(r"S(?P<season>\d{1,2})E(?P<ep1>\d{1,3})(?:[-]?E\d{1,3})*", re.IGNORECASE)
# Extracts every E<digits> group from a matched SxxExx span, in order --
# used instead of parsing named groups individually so back-to-back and
# hyphenated forms share one code path.
_MULTI_EPISODE_PATTERN = re.compile(r"E(\d{1,3})", re.IGNORECASE)

# 1x02. Bounded to 1-2 season digits and (?<!\d)/(?!\d) on both sides so
# this doesn't fire inside an unrelated longer digit run (e.g. a
# resolution like "1920x1080").
_NXNN_PATTERN = re.compile(r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)", re.IGNORECASE)

# "Season 01 Episode 02" and dot/underscore/dash-separated variants of it.
_SEASON_EPISODE_WORDS_PATTERN = re.compile(
    r"season[\s._-]*(\d{1,2})[\s._-]*episode[\s._-]*(\d{1,3})", re.IGNORECASE
)

# A season-folder name on its own, e.g. "Season 01" -- season number only,
# no episode evidence. Same shape as inventory._SEASON_DIR_PATTERN.
_SEASON_FOLDER_PATTERN = re.compile(r"^season[\s_-]*(\d{1,2})$", re.IGNORECASE)

# Bonus/non-episode content recognized by keyword rather than a numbered
# pattern. Longer, more specific phrases are listed before the generic
# "extra"/"extras" catch-all.
_EXTRA_KEYWORD_CANONICAL: dict[str, str] = {
    "behind the scenes": "behind_the_scenes",
    "deleted scenes": "deleted_scene",
    "deleted scene": "deleted_scene",
    "gag reel": "gag_reel",
    "bloopers": "bloopers",
    "featurette": "featurette",
    "interview": "interview",
    "trailer": "trailer",
    "extras": "extra",
    "extra": "extra",
}
_EXTRA_KEYWORD_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _EXTRA_KEYWORD_CANONICAL) + r")\b", re.IGNORECASE
)


@dataclass(frozen=True)
class _SeasonEpisodeMatch:
    pattern: str
    match: re.Match[str]
    season: int
    episodes: tuple[int, ...]


def _search_season_episode(text: str) -> _SeasonEpisodeMatch | None:
    """Try each supported filename season/episode pattern in priority
    order (SxxExx, then "Season N Episode M" words, then NxNN), returning
    the first that matches. Filename evidence only -- season-folder
    evidence is applied separately by the caller, never here, so it can
    never override a pattern actually present in the filename."""
    sxxexx = _SXXEXX_PATTERN.search(text)
    if sxxexx is not None:
        episodes = tuple(int(n) for n in _MULTI_EPISODE_PATTERN.findall(sxxexx.group(0)))
        return _SeasonEpisodeMatch("SxxExx", sxxexx, int(sxxexx.group("season")), episodes)

    words = _SEASON_EPISODE_WORDS_PATTERN.search(text)
    if words is not None:
        return _SeasonEpisodeMatch("season_episode_words", words, int(words.group(1)), (int(words.group(2)),))

    nxnn = _NXNN_PATTERN.search(text)
    if nxnn is not None:
        return _SeasonEpisodeMatch("NxNN", nxnn, int(nxnn.group(1)), (int(nxnn.group(2)),))

    return None


def _folder_season_number(parent_directory: str, layout: str) -> int | None:
    """Season number from a "Season NN" parent directory -- only
    meaningful for tv_season_folder layout, where the immediate parent is
    the season folder itself."""
    if layout != "tv_season_folder":
        return None
    match = _SEASON_FOLDER_PATTERN.match(Path(parent_directory).name.strip())
    return int(match.group(1)) if match else None


def _folder_series_title(input_data: IdentificationInput) -> str | None:
    """Series title from directory structure: the series folder itself for
    tv_series_folder, or its parent (the season folder's parent) for
    tv_season_folder. None for any other layout -- there is no reliable
    series-named directory to read."""
    if input_data.layout == "tv_series_folder":
        name = Path(input_data.parent_directory).name
    elif input_data.layout == "tv_season_folder":
        name = Path(input_data.parent_directory).parent.name
    else:
        return None
    cleaned = _clean_release_text(name)
    return cleaned.title if cleaned.title and not _is_ambiguous_title(cleaned.title) else None


def _extra_candidate(
    input_data: IdentificationInput, extra_match: re.Match[str], folder_series_title: str | None
) -> IdentificationCandidate:
    canonical = _EXTRA_KEYWORD_CANONICAL[extra_match.group(0).lower()]
    evidence: dict[str, object] = {"pattern": "extra_keyword", "matched_text": extra_match.group(0)}
    # MEDIUM when at least a series folder places the content, LOW when
    # even that context is missing -- never higher, since bonus content
    # has no episode identity to be confident about.
    confidence = Confidence.MEDIUM if folder_series_title else Confidence.LOW
    return IdentificationCandidate(
        media_file_id=input_data.media_file_id,
        candidate_type=CandidateType.EXTRA,
        confidence=confidence,
        parser_version=PARSER_VERSION,
        evidence=evidence,
        parsed_series_title=folder_series_title,
        special_type=canonical,
    )


def _unknown_television_candidate(
    input_data: IdentificationInput, folder_series_title: str | None, folder_season: int | None
) -> IdentificationCandidate:
    """No season/episode pattern and no extra-content keyword in the
    filename -- insufficient evidence to classify. Any folder context is
    recorded in evidence but never promoted into season_number/
    episode_number: those stay unset rather than fabricated."""
    evidence: dict[str, object] = {"pattern": None}
    if folder_series_title is not None:
        evidence["folder_series_title"] = folder_series_title
    if folder_season is not None:
        evidence["folder_season"] = folder_season
    return IdentificationCandidate(
        media_file_id=input_data.media_file_id,
        candidate_type=CandidateType.UNKNOWN,
        confidence=Confidence.UNKNOWN,
        parser_version=PARSER_VERSION,
        evidence=evidence,
        parsed_series_title=folder_series_title,
    )


def _parse_television(input_data: IdentificationInput) -> IdentificationCandidate:
    """Conservative TV season/episode parsing.

    Filename evidence for season/episode numbers always takes precedence
    over season-folder evidence -- the folder only fills in a series title
    when the filename doesn't have one, or corroborates/conflicts with the
    filename's season number, never overrides it.

    Confidence rubric:
    - HIGH: a season/episode pattern was found in the filename, and a
      series title was found in the filename too (no folder dependency).
    - MEDIUM: a season/episode pattern was found in the filename, but the
      series title came from folder context instead.
    - LOW: the filename's season number conflicts with its season-folder
      directory, or no series title could be found anywhere; also used for
      EXTRA candidates with no folder context to place them.
    - UNKNOWN: no season/episode pattern and no recognized extra-content
      keyword anywhere in the filename.
    """
    filename_stem = _strip_extension(input_data.filename)
    match_result = _search_season_episode(filename_stem)
    folder_season = _folder_season_number(input_data.parent_directory, input_data.layout)
    folder_series_title = _folder_series_title(input_data)

    if match_result is None:
        extra_match = _EXTRA_KEYWORD_PATTERN.search(filename_stem)
        if extra_match is not None:
            return _extra_candidate(input_data, extra_match, folder_series_title)
        return _unknown_television_candidate(input_data, folder_series_title, folder_season)

    series_raw = filename_stem[: match_result.match.start()]
    episode_title_raw = filename_stem[match_result.match.end() :]
    series_clean = _clean_release_text(series_raw)
    episode_clean = _clean_release_text(episode_title_raw)

    series_has_title = bool(series_clean.title) and not _is_ambiguous_title(series_clean.title)
    if series_has_title:
        series_title, title_source = series_clean.title, "filename"
    elif folder_series_title is not None:
        series_title, title_source = folder_series_title, "parent_directory"
    else:
        series_title, title_source = None, None

    episode_title = (
        episode_clean.title if episode_clean.title and not _is_ambiguous_title(episode_clean.title) else None
    )

    season_conflict = folder_season is not None and folder_season != match_result.season
    is_special = match_result.season == 0

    if series_title is None or season_conflict:
        confidence = Confidence.LOW
    elif title_source == "filename":
        confidence = Confidence.HIGH
    else:
        confidence = Confidence.MEDIUM

    evidence: dict[str, object] = {
        "pattern": match_result.pattern,
        "matched_text": match_result.match.group(0),
        "series_title_source": title_source,
        "filename_season": match_result.season,
        "folder_season": folder_season,
    }
    if season_conflict:
        evidence["conflict"] = "season_mismatch_between_filename_and_season_folder"

    return IdentificationCandidate(
        media_file_id=input_data.media_file_id,
        candidate_type=CandidateType.SPECIAL if is_special else CandidateType.EPISODE,
        confidence=confidence,
        parser_version=PARSER_VERSION,
        evidence=evidence,
        parsed_series_title=series_title,
        season_number=match_result.season,
        episode_number=match_result.episodes[0] if match_result.episodes else None,
        episode_numbers=match_result.episodes,
        episode_title=episode_title,
        special_type="season_zero" if is_special else None,
    )


# --- classification: which parser to run ---------------------------------------

_MOVIE_LAYOUTS = frozenset({"movie_flat", "movie_folder", "movie_collection_folder"})
_TV_LAYOUTS = frozenset({"tv_series_folder", "tv_season_folder"})


def _parse_unclassified(input_data: IdentificationInput) -> IdentificationCandidate:
    """Neither category nor layout indicates movie or TV. Falls back to
    whichever parser's own pattern evidence actually fires in the
    filename -- a clear S01E02 pattern or an explicit year is real
    evidence of type independent of where the file lives.

    The movie fallback deliberately requires a parsed year, not just a
    parsed title: with no directory placement to lean on, a bare word
    (e.g. "readme") would otherwise "parse" as a MEDIUM-confidence movie
    title, which is exactly the kind of directory-independent guess this
    milestone's brief warns against. A year is a much stronger, more
    specific signal that this file is actually a movie.
    """
    television_candidate = _parse_television(input_data)
    if television_candidate.candidate_type != CandidateType.UNKNOWN:
        return television_candidate
    movie_candidate = _parse_movie(input_data)
    if movie_candidate.parsed_year is not None:
        return movie_candidate
    return television_candidate


def evaluate_file(input_data: IdentificationInput) -> IdentificationCandidate:
    """Parse one file's local identification candidate.

    Category and layout decide *which* parser runs -- legitimate evidence
    of content class, since a file's location in a movie- or TV-style
    directory is a real (if weak) signal. They are never used to inflate
    *confidence*: each parser derives its own confidence purely from the
    strength of what it actually parsed (see each parser's confidence
    rubric), so a file sitting in a movie/TV directory with an
    unparseable name still comes back LOW/UNKNOWN, never HIGH.

    Always returns exactly one candidate, never raises for a file this
    milestone can't confidently classify -- worst case is a MOVIE/EPISODE
    candidate at confidence UNKNOWN/LOW, or a genuinely UNKNOWN candidate
    type when even category/layout give no signal.
    """
    if input_data.category in inventory.MOVIE_CATEGORIES or input_data.layout in _MOVIE_LAYOUTS:
        return _parse_movie(input_data)
    if input_data.category in inventory.TV_CATEGORIES or input_data.layout in _TV_LAYOUTS:
        return _parse_television(input_data)
    return _parse_unclassified(input_data)
