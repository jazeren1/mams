"""Tests for resolution_repository.py: external_identities upsert,
resolution_attempts/resolution_matches persistence, manual
select/reject, and media_identity_assignments lifecycle.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mams.db import connect, migrate
from mams.resolution_repository import (
    AssignmentNotConfirmableError,
    CandidateMatchInput,
    assign_identity,
    confirm_assignment_for_ingest,
    get_active_assignment,
    get_attempt_stats,
    get_external_identity,
    get_latest_attempt_for_candidate,
    list_attempts,
    record_attempt,
    reject_attempt,
    revoke_assignment,
    select_match,
    upsert_external_identity,
)
from mams.scoring import MatchScore

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"


@pytest.fixture()
def connection(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "mams.db"
    migrate(db_path, REPO_MIGRATIONS_DIR)
    conn = connect(db_path)
    yield conn
    conn.close()


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _insert_library(connection: sqlite3.Connection, category: str = "movies") -> int:
    cursor = connection.execute(
        "INSERT INTO libraries (category, root_path) VALUES (?, ?)", (category, f"/Volumes/{category}")
    )
    return _lastrowid(cursor)


def _insert_scan_run(connection: sqlite3.Connection) -> int:
    cursor = connection.execute("INSERT INTO scan_runs DEFAULT VALUES")
    return _lastrowid(cursor)


def _insert_media_file(connection: sqlite3.Connection, *, library_id: int, scan_id: int, name: str = "Alien (1979).mkv") -> int:
    cursor = connection.execute(
        """
        INSERT INTO media_files (
            library_id, absolute_path, relative_path, filename, extension,
            parent_directory, layout, size_bytes, first_seen_scan_id, last_seen_scan_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (library_id, f"/Volumes/movies/{name}", name, name, ".mkv", "/Volumes/movies", "movie_flat", 1234, scan_id, scan_id),
    )
    return _lastrowid(cursor)


def _insert_candidate(connection: sqlite3.Connection, *, media_file_id: int) -> int:
    cursor = connection.execute(
        """
        INSERT INTO identification_candidates (media_file_id, candidate_type, parsed_title, confidence, parser_version)
        VALUES (?, 'MOVIE', 'Alien', 'HIGH', 1)
        """,
        (media_file_id,),
    )
    return _lastrowid(cursor)


def _movie_match(*, provider_id: int = 348, title: str = "Alien", score: float = 0.95, selected: bool = False) -> CandidateMatchInput:
    return CandidateMatchInput(
        provider_media_type="MOVIE",
        provider_id=provider_id,
        title=title,
        release_year=1979,
        series_title=None,
        series_provider_id=None,
        season_number=None,
        episode_number=None,
        score=MatchScore(total_score=score, components={"title_score": score}, reasons=("exact normalized title match",)),
        selected=selected,
    )


@pytest.fixture()
def seeded(connection: sqlite3.Connection) -> tuple[int, int, int]:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
    candidate_id = _insert_candidate(connection, media_file_id=media_file_id)
    return library_id, media_file_id, candidate_id


# --- external_identities upsert ------------------------------------------------


def test_upsert_external_identity_creates_new_row(connection: sqlite3.Connection) -> None:
    identity = upsert_external_identity(connection, media_type="MOVIE", provider_id=348, title="Alien", release_year=1979)
    assert identity.provider == "TMDB"
    assert identity.media_type == "MOVIE"
    assert identity.title == "Alien"
    fetched = get_external_identity(connection, identity.id)
    assert fetched == identity


def test_upsert_external_identity_is_idempotent_when_unchanged(connection: sqlite3.Connection) -> None:
    first = upsert_external_identity(connection, media_type="MOVIE", provider_id=348, title="Alien", release_year=1979)
    second = upsert_external_identity(connection, media_type="MOVIE", provider_id=348, title="Alien", release_year=1979)
    assert first.id == second.id
    assert first.updated_at == second.updated_at
    count = connection.execute("SELECT COUNT(*) FROM external_identities").fetchone()[0]
    assert count == 1


def test_upsert_external_identity_updates_changed_fields_in_place(connection: sqlite3.Connection) -> None:
    first = upsert_external_identity(connection, media_type="MOVIE", provider_id=348, title="Alien", release_year=1979)
    second = upsert_external_identity(connection, media_type="MOVIE", provider_id=348, title="Alien (Updated)", release_year=1979)
    assert first.id == second.id
    assert second.title == "Alien (Updated)"
    count = connection.execute("SELECT COUNT(*) FROM external_identities").fetchone()[0]
    assert count == 1


def test_upsert_external_identity_distinguishes_media_types_with_same_provider_id(connection: sqlite3.Connection) -> None:
    movie = upsert_external_identity(connection, media_type="MOVIE", provider_id=550, title="Fight Club")
    series = upsert_external_identity(connection, media_type="SERIES", provider_id=550, title="Some Show")
    assert movie.id != series.id


# --- record_attempt -------------------------------------------------------------


def test_record_attempt_persists_ranked_matches(connection: sqlite3.Connection, seeded: tuple[int, int, int]) -> None:
    _, media_file_id, candidate_id = seeded
    attempt = record_attempt(
        connection,
        identification_candidate_id=candidate_id,
        media_file_id=media_file_id,
        status="RESOLVED",
        query_text="Alien",
        query_year=1979,
        error_message=None,
        matches=[
            _movie_match(provider_id=348, score=0.95, selected=True),
            _movie_match(provider_id=999, score=0.5, selected=False),
        ],
        algorithm_version=1,
    )
    assert attempt.status == "RESOLVED"
    assert len(attempt.matches) == 2
    assert attempt.matches[0].rank == 1
    assert attempt.matches[0].selected is True
    assert attempt.matches[1].rank == 2
    assert attempt.selected_match_id == attempt.matches[0].id


def test_record_attempt_with_no_matches(connection: sqlite3.Connection, seeded: tuple[int, int, int]) -> None:
    _, media_file_id, candidate_id = seeded
    attempt = record_attempt(
        connection,
        identification_candidate_id=candidate_id,
        media_file_id=media_file_id,
        status="NO_MATCH",
        query_text="Alien",
        query_year=1979,
        error_message=None,
        matches=[],
        algorithm_version=1,
    )
    assert attempt.matches == ()
    assert attempt.selected_match_id is None


def test_record_attempt_persists_error_message_for_failed(connection: sqlite3.Connection, seeded: tuple[int, int, int]) -> None:
    _, media_file_id, candidate_id = seeded
    attempt = record_attempt(
        connection,
        identification_candidate_id=candidate_id,
        media_file_id=media_file_id,
        status="FAILED",
        query_text="Alien",
        query_year=1979,
        error_message="TMDb rate limit exceeded",
        matches=[],
        algorithm_version=1,
    )
    assert attempt.status == "FAILED"
    assert attempt.error_message == "TMDb rate limit exceeded"


def test_multiple_attempts_preserved_for_same_candidate(connection: sqlite3.Connection, seeded: tuple[int, int, int]) -> None:
    _, media_file_id, candidate_id = seeded
    record_attempt(
        connection, identification_candidate_id=candidate_id, media_file_id=media_file_id, status="NO_MATCH",
        query_text="Alien", query_year=1979, error_message=None, matches=[], algorithm_version=1,
    )
    second = record_attempt(
        connection, identification_candidate_id=candidate_id, media_file_id=media_file_id, status="RESOLVED",
        query_text="Alien", query_year=1979, error_message=None, matches=[_movie_match(selected=True)], algorithm_version=1,
    )
    all_attempts = list_attempts(connection, media_file_id=media_file_id)
    assert len(all_attempts) == 2
    latest = get_latest_attempt_for_candidate(connection, candidate_id)
    assert latest is not None
    assert latest.id == second.id


# --- query layer -----------------------------------------------------------------


def test_list_attempts_filters_by_status(connection: sqlite3.Connection, seeded: tuple[int, int, int]) -> None:
    _, media_file_id, candidate_id = seeded
    record_attempt(
        connection, identification_candidate_id=candidate_id, media_file_id=media_file_id, status="NO_MATCH",
        query_text="x", query_year=None, error_message=None, matches=[], algorithm_version=1,
    )
    record_attempt(
        connection, identification_candidate_id=candidate_id, media_file_id=media_file_id, status="RESOLVED",
        query_text="x", query_year=None, error_message=None, matches=[_movie_match(selected=True)], algorithm_version=1,
    )
    resolved = list_attempts(connection, status="RESOLVED")
    assert len(resolved) == 1
    assert resolved[0].status == "RESOLVED"


def test_list_attempts_uses_a_bounded_number_of_queries(connection: sqlite3.Connection, seeded: tuple[int, int, int]) -> None:
    _, media_file_id, candidate_id = seeded
    for _ in range(3):
        record_attempt(
            connection, identification_candidate_id=candidate_id, media_file_id=media_file_id, status="RESOLVED",
            query_text="x", query_year=None, error_message=None, matches=[_movie_match(selected=True)], algorithm_version=1,
        )
    executed: list[str] = []
    connection.set_trace_callback(executed.append)
    list_attempts(connection)
    connection.set_trace_callback(None)
    selects = [s for s in executed if s.strip().upper().startswith("SELECT")]
    assert len(selects) == 2


def test_get_attempt_stats(connection: sqlite3.Connection, seeded: tuple[int, int, int]) -> None:
    _, media_file_id, candidate_id = seeded
    record_attempt(
        connection, identification_candidate_id=candidate_id, media_file_id=media_file_id, status="NO_MATCH",
        query_text="x", query_year=None, error_message=None, matches=[], algorithm_version=1,
    )
    record_attempt(
        connection, identification_candidate_id=candidate_id, media_file_id=media_file_id, status="RESOLVED",
        query_text="x", query_year=None, error_message=None, matches=[_movie_match(selected=True)], algorithm_version=1,
    )
    stats = get_attempt_stats(connection)
    assert stats.total_count == 2
    assert stats.status_counts == {"NO_MATCH": 1, "RESOLVED": 1}


# --- manual select / reject ------------------------------------------------------


def test_select_match_marks_resolved_and_preserves_alternatives(connection: sqlite3.Connection, seeded: tuple[int, int, int]) -> None:
    _, media_file_id, candidate_id = seeded
    attempt = record_attempt(
        connection, identification_candidate_id=candidate_id, media_file_id=media_file_id, status="REVIEW_REQUIRED",
        query_text="Alien", query_year=1979, error_message=None,
        matches=[_movie_match(provider_id=348, score=0.7), _movie_match(provider_id=999, score=0.65)],
        algorithm_version=1,
    )
    second_match_id = attempt.matches[1].id

    updated = select_match(connection, attempt_id=attempt.id, match_id=second_match_id)

    assert updated.status == "RESOLVED"
    assert updated.selected_match_id == second_match_id
    selected_flags = {m.id: m.selected for m in updated.matches}
    assert selected_flags[second_match_id] is True
    assert selected_flags[attempt.matches[0].id] is False
    assert len(updated.matches) == 2  # alternatives preserved, not deleted


def test_reject_attempt_marks_no_match_and_clears_selection(connection: sqlite3.Connection, seeded: tuple[int, int, int]) -> None:
    _, media_file_id, candidate_id = seeded
    attempt = record_attempt(
        connection, identification_candidate_id=candidate_id, media_file_id=media_file_id, status="REVIEW_REQUIRED",
        query_text="Alien", query_year=1979, error_message=None,
        matches=[_movie_match(provider_id=348, score=0.7)], algorithm_version=1,
    )
    updated = reject_attempt(connection, attempt_id=attempt.id)
    assert updated.status == "NO_MATCH"
    assert updated.selected_match_id is None
    assert all(not m.selected for m in updated.matches)
    assert len(updated.matches) == 1  # alternatives preserved


# --- media_identity_assignments lifecycle ----------------------------------------


def test_assign_identity_creates_active_assignment(connection: sqlite3.Connection, seeded: tuple[int, int, int]) -> None:
    _, media_file_id, candidate_id = seeded
    identity = upsert_external_identity(connection, media_type="MOVIE", provider_id=348, title="Alien", release_year=1979)
    assignment = assign_identity(
        connection, media_file_id=media_file_id, identification_candidate_id=candidate_id,
        external_identity_id=identity.id, resolution_attempt_id=None, assignment_method="AUTO", confidence="HIGH",
    )
    assert assignment.status == "ACTIVE"
    active = get_active_assignment(connection, media_file_id)
    assert active is not None
    assert active.id == assignment.id


def test_assign_identity_is_a_no_op_when_unchanged(connection: sqlite3.Connection, seeded: tuple[int, int, int]) -> None:
    """Repeated evaluation with the same outcome must never duplicate
    assignment history."""
    _, media_file_id, candidate_id = seeded
    identity = upsert_external_identity(connection, media_type="MOVIE", provider_id=348, title="Alien", release_year=1979)
    first = assign_identity(
        connection, media_file_id=media_file_id, identification_candidate_id=candidate_id,
        external_identity_id=identity.id, resolution_attempt_id=None, assignment_method="AUTO", confidence="HIGH",
    )
    second = assign_identity(
        connection, media_file_id=media_file_id, identification_candidate_id=candidate_id,
        external_identity_id=identity.id, resolution_attempt_id=None, assignment_method="AUTO", confidence="HIGH",
    )
    assert first.id == second.id
    count = connection.execute("SELECT COUNT(*) FROM media_identity_assignments").fetchone()[0]
    assert count == 1


def test_assign_identity_supersedes_prior_assignment_on_change(connection: sqlite3.Connection, seeded: tuple[int, int, int]) -> None:
    _, media_file_id, candidate_id = seeded
    identity_1 = upsert_external_identity(connection, media_type="MOVIE", provider_id=348, title="Alien", release_year=1979)
    identity_2 = upsert_external_identity(connection, media_type="MOVIE", provider_id=999, title="Alien 3", release_year=1992)

    first = assign_identity(
        connection, media_file_id=media_file_id, identification_candidate_id=candidate_id,
        external_identity_id=identity_1.id, resolution_attempt_id=None, assignment_method="AUTO", confidence="HIGH",
    )
    second = assign_identity(
        connection, media_file_id=media_file_id, identification_candidate_id=candidate_id,
        external_identity_id=identity_2.id, resolution_attempt_id=None, assignment_method="MANUAL", confidence="MEDIUM",
    )

    assert second.id != first.id
    count = connection.execute("SELECT COUNT(*) FROM media_identity_assignments").fetchone()[0]
    assert count == 2
    active = get_active_assignment(connection, media_file_id)
    assert active is not None
    assert active.id == second.id
    superseded = connection.execute(
        "SELECT status FROM media_identity_assignments WHERE id = ?", (first.id,)
    ).fetchone()
    assert superseded["status"] == "SUPERSEDED"


def test_revoke_assignment(connection: sqlite3.Connection, seeded: tuple[int, int, int]) -> None:
    _, media_file_id, candidate_id = seeded
    identity = upsert_external_identity(connection, media_type="MOVIE", provider_id=348, title="Alien", release_year=1979)
    assignment = assign_identity(
        connection, media_file_id=media_file_id, identification_candidate_id=candidate_id,
        external_identity_id=identity.id, resolution_attempt_id=None, assignment_method="AUTO", confidence="HIGH",
    )
    revoked = revoke_assignment(connection, assignment_id=assignment.id)
    assert revoked is not None
    assert revoked.status == "REVOKED"
    assert get_active_assignment(connection, media_file_id) is None


# --- confirm_assignment_for_ingest -------------------------------------------------


def _manual_assignment(connection: sqlite3.Connection, *, media_file_id: int, candidate_id: int) -> int:
    identity = upsert_external_identity(connection, media_type="MOVIE", provider_id=8077, title="Alien 3", release_year=1992)
    assignment = assign_identity(
        connection, media_file_id=media_file_id, identification_candidate_id=candidate_id,
        external_identity_id=identity.id, resolution_attempt_id=None, assignment_method="MANUAL", confidence="MEDIUM",
    )
    return assignment.id


def test_confirm_assignment_for_ingest_sets_confirmation(connection: sqlite3.Connection, seeded: tuple[int, int, int]) -> None:
    _, media_file_id, candidate_id = seeded
    assignment_id = _manual_assignment(connection, media_file_id=media_file_id, candidate_id=candidate_id)

    confirmed = confirm_assignment_for_ingest(connection, assignment_id=assignment_id)

    assert confirmed.confirmed_for_ingest_at is not None
    assert confirmed.confirmed_by == "MANUAL_CLI"


def test_confirm_assignment_for_ingest_is_idempotent(connection: sqlite3.Connection, seeded: tuple[int, int, int]) -> None:
    _, media_file_id, candidate_id = seeded
    assignment_id = _manual_assignment(connection, media_file_id=media_file_id, candidate_id=candidate_id)

    first = confirm_assignment_for_ingest(connection, assignment_id=assignment_id)
    second = confirm_assignment_for_ingest(connection, assignment_id=assignment_id)

    assert first.confirmed_for_ingest_at == second.confirmed_for_ingest_at


def test_confirm_assignment_for_ingest_rejects_auto_assignment(
    connection: sqlite3.Connection, seeded: tuple[int, int, int]
) -> None:
    _, media_file_id, candidate_id = seeded
    identity = upsert_external_identity(connection, media_type="MOVIE", provider_id=348, title="Alien", release_year=1979)
    assignment = assign_identity(
        connection, media_file_id=media_file_id, identification_candidate_id=candidate_id,
        external_identity_id=identity.id, resolution_attempt_id=None, assignment_method="AUTO", confidence="HIGH",
    )

    with pytest.raises(AssignmentNotConfirmableError, match="AUTO"):
        confirm_assignment_for_ingest(connection, assignment_id=assignment.id)


def test_confirm_assignment_for_ingest_rejects_superseded_assignment(
    connection: sqlite3.Connection, seeded: tuple[int, int, int]
) -> None:
    _, media_file_id, candidate_id = seeded
    stale_id = _manual_assignment(connection, media_file_id=media_file_id, candidate_id=candidate_id)
    identity_2 = upsert_external_identity(connection, media_type="MOVIE", provider_id=999, title="Alien 3 Remake", release_year=1993)
    assign_identity(
        connection, media_file_id=media_file_id, identification_candidate_id=candidate_id,
        external_identity_id=identity_2.id, resolution_attempt_id=None, assignment_method="MANUAL", confidence="MEDIUM",
    )

    with pytest.raises(AssignmentNotConfirmableError, match="not ACTIVE"):
        confirm_assignment_for_ingest(connection, assignment_id=stale_id)


def test_confirm_assignment_for_ingest_rejects_unknown_id(connection: sqlite3.Connection) -> None:
    with pytest.raises(AssignmentNotConfirmableError):
        confirm_assignment_for_ingest(connection, assignment_id=999999)


def test_confirm_assignment_for_ingest_does_not_carry_to_replacement_assignment(
    connection: sqlite3.Connection, seeded: tuple[int, int, int]
) -> None:
    """Confirmation must apply only to the exact assignment row -- a new
    manual selection (a new assignment row) must always start
    unconfirmed, even if the prior assignment for the same file was
    confirmed."""
    _, media_file_id, candidate_id = seeded
    first_id = _manual_assignment(connection, media_file_id=media_file_id, candidate_id=candidate_id)
    confirm_assignment_for_ingest(connection, assignment_id=first_id)

    identity_2 = upsert_external_identity(connection, media_type="MOVIE", provider_id=999, title="Alien 3 Remake", release_year=1993)
    replacement = assign_identity(
        connection, media_file_id=media_file_id, identification_candidate_id=candidate_id,
        external_identity_id=identity_2.id, resolution_attempt_id=None, assignment_method="MANUAL", confidence="MEDIUM",
    )

    assert replacement.confirmed_for_ingest_at is None
