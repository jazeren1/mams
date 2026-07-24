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

from .identification import IdentificationCandidate


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
