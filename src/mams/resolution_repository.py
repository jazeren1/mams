"""Persistence for external identity resolution: owns all SQL for
`external_identities`, `resolution_attempts`, `resolution_matches`, and
`media_identity_assignments`. Mirrors `identification_repository.py`'s
role for its schema -- see docs/DATABASE.md for full table rationale.

`resolution_service.py` is the only caller that combines this module with
`tmdb.py` (HTTP) and `scoring.py` (pure scoring); neither of those modules
is imported here, and this module never performs HTTP.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass

from .scoring import MatchScore


# --- external_identities -------------------------------------------------------


@dataclass(frozen=True)
class ExternalIdentityRecord:
    id: int
    provider: str
    media_type: str
    provider_id: int
    title: str | None
    original_title: str | None
    release_year: int | None
    release_date: str | None
    series_provider_id: int | None
    season_number: int | None
    episode_number: int | None
    episode_title: str | None
    runtime_seconds: int | None
    original_language: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _row_to_external_identity(row: sqlite3.Row) -> ExternalIdentityRecord:
    return ExternalIdentityRecord(
        id=row["id"],
        provider=row["provider"],
        media_type=row["media_type"],
        provider_id=row["provider_id"],
        title=row["title"],
        original_title=row["original_title"],
        release_year=row["release_year"],
        release_date=row["release_date"],
        series_provider_id=row["series_provider_id"],
        season_number=row["season_number"],
        episode_number=row["episode_number"],
        episode_title=row["episode_title"],
        runtime_seconds=row["runtime_seconds"],
        original_language=row["original_language"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_external_identity(connection: sqlite3.Connection, identity_id: int) -> ExternalIdentityRecord | None:
    row = connection.execute("SELECT * FROM external_identities WHERE id = ?", (identity_id,)).fetchone()
    return _row_to_external_identity(row) if row is not None else None


_IDENTITY_FIELDS = (
    "title",
    "original_title",
    "release_year",
    "release_date",
    "series_provider_id",
    "season_number",
    "episode_number",
    "episode_title",
    "runtime_seconds",
    "original_language",
)


def upsert_external_identity(
    connection: sqlite3.Connection,
    *,
    media_type: str,
    provider_id: int,
    title: str | None = None,
    original_title: str | None = None,
    release_year: int | None = None,
    release_date: str | None = None,
    series_provider_id: int | None = None,
    season_number: int | None = None,
    episode_number: int | None = None,
    episode_title: str | None = None,
    runtime_seconds: int | None = None,
    original_language: str | None = None,
) -> ExternalIdentityRecord:
    """Insert or refresh the one external_identities row for
    (provider='TMDB', media_type, provider_id). Refreshes display fields
    in place only if they actually changed (same no-churn discipline as
    identification_repository.reconcile_candidates), since the same TMDb
    title is very likely to be resolved against repeatedly."""
    values = (
        title,
        original_title,
        release_year,
        release_date,
        series_provider_id,
        season_number,
        episode_number,
        episode_title,
        runtime_seconds,
        original_language,
    )
    row = connection.execute(
        "SELECT * FROM external_identities WHERE provider = 'TMDB' AND media_type = ? AND provider_id = ?",
        (media_type, provider_id),
    ).fetchone()

    if row is None:
        cursor = connection.execute(
            """
            INSERT INTO external_identities (
                provider, media_type, provider_id, title, original_title, release_year, release_date,
                series_provider_id, season_number, episode_number, episode_title, runtime_seconds, original_language
            ) VALUES ('TMDB', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (media_type, provider_id, *values),
        )
        identity_id = cursor.lastrowid
        assert identity_id is not None
    else:
        identity_id = row["id"]
        current = tuple(row[field] for field in _IDENTITY_FIELDS)
        if current != values:
            connection.execute(
                """
                UPDATE external_identities SET
                    title = ?, original_title = ?, release_year = ?, release_date = ?, series_provider_id = ?,
                    season_number = ?, episode_number = ?, episode_title = ?, runtime_seconds = ?,
                    original_language = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (*values, identity_id),
            )

    identity = get_external_identity(connection, identity_id)
    assert identity is not None
    return identity


# --- resolution_attempts / resolution_matches ----------------------------------


@dataclass(frozen=True)
class CandidateMatchInput:
    """One ranked, scored candidate produced by resolution_service, ready
    to persist as a resolution_matches row. `rank` is assigned by the
    caller (1-based, deterministic tie-break already applied)."""

    provider_media_type: str
    provider_id: int
    title: str | None
    release_year: int | None
    series_title: str | None
    series_provider_id: int | None
    season_number: int | None
    episode_number: int | None
    score: MatchScore
    selected: bool = False


@dataclass(frozen=True)
class MatchRecord:
    id: int
    resolution_attempt_id: int
    provider: str
    provider_media_type: str
    provider_id: int
    title: str | None
    release_year: int | None
    series_title: str | None
    series_provider_id: int | None
    season_number: int | None
    episode_number: int | None
    score: float
    rank: int
    scoring: dict[str, object]
    selected: bool
    created_at: str

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        return data


def _row_to_match_record(row: sqlite3.Row) -> MatchRecord:
    return MatchRecord(
        id=row["id"],
        resolution_attempt_id=row["resolution_attempt_id"],
        provider=row["provider"],
        provider_media_type=row["provider_media_type"],
        provider_id=row["provider_id"],
        title=row["title"],
        release_year=row["release_year"],
        series_title=row["series_title"],
        series_provider_id=row["series_provider_id"],
        season_number=row["season_number"],
        episode_number=row["episode_number"],
        score=row["score"],
        rank=row["rank"],
        scoring=json.loads(row["scoring_json"]),
        selected=bool(row["selected"]),
        created_at=row["created_at"],
    )


@dataclass(frozen=True)
class AttemptRecord:
    id: int
    identification_candidate_id: int | None
    media_file_id: int | None
    category: str | None
    absolute_path: str | None
    provider: str
    status: str
    query_text: str | None
    query_year: int | None
    started_at: str
    completed_at: str | None
    error_message: str | None
    selected_match_id: int | None
    algorithm_version: int
    created_at: str
    identification_override_id: int | None
    matches: tuple[MatchRecord, ...]

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["matches"] = [m.to_dict() for m in self.matches]
        return data


_ATTEMPT_BASE_SELECT = """
    SELECT
        resolution_attempts.*,
        libraries.category AS category,
        media_files.absolute_path AS absolute_path
    FROM resolution_attempts
    LEFT JOIN media_files ON media_files.id = resolution_attempts.media_file_id
    LEFT JOIN libraries ON libraries.id = media_files.library_id
"""


def _row_to_attempt_record(row: sqlite3.Row, matches: tuple[MatchRecord, ...]) -> AttemptRecord:
    return AttemptRecord(
        id=row["id"],
        identification_candidate_id=row["identification_candidate_id"],
        media_file_id=row["media_file_id"],
        category=row["category"],
        absolute_path=row["absolute_path"],
        provider=row["provider"],
        status=row["status"],
        query_text=row["query_text"],
        query_year=row["query_year"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        error_message=row["error_message"],
        selected_match_id=row["selected_match_id"],
        algorithm_version=row["algorithm_version"],
        created_at=row["created_at"],
        identification_override_id=row["identification_override_id"],
        matches=matches,
    )


def _matches_for_attempts(connection: sqlite3.Connection, attempt_ids: list[int]) -> dict[int, list[MatchRecord]]:
    """One bounded query for every attempt's matches, never one query per
    attempt row (see identification_repository.py's query-count
    discipline)."""
    if not attempt_ids:
        return {}
    placeholders = ",".join("?" for _ in attempt_ids)
    rows = connection.execute(
        f"SELECT * FROM resolution_matches WHERE resolution_attempt_id IN ({placeholders}) ORDER BY resolution_attempt_id, rank",
        attempt_ids,
    ).fetchall()
    by_attempt: dict[int, list[MatchRecord]] = {attempt_id: [] for attempt_id in attempt_ids}
    for row in rows:
        by_attempt[row["resolution_attempt_id"]].append(_row_to_match_record(row))
    return by_attempt


def get_attempt(connection: sqlite3.Connection, attempt_id: int) -> AttemptRecord | None:
    row = connection.execute(_ATTEMPT_BASE_SELECT + " WHERE resolution_attempts.id = ?", (attempt_id,)).fetchone()
    if row is None:
        return None
    matches = _matches_for_attempts(connection, [attempt_id]).get(attempt_id, [])
    return _row_to_attempt_record(row, tuple(matches))


def list_attempts(
    connection: sqlite3.Connection,
    *,
    status: str | None = None,
    media_file_id: int | None = None,
    category: str | None = None,
    limit: int | None = None,
) -> list[AttemptRecord]:
    """List resolution_attempts for browsing, most recent first. Two
    queries total regardless of result count: one for attempts, one for
    all their matches."""
    clauses: list[str] = []
    params: list[object] = []
    if status is not None:
        clauses.append("resolution_attempts.status = ?")
        params.append(status)
    if media_file_id is not None:
        clauses.append("resolution_attempts.media_file_id = ?")
        params.append(media_file_id)
    if category is not None:
        clauses.append("libraries.category = ?")
        params.append(category)

    sql = _ATTEMPT_BASE_SELECT
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY resolution_attempts.started_at DESC, resolution_attempts.id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    rows = connection.execute(sql, params).fetchall()
    attempt_ids = [row["id"] for row in rows]
    matches_by_attempt = _matches_for_attempts(connection, attempt_ids)
    return [_row_to_attempt_record(row, tuple(matches_by_attempt.get(row["id"], []))) for row in rows]


def get_latest_attempt_for_candidate(
    connection: sqlite3.Connection, identification_candidate_id: int
) -> AttemptRecord | None:
    row = connection.execute(
        _ATTEMPT_BASE_SELECT
        + " WHERE resolution_attempts.identification_candidate_id = ?"
        + " ORDER BY resolution_attempts.started_at DESC, resolution_attempts.id DESC LIMIT 1",
        (identification_candidate_id,),
    ).fetchone()
    if row is None:
        return None
    matches = _matches_for_attempts(connection, [row["id"]]).get(row["id"], [])
    return _row_to_attempt_record(row, tuple(matches))


@dataclass(frozen=True)
class AttemptStats:
    total_count: int
    status_counts: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def get_attempt_stats(connection: sqlite3.Connection) -> AttemptStats:
    total = connection.execute("SELECT COUNT(*) AS n FROM resolution_attempts").fetchone()["n"]
    status_counts = {
        row["status"]: row["n"]
        for row in connection.execute(
            "SELECT status, COUNT(*) AS n FROM resolution_attempts GROUP BY status"
        ).fetchall()
    }
    return AttemptStats(total_count=total, status_counts=status_counts)


def record_attempt(
    connection: sqlite3.Connection,
    *,
    identification_candidate_id: int | None,
    media_file_id: int | None,
    status: str,
    query_text: str | None,
    query_year: int | None,
    error_message: str | None,
    matches: list[CandidateMatchInput],
    algorithm_version: int,
    identification_override_id: int | None = None,
) -> AttemptRecord:
    """Insert one fully-formed resolution_attempts row plus its ranked
    resolution_matches, in a single call. Attempts are always recorded
    complete (there is no PENDING-then-update lifecycle in this
    milestone): the entire search+score computation happens in
    `resolution_service.py` before anything is persisted, so `started_at`
    and `completed_at` both land in the same call. Not itself
    transactional -- callers run this inside `with connection:`.
    `identification_override_id` records whichever manual override (if
    any) was active when this attempt ran -- `None` means the attempt
    used the plain parsed candidate -- so a later readiness audit can tell
    an override change apart from no change at all (Milestone 7C)."""
    cursor = connection.execute(
        """
        INSERT INTO resolution_attempts (
            identification_candidate_id, media_file_id, provider, status, query_text, query_year,
            completed_at, error_message, algorithm_version, identification_override_id
        ) VALUES (?, ?, 'TMDB', ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?)
        """,
        (
            identification_candidate_id,
            media_file_id,
            status,
            query_text,
            query_year,
            error_message,
            algorithm_version,
            identification_override_id,
        ),
    )
    attempt_id = cursor.lastrowid
    assert attempt_id is not None

    selected_match_id: int | None = None
    for rank, match in enumerate(matches, start=1):
        scoring_json = json.dumps(match.score.to_scoring_dict(), sort_keys=True, separators=(",", ":"))
        match_cursor = connection.execute(
            """
            INSERT INTO resolution_matches (
                resolution_attempt_id, provider, provider_media_type, provider_id, title, release_year,
                series_title, series_provider_id, season_number, episode_number, score, rank, scoring_json, selected
            ) VALUES (?, 'TMDB', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                match.provider_media_type,
                match.provider_id,
                match.title,
                match.release_year,
                match.series_title,
                match.series_provider_id,
                match.season_number,
                match.episode_number,
                match.score.total_score,
                rank,
                scoring_json,
                1 if match.selected else 0,
            ),
        )
        if match.selected:
            selected_match_id = match_cursor.lastrowid

    if selected_match_id is not None:
        connection.execute(
            "UPDATE resolution_attempts SET selected_match_id = ? WHERE id = ?", (selected_match_id, attempt_id)
        )

    attempt = get_attempt(connection, attempt_id)
    assert attempt is not None
    return attempt


def select_match(connection: sqlite3.Connection, *, attempt_id: int, match_id: int) -> AttemptRecord:
    """Manual review: mark exactly one match `selected`, clear any other
    selected flag on this attempt, and mark the attempt RESOLVED. Every
    other ranked match is left untouched (still queryable as the rejected
    alternatives a reviewer considered)."""
    connection.execute(
        "UPDATE resolution_matches SET selected = 0 WHERE resolution_attempt_id = ?", (attempt_id,)
    )
    connection.execute(
        "UPDATE resolution_matches SET selected = 1 WHERE id = ? AND resolution_attempt_id = ?",
        (match_id, attempt_id),
    )
    connection.execute(
        "UPDATE resolution_attempts SET status = 'RESOLVED', selected_match_id = ? WHERE id = ?",
        (match_id, attempt_id),
    )
    attempt = get_attempt(connection, attempt_id)
    assert attempt is not None
    return attempt


def reject_attempt(connection: sqlite3.Connection, *, attempt_id: int) -> AttemptRecord:
    """Manual review: reject every returned match. The attempt becomes
    NO_MATCH (the same terminal status an automatic evaluation reaches
    when nothing plausible was returned) -- no separate REJECTED status
    is introduced; "a human found nothing acceptable here" and "nothing
    plausible was returned" are the same outcome for every downstream
    consumer (no assignment, eligible for ingest planning to treat as
    unresolved). Ranked alternatives are preserved untouched."""
    connection.execute(
        "UPDATE resolution_matches SET selected = 0 WHERE resolution_attempt_id = ?", (attempt_id,)
    )
    connection.execute(
        "UPDATE resolution_attempts SET status = 'NO_MATCH', selected_match_id = NULL WHERE id = ?",
        (attempt_id,),
    )
    attempt = get_attempt(connection, attempt_id)
    assert attempt is not None
    return attempt


# --- media_identity_assignments -------------------------------------------------


@dataclass(frozen=True)
class AssignmentRecord:
    id: int
    media_file_id: int
    identification_candidate_id: int | None
    external_identity_id: int
    resolution_attempt_id: int | None
    assignment_method: str
    confidence: str
    status: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _row_to_assignment(row: sqlite3.Row) -> AssignmentRecord:
    return AssignmentRecord(
        id=row["id"],
        media_file_id=row["media_file_id"],
        identification_candidate_id=row["identification_candidate_id"],
        external_identity_id=row["external_identity_id"],
        resolution_attempt_id=row["resolution_attempt_id"],
        assignment_method=row["assignment_method"],
        confidence=row["confidence"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_assignment(connection: sqlite3.Connection, assignment_id: int) -> AssignmentRecord | None:
    row = connection.execute("SELECT * FROM media_identity_assignments WHERE id = ?", (assignment_id,)).fetchone()
    return _row_to_assignment(row) if row is not None else None


def get_active_assignment(connection: sqlite3.Connection, media_file_id: int) -> AssignmentRecord | None:
    row = connection.execute(
        "SELECT * FROM media_identity_assignments WHERE media_file_id = ? AND status = 'ACTIVE'", (media_file_id,)
    ).fetchone()
    return _row_to_assignment(row) if row is not None else None


def assign_identity(
    connection: sqlite3.Connection,
    *,
    media_file_id: int,
    identification_candidate_id: int | None,
    external_identity_id: int,
    resolution_attempt_id: int | None,
    assignment_method: str,
    confidence: str,
) -> AssignmentRecord:
    """Confirm an external identity for a file. If the current ACTIVE
    assignment already points at the same identity with the same method
    and confidence, this is a no-op (returns the existing row unchanged)
    -- repeated evaluation must never create duplicate assignment history
    for an unchanged outcome. Otherwise, any existing ACTIVE assignment is
    marked SUPERSEDED and a new ACTIVE row is inserted -- assignments are
    append-only history, never updated in place (see docs/DATABASE.md)."""
    existing = get_active_assignment(connection, media_file_id)
    if (
        existing is not None
        and existing.external_identity_id == external_identity_id
        and existing.assignment_method == assignment_method
        and existing.confidence == confidence
    ):
        return existing

    if existing is not None:
        connection.execute(
            "UPDATE media_identity_assignments SET status = 'SUPERSEDED', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (existing.id,),
        )

    cursor = connection.execute(
        """
        INSERT INTO media_identity_assignments (
            media_file_id, identification_candidate_id, external_identity_id, resolution_attempt_id,
            assignment_method, confidence, status
        ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE')
        """,
        (media_file_id, identification_candidate_id, external_identity_id, resolution_attempt_id, assignment_method, confidence),
    )
    assignment_id = cursor.lastrowid
    assert assignment_id is not None
    assignment = get_assignment(connection, assignment_id)
    assert assignment is not None
    return assignment


def revoke_assignment(connection: sqlite3.Connection, *, assignment_id: int) -> AssignmentRecord | None:
    """Revoke an ACTIVE assignment (a human determined it was wrong).
    A no-op (returns the row unchanged) if it is not currently ACTIVE."""
    connection.execute(
        "UPDATE media_identity_assignments SET status = 'REVOKED', updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND status = 'ACTIVE'",
        (assignment_id,),
    )
    return get_assignment(connection, assignment_id)
