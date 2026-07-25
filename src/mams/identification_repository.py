"""Persistence for identification candidates: reconciles
`IdentificationCandidate` objects produced by `identification.evaluate_file()`
into the `identification_candidates` table, and (see the query layer
section below) provides read-only list/get/stats access for the CLI.

This module owns all identification-related SQL, mirroring
findings_repository.py's role for the findings schema. Parsing itself
lives in identification.py and never touches SQLite; this module never
parses, only reconciles and reads already-computed candidates/rows.

## Reconciliation lifecycle

`reconcile_candidates()` is given the full candidate set for every
currently-ACTIVE media file (see identification_service.py -- MISSING
files are never included). Keyed by `media_file_id` (the table's
`UNIQUE` column):

- No existing row -> INSERT. `created_at`/`updated_at` both default to
  CURRENT_TIMESTAMP.
- Existing row, content identical -> left untouched entirely (no UPDATE
  statement at all), so `id`/`created_at`/`updated_at` are all stable and
  a repeated evaluation against unchanged inventory produces zero writes.
- Existing row, content differs -> UPDATE every content column plus
  `updated_at`, preserving `id` and `created_at`.

Unlike `findings_repository.reconcile_findings()`, there is no
resolve/reactivate/ignore step: a candidate has no status lifecycle (see
docs/DATABASE.md) -- it is always "the current interpretation," or, for a
file no longer visited this run (MISSING), left exactly as it was.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass

from . import identification
from .identification import CandidateType, Confidence, IdentificationCandidate


def _serialize_evidence(evidence: dict[str, object]) -> str | None:
    """NULL for no evidence, otherwise a stable, sorted, compact encoding --
    same convention as findings.evidence_json (see docs/DATABASE.md)."""
    if not evidence:
        return None
    return json.dumps(evidence, sort_keys=True, separators=(",", ":"))


def _serialize_episode_numbers(episode_numbers: tuple[int, ...]) -> str | None:
    """NULL when there are no episode numbers, otherwise a compact JSON
    array in the order the parser produced them."""
    if not episode_numbers:
        return None
    return json.dumps(list(episode_numbers), separators=(",", ":"))


@dataclass(frozen=True)
class ReconciliationResult:
    """Summary of one `reconcile_candidates()` call, for CLI display and
    lifecycle-determinism tests."""

    created: int
    updated: int
    unchanged: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _row_matches(
    row: sqlite3.Row, candidate: IdentificationCandidate, evidence_json: str | None, episode_numbers_json: str | None
) -> bool:
    return (
        row["candidate_type"] == candidate.candidate_type.value
        and row["parsed_title"] == candidate.parsed_title
        and row["parsed_year"] == candidate.parsed_year
        and row["parsed_series_title"] == candidate.parsed_series_title
        and row["season_number"] == candidate.season_number
        and row["episode_number"] == candidate.episode_number
        and row["episode_numbers_json"] == episode_numbers_json
        and row["episode_title"] == candidate.episode_title
        and row["edition"] == candidate.edition
        and row["part_number"] == candidate.part_number
        and row["special_type"] == candidate.special_type
        and row["confidence"] == candidate.confidence.value
        and row["parser_version"] == candidate.parser_version
        and row["evidence_json"] == evidence_json
    )


def _insert_candidate(
    connection: sqlite3.Connection,
    candidate: IdentificationCandidate,
    evidence_json: str | None,
    episode_numbers_json: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO identification_candidates (
            media_file_id, candidate_type, parsed_title, parsed_year, parsed_series_title,
            season_number, episode_number, episode_numbers_json, episode_title,
            edition, part_number, special_type, confidence, parser_version, evidence_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate.media_file_id,
            candidate.candidate_type.value,
            candidate.parsed_title,
            candidate.parsed_year,
            candidate.parsed_series_title,
            candidate.season_number,
            candidate.episode_number,
            episode_numbers_json,
            candidate.episode_title,
            candidate.edition,
            candidate.part_number,
            candidate.special_type,
            candidate.confidence.value,
            candidate.parser_version,
            evidence_json,
        ),
    )


def _update_candidate(
    connection: sqlite3.Connection,
    row_id: int,
    candidate: IdentificationCandidate,
    evidence_json: str | None,
    episode_numbers_json: str | None,
) -> None:
    connection.execute(
        """
        UPDATE identification_candidates
        SET candidate_type = ?, parsed_title = ?, parsed_year = ?, parsed_series_title = ?,
            season_number = ?, episode_number = ?, episode_numbers_json = ?, episode_title = ?,
            edition = ?, part_number = ?, special_type = ?, confidence = ?, parser_version = ?,
            evidence_json = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            candidate.candidate_type.value,
            candidate.parsed_title,
            candidate.parsed_year,
            candidate.parsed_series_title,
            candidate.season_number,
            candidate.episode_number,
            episode_numbers_json,
            candidate.episode_title,
            candidate.edition,
            candidate.part_number,
            candidate.special_type,
            candidate.confidence.value,
            candidate.parser_version,
            evidence_json,
            row_id,
        ),
    )


def reconcile_candidates(
    connection: sqlite3.Connection, candidates: list[IdentificationCandidate]
) -> ReconciliationResult:
    """Reconcile a full ACTIVE-inventory candidate set into
    identification_candidates.

    Not itself transactional -- callers run this inside `with connection:`
    (see identification_service.evaluate_candidates) so a failure partway
    through rolls back every write this call has made. See the module
    docstring for the full lifecycle.
    """
    existing_by_media_file_id = {
        row["media_file_id"]: row
        for row in connection.execute("SELECT * FROM identification_candidates").fetchall()
    }

    created = updated = unchanged = 0
    for candidate in candidates:
        evidence_json = _serialize_evidence(candidate.evidence)
        episode_numbers_json = _serialize_episode_numbers(candidate.episode_numbers)
        existing = existing_by_media_file_id.get(candidate.media_file_id)

        if existing is None:
            _insert_candidate(connection, candidate, evidence_json, episode_numbers_json)
            created += 1
        elif _row_matches(existing, candidate, evidence_json, episode_numbers_json):
            unchanged += 1
        else:
            _update_candidate(connection, existing["id"], candidate, evidence_json, episode_numbers_json)
            updated += 1

    return ReconciliationResult(created=created, updated=updated, unchanged=unchanged)


# --- query layer (list / get / stats) ---------------------------------------
#
# Read-only access for `mams identify list/show/stats`. Every function here
# runs a fixed, small number of queries regardless of how many candidate
# rows match -- never one query per row. Path/category are resolved via
# JOIN at read time (never stored on identification_candidates -- see
# docs/DATABASE.md), the same pattern findings_repository.py uses.


@dataclass(frozen=True)
class CandidateRecord:
    """One identification_candidates row, with category/path resolved via
    join and evidence_json/episode_numbers_json parsed back into plain
    Python values."""

    id: int
    media_file_id: int
    category: str
    absolute_path: str
    relative_path: str
    candidate_type: str
    confidence: str
    parser_version: int
    parsed_title: str | None
    parsed_year: int | None
    parsed_series_title: str | None
    season_number: int | None
    episode_number: int | None
    episode_numbers: tuple[int, ...]
    episode_title: str | None
    edition: str | None
    part_number: int | None
    special_type: str | None
    evidence: dict[str, object] | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["episode_numbers"] = list(self.episode_numbers)
        return data


@dataclass(frozen=True)
class CandidateStats:
    total_count: int
    type_counts: dict[str, int]
    confidence_counts: dict[str, int]
    with_year_count: int
    without_year_count: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_CANDIDATE_BASE_SELECT = """
    SELECT
        identification_candidates.*,
        libraries.category AS category,
        media_files.absolute_path AS absolute_path,
        media_files.relative_path AS relative_path
    FROM identification_candidates
    JOIN media_files ON media_files.id = identification_candidates.media_file_id
    JOIN libraries ON libraries.id = media_files.library_id
"""

_CANDIDATE_ORDER_BY = " ORDER BY libraries.category, media_files.relative_path, identification_candidates.id"


def _row_to_candidate_record(row: sqlite3.Row) -> CandidateRecord:
    episode_numbers = (
        tuple(json.loads(row["episode_numbers_json"])) if row["episode_numbers_json"] is not None else ()
    )
    evidence = json.loads(row["evidence_json"]) if row["evidence_json"] is not None else None
    return CandidateRecord(
        id=row["id"],
        media_file_id=row["media_file_id"],
        category=row["category"],
        absolute_path=row["absolute_path"],
        relative_path=row["relative_path"],
        candidate_type=row["candidate_type"],
        confidence=row["confidence"],
        parser_version=row["parser_version"],
        parsed_title=row["parsed_title"],
        parsed_year=row["parsed_year"],
        parsed_series_title=row["parsed_series_title"],
        season_number=row["season_number"],
        episode_number=row["episode_number"],
        episode_numbers=episode_numbers,
        episode_title=row["episode_title"],
        edition=row["edition"],
        part_number=row["part_number"],
        special_type=row["special_type"],
        evidence=evidence,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_candidates(
    connection: sqlite3.Connection,
    *,
    candidate_type: str | None = None,
    confidence: str | None = None,
    category: str | None = None,
    has_year: bool | None = None,
    season_number: int | None = None,
    media_file_id: int | None = None,
    limit: int | None = None,
) -> list[CandidateRecord]:
    """List identification_candidates rows for browsing, with deterministic
    ordering. All filters are optional and combined with AND. `has_year`
    restricts to rows with (`True`) or without (`False`) a `parsed_year`;
    `None` (default) applies no filter. Ordered by (category,
    relative_path, id) -- stable regardless of insertion order. Single
    query with two JOINs, never one query per row.
    """
    clauses: list[str] = []
    params: list[object] = []
    if candidate_type is not None:
        clauses.append("identification_candidates.candidate_type = ?")
        params.append(candidate_type)
    if confidence is not None:
        clauses.append("identification_candidates.confidence = ?")
        params.append(confidence)
    if category is not None:
        clauses.append("libraries.category = ?")
        params.append(category)
    if has_year is True:
        clauses.append("identification_candidates.parsed_year IS NOT NULL")
    elif has_year is False:
        clauses.append("identification_candidates.parsed_year IS NULL")
    if season_number is not None:
        clauses.append("identification_candidates.season_number = ?")
        params.append(season_number)
    if media_file_id is not None:
        clauses.append("identification_candidates.media_file_id = ?")
        params.append(media_file_id)

    sql = _CANDIDATE_BASE_SELECT
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += _CANDIDATE_ORDER_BY
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    rows = connection.execute(sql, params).fetchall()
    return [_row_to_candidate_record(row) for row in rows]


def get_candidate(connection: sqlite3.Connection, candidate_id: int) -> CandidateRecord | None:
    """Look up a single candidate by id, or None if it doesn't exist."""
    sql = _CANDIDATE_BASE_SELECT + " WHERE identification_candidates.id = ?"
    row = connection.execute(sql, (candidate_id,)).fetchone()
    return _row_to_candidate_record(row) if row is not None else None


def get_candidate_stats(connection: sqlite3.Connection) -> CandidateStats:
    """Aggregate candidate statistics in three fixed queries, none of them
    per-row: total count, counts by candidate_type, counts by confidence,
    and a with/without-parsed_year split."""
    total = connection.execute("SELECT COUNT(*) AS n FROM identification_candidates").fetchone()["n"]
    type_counts = {
        row["candidate_type"]: row["n"]
        for row in connection.execute(
            "SELECT candidate_type, COUNT(*) AS n FROM identification_candidates GROUP BY candidate_type"
        ).fetchall()
    }
    confidence_counts = {
        row["confidence"]: row["n"]
        for row in connection.execute(
            "SELECT confidence, COUNT(*) AS n FROM identification_candidates GROUP BY confidence"
        ).fetchall()
    }
    year_row = connection.execute(
        """
        SELECT
            SUM(CASE WHEN parsed_year IS NOT NULL THEN 1 ELSE 0 END) AS with_year,
            SUM(CASE WHEN parsed_year IS NULL THEN 1 ELSE 0 END) AS without_year
        FROM identification_candidates
        """
    ).fetchone()

    return CandidateStats(
        total_count=total,
        type_counts=type_counts,
        confidence_counts=confidence_counts,
        with_year_count=year_row["with_year"] or 0,
        without_year_count=year_row["without_year"] or 0,
    )


# --- identification_overrides (Milestone 7C, Phase D) -----------------------
#
# An explicit, operator-controlled override of a file's effective local
# identification, kept entirely separate from identification_candidates so
# the parser's own evidence is never overwritten or lost (see
# docs/DATABASE.md, "identification_overrides"). This module owns this SQL
# too, mirroring its role for identification_candidates.


class InvalidOverrideError(ValueError):
    """Raised for an override request missing a field its candidate_type
    requires -- MOVIE needs --title; EPISODE needs --series/--season/at
    least one --episode. Raised before any write, so a rejected request
    never touches the database."""


@dataclass(frozen=True)
class OverrideRecord:
    """One identification_overrides row. `cleared_at` is `None` while the
    override is active; a cleared override's row is retained (not
    deleted), the same "supersede, don't erase" discipline as
    media_identity_assignments."""

    id: int
    media_file_id: int
    candidate_type: str
    title: str | None
    year: int | None
    series_title: str | None
    season_number: int | None
    episode_numbers: tuple[int, ...]
    episode_title: str | None
    reason: str | None
    created_at: str
    updated_at: str
    cleared_at: str | None

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["episode_numbers"] = list(self.episode_numbers)
        return data


def _row_to_override(row: sqlite3.Row) -> OverrideRecord:
    episode_numbers = (
        tuple(json.loads(row["episode_numbers_json"])) if row["episode_numbers_json"] is not None else ()
    )
    return OverrideRecord(
        id=row["id"],
        media_file_id=row["media_file_id"],
        candidate_type=row["candidate_type"],
        title=row["title"],
        year=row["year"],
        series_title=row["series_title"],
        season_number=row["season_number"],
        episode_numbers=episode_numbers,
        episode_title=row["episode_title"],
        reason=row["reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        cleared_at=row["cleared_at"],
    )


def get_active_override(connection: sqlite3.Connection, media_file_id: int) -> OverrideRecord | None:
    """The currently active override for a file, or None if it has never
    had one or its override was cleared. At most one row can ever match,
    enforced by the partial unique index on (media_file_id) WHERE
    cleared_at IS NULL."""
    row = connection.execute(
        "SELECT * FROM identification_overrides WHERE media_file_id = ? AND cleared_at IS NULL", (media_file_id,)
    ).fetchone()
    return _row_to_override(row) if row is not None else None


def get_override(connection: sqlite3.Connection, override_id: int) -> OverrideRecord | None:
    row = connection.execute("SELECT * FROM identification_overrides WHERE id = ?", (override_id,)).fetchone()
    return _row_to_override(row) if row is not None else None


def clear_override(connection: sqlite3.Connection, media_file_id: int) -> OverrideRecord | None:
    """Clear the active override for a file, if any -- reverting the
    effective candidate to the current parsed interpretation. A no-op
    (returns `None`) if no override is currently active; the cleared row
    is retained, never deleted. Not itself transactional -- callers run
    this inside `with connection:`."""
    existing = get_active_override(connection, media_file_id)
    if existing is None:
        return None
    connection.execute(
        "UPDATE identification_overrides SET cleared_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (existing.id,),
    )
    cleared = get_override(connection, existing.id)
    assert cleared is not None
    return cleared


def create_override(
    connection: sqlite3.Connection,
    *,
    media_file_id: int,
    candidate_type: str,
    title: str | None = None,
    year: int | None = None,
    series_title: str | None = None,
    season_number: int | None = None,
    episode_numbers: tuple[int, ...] | None = None,
    episode_title: str | None = None,
    reason: str | None = None,
) -> OverrideRecord:
    """Create a new active override for a file, clearing any existing
    active override first -- the same "supersede, don't overwrite"
    discipline as media_identity_assignments.assign_identity, so an
    override's full history (what was overridden, when, why) stays
    auditable across changes of mind rather than being overwritten in
    place. Raises `InvalidOverrideError` (before any write) if a field
    required by `candidate_type` is missing. Not itself transactional --
    callers run this inside `with connection:`.
    """
    if candidate_type == "MOVIE":
        if not title or not title.strip():
            raise InvalidOverrideError("a MOVIE override requires a non-empty title")
    elif candidate_type == "EPISODE":
        if not series_title or not series_title.strip():
            raise InvalidOverrideError("an EPISODE override requires a non-empty series title")
        if season_number is None:
            raise InvalidOverrideError("an EPISODE override requires a season number")
        if not episode_numbers:
            raise InvalidOverrideError("an EPISODE override requires at least one episode number")
    else:
        raise InvalidOverrideError(f"unsupported override candidate_type {candidate_type!r}; expected MOVIE or EPISODE")

    clear_override(connection, media_file_id)

    episode_numbers_json = (
        json.dumps(list(episode_numbers), separators=(",", ":")) if episode_numbers else None
    )
    cursor = connection.execute(
        """
        INSERT INTO identification_overrides (
            media_file_id, candidate_type, title, year, series_title, season_number,
            episode_numbers_json, episode_title, reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (media_file_id, candidate_type, title, year, series_title, season_number, episode_numbers_json, episode_title, reason),
    )
    override_id = cursor.lastrowid
    assert override_id is not None
    override = get_override(connection, override_id)
    assert override is not None
    return override


# --- effective candidate (Milestone 7C, Phase D) -----------------------------
#
# What resolution should actually use: an active manual override if one
# exists, else the current parsed candidate. Computed at read time, never
# materialized as its own table row -- there is exactly one place this can
# drift from ("is there an active override right now"), and it is already
# the single source of truth (identification_overrides).

_EFFECTIVE_SOURCE_PARSED = "PARSED"
_EFFECTIVE_SOURCE_OVERRIDE = "MANUAL_OVERRIDE"


@dataclass(frozen=True)
class EffectiveCandidate:
    """The candidate identification resolution/planning should use for one
    file right now, tagged with where it came from. `identification_candidate_id`
    is always the underlying *parsed* row's id (for FK linkage -- overrides
    never get their own identification_candidates row); `override_id` is
    set only when `source` is MANUAL_OVERRIDE."""

    media_file_id: int
    source: str
    candidate_type: str
    confidence: str
    parser_version: int
    parsed_title: str | None
    parsed_year: int | None
    parsed_series_title: str | None
    season_number: int | None
    episode_number: int | None
    episode_numbers: tuple[int, ...]
    episode_title: str | None
    identification_candidate_id: int | None
    override_id: int | None

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["episode_numbers"] = list(self.episode_numbers)
        return data


def get_effective_candidate(connection: sqlite3.Connection, media_file_id: int) -> EffectiveCandidate | None:
    """The effective candidate for a file: its active override if one
    exists, else its current parsed candidate. Returns `None` only if
    neither exists (the file has never been through `mams identify
    evaluate`)."""
    override = get_active_override(connection, media_file_id)
    parsed_matches = list_candidates(connection, media_file_id=media_file_id)
    parsed = parsed_matches[0] if parsed_matches else None

    if override is not None:
        return EffectiveCandidate(
            media_file_id=media_file_id,
            source=_EFFECTIVE_SOURCE_OVERRIDE,
            candidate_type=override.candidate_type,
            confidence=Confidence.HIGH.value,
            parser_version=parsed.parser_version if parsed is not None else identification.PARSER_VERSION,
            parsed_title=override.title,
            parsed_year=override.year,
            parsed_series_title=override.series_title,
            season_number=override.season_number,
            episode_number=override.episode_numbers[0] if override.episode_numbers else None,
            episode_numbers=override.episode_numbers,
            episode_title=override.episode_title,
            identification_candidate_id=parsed.id if parsed is not None else None,
            override_id=override.id,
        )

    if parsed is not None:
        return EffectiveCandidate(
            media_file_id=media_file_id,
            source=_EFFECTIVE_SOURCE_PARSED,
            candidate_type=parsed.candidate_type,
            confidence=parsed.confidence,
            parser_version=parsed.parser_version,
            parsed_title=parsed.parsed_title,
            parsed_year=parsed.parsed_year,
            parsed_series_title=parsed.parsed_series_title,
            season_number=parsed.season_number,
            episode_number=parsed.episode_number,
            episode_numbers=parsed.episode_numbers,
            episode_title=parsed.episode_title,
            identification_candidate_id=parsed.id,
            override_id=None,
        )

    return None


def to_identification_candidate(effective: EffectiveCandidate) -> IdentificationCandidate:
    """Build the pure `identification.IdentificationCandidate` scoring/
    resolution depend on from an `EffectiveCandidate`. `evidence` is
    intentionally empty, mirroring `resolution_service._to_identification_candidate`
    -- scoring never reads it."""
    return IdentificationCandidate(
        media_file_id=effective.media_file_id,
        candidate_type=CandidateType(effective.candidate_type),
        confidence=Confidence(effective.confidence),
        parser_version=effective.parser_version,
        evidence={},
        parsed_title=effective.parsed_title,
        parsed_year=effective.parsed_year,
        parsed_series_title=effective.parsed_series_title,
        season_number=effective.season_number,
        episode_number=effective.episode_number,
        episode_numbers=effective.episode_numbers,
        episode_title=effective.episode_title,
    )
