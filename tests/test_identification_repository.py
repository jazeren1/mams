"""Tests for the identification candidate query layer (list_candidates,
get_candidate, get_candidate_stats) in identification_repository.py.

Builds identification_candidates rows directly via SQL (same approach as
test_findings_repository.py) since these tests are about querying
already-persisted rows, not reconciliation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mams import identification_repository as repo
from mams.db import connect, migrate

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"


@pytest.fixture()
def connection(tmp_path: Path):
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


def _insert_media_file(
    connection: sqlite3.Connection, *, library_id: int, scan_id: int, name: str, relative_path: str | None = None
) -> int:
    relative_path = relative_path or name
    cursor = connection.execute(
        """
        INSERT INTO media_files (
            library_id, absolute_path, relative_path, filename, extension,
            parent_directory, layout, size_bytes, first_seen_scan_id, last_seen_scan_id
        ) VALUES (?, ?, ?, ?, ?, ?, 'movie_flat', 1000, ?, ?)
        """,
        (
            library_id,
            f"/Volumes/movies/{relative_path}",
            relative_path,
            name,
            ".mkv",
            "/Volumes/movies",
            scan_id,
            scan_id,
        ),
    )
    return _lastrowid(cursor)


def _insert_candidate_row(
    connection: sqlite3.Connection,
    *,
    media_file_id: int,
    candidate_type: str = "MOVIE",
    confidence: str = "HIGH",
    parser_version: int = 1,
    parsed_title: str | None = "Alien",
    parsed_year: int | None = 1979,
    season_number: int | None = None,
    episode_numbers_json: str | None = None,
    evidence_json: str | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO identification_candidates (
            media_file_id, candidate_type, confidence, parser_version,
            parsed_title, parsed_year, season_number, episode_numbers_json, evidence_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            media_file_id,
            candidate_type,
            confidence,
            parser_version,
            parsed_title,
            parsed_year,
            season_number,
            episode_numbers_json,
            evidence_json,
        ),
    )
    return _lastrowid(cursor)


# --- list_candidates: filters ------------------------------------------------------


def test_list_candidates_with_no_filters_returns_everything(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    file_a = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    file_b = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="B.mkv")
    _insert_candidate_row(connection, media_file_id=file_a)
    _insert_candidate_row(connection, media_file_id=file_b)

    results = repo.list_candidates(connection)
    assert len(results) == 2


def test_list_candidates_filters_by_candidate_type(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    file_a = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    file_b = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="B.mkv")
    _insert_candidate_row(connection, media_file_id=file_a, candidate_type="MOVIE")
    _insert_candidate_row(connection, media_file_id=file_b, candidate_type="UNKNOWN", parsed_title=None, parsed_year=None)

    results = repo.list_candidates(connection, candidate_type="UNKNOWN")
    assert len(results) == 1
    assert results[0].media_file_id == file_b


def test_list_candidates_filters_by_confidence(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    file_a = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    file_b = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="B.mkv")
    _insert_candidate_row(connection, media_file_id=file_a, confidence="HIGH")
    _insert_candidate_row(connection, media_file_id=file_b, confidence="LOW")

    results = repo.list_candidates(connection, confidence="LOW")
    assert len(results) == 1
    assert results[0].media_file_id == file_b


def test_list_candidates_filters_by_category(connection: sqlite3.Connection) -> None:
    movies_id = _insert_library(connection, category="movies")
    tv_id = _insert_library(connection, category="tv")
    scan_id = _insert_scan_run(connection)
    movie_file = _insert_media_file(connection, library_id=movies_id, scan_id=scan_id, name="A.mkv")
    tv_file = _insert_media_file(connection, library_id=tv_id, scan_id=scan_id, name="B.mkv")
    _insert_candidate_row(connection, media_file_id=movie_file)
    _insert_candidate_row(connection, media_file_id=tv_file, candidate_type="EPISODE")

    results = repo.list_candidates(connection, category="tv")
    assert len(results) == 1
    assert results[0].category == "tv"


def test_list_candidates_filters_by_has_year_true(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    file_a = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    file_b = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="B.mkv")
    _insert_candidate_row(connection, media_file_id=file_a, parsed_year=1979)
    _insert_candidate_row(connection, media_file_id=file_b, parsed_year=None, confidence="MEDIUM")

    results = repo.list_candidates(connection, has_year=True)
    assert len(results) == 1
    assert results[0].media_file_id == file_a


def test_list_candidates_filters_by_has_year_false(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    file_a = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    file_b = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="B.mkv")
    _insert_candidate_row(connection, media_file_id=file_a, parsed_year=1979)
    _insert_candidate_row(connection, media_file_id=file_b, parsed_year=None, confidence="MEDIUM")

    results = repo.list_candidates(connection, has_year=False)
    assert len(results) == 1
    assert results[0].media_file_id == file_b


def test_list_candidates_filters_by_season_number(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection, category="tv")
    scan_id = _insert_scan_run(connection)
    file_a = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    file_b = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="B.mkv")
    _insert_candidate_row(
        connection, media_file_id=file_a, candidate_type="EPISODE", parsed_title=None, season_number=1
    )
    _insert_candidate_row(
        connection, media_file_id=file_b, candidate_type="EPISODE", parsed_title=None, season_number=2
    )

    results = repo.list_candidates(connection, season_number=2)
    assert len(results) == 1
    assert results[0].media_file_id == file_b


def test_list_candidates_filters_by_media_file_id(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    file_a = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    file_b = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="B.mkv")
    _insert_candidate_row(connection, media_file_id=file_a)
    _insert_candidate_row(connection, media_file_id=file_b)

    results = repo.list_candidates(connection, media_file_id=file_a)
    assert len(results) == 1
    assert results[0].media_file_id == file_a


def test_list_candidates_respects_limit(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    for i in range(5):
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name=f"F{i}.mkv")
        _insert_candidate_row(connection, media_file_id=media_file_id)

    results = repo.list_candidates(connection, limit=2)
    assert len(results) == 2


def test_list_candidates_combines_filters_with_and(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    file_a = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    _insert_candidate_row(connection, media_file_id=file_a, confidence="HIGH", candidate_type="MOVIE")

    results = repo.list_candidates(connection, confidence="LOW", candidate_type="MOVIE")
    assert results == []


def test_list_candidates_on_empty_database_returns_empty_list(connection: sqlite3.Connection) -> None:
    assert repo.list_candidates(connection) == []


# --- category/path resolution and episode_numbers round-trip ----------------------


def test_list_candidates_resolves_category_and_path_via_join(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection, category="tv")
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(
        connection, library_id=library_id, scan_id=scan_id, name="Ep.mkv", relative_path="Show/Season 01/Ep.mkv"
    )
    _insert_candidate_row(
        connection,
        media_file_id=media_file_id,
        candidate_type="EPISODE",
        parsed_title=None,
        season_number=1,
        episode_numbers_json="[2,3]",
    )

    [result] = repo.list_candidates(connection)
    assert result.category == "tv"
    assert result.relative_path == "Show/Season 01/Ep.mkv"
    assert result.episode_numbers == (2, 3)


def test_candidate_with_no_episode_numbers_json_has_empty_tuple(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    _insert_candidate_row(connection, media_file_id=media_file_id)

    [result] = repo.list_candidates(connection)
    assert result.episode_numbers == ()


def test_candidate_evidence_round_trips_through_json(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    _insert_candidate_row(connection, media_file_id=media_file_id, evidence_json='{"title_source":"filename"}')

    [result] = repo.list_candidates(connection)
    assert result.evidence == {"title_source": "filename"}


# --- deterministic ordering --------------------------------------------------------


def test_list_candidates_orders_by_category_then_path(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    file_b = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="B.mkv")
    file_a = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    _insert_candidate_row(connection, media_file_id=file_b)
    _insert_candidate_row(connection, media_file_id=file_a)

    results = repo.list_candidates(connection)
    assert [r.relative_path for r in results] == ["A.mkv", "B.mkv"]


def test_list_candidates_ordering_is_stable_across_repeated_calls(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    for i in range(5):
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name=f"F{i}.mkv")
        _insert_candidate_row(connection, media_file_id=media_file_id)

    first = [r.id for r in repo.list_candidates(connection)]
    second = [r.id for r in repo.list_candidates(connection)]
    assert first == second


# --- get_candidate ------------------------------------------------------------------


def test_get_candidate_returns_matching_record(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    candidate_id = _insert_candidate_row(connection, media_file_id=media_file_id)

    record = repo.get_candidate(connection, candidate_id)
    assert record is not None
    assert record.id == candidate_id
    assert record.parsed_title == "Alien"


def test_get_candidate_returns_none_for_unknown_id(connection: sqlite3.Connection) -> None:
    assert repo.get_candidate(connection, 9999) is None


# --- stats ----------------------------------------------------------------------------


def test_get_candidate_stats_totals(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    file_a = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    file_b = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="B.mkv")
    file_c = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="C.mkv")
    _insert_candidate_row(connection, media_file_id=file_a, candidate_type="MOVIE", confidence="HIGH", parsed_year=1979)
    _insert_candidate_row(
        connection, media_file_id=file_b, candidate_type="MOVIE", confidence="MEDIUM", parsed_year=None
    )
    _insert_candidate_row(
        connection, media_file_id=file_c, candidate_type="UNKNOWN", confidence="UNKNOWN",
        parsed_title=None, parsed_year=None,
    )

    stats = repo.get_candidate_stats(connection)
    assert stats.total_count == 3
    assert stats.type_counts == {"MOVIE": 2, "UNKNOWN": 1}
    assert stats.confidence_counts == {"HIGH": 1, "MEDIUM": 1, "UNKNOWN": 1}
    assert stats.with_year_count == 1
    assert stats.without_year_count == 2


def test_get_candidate_stats_on_empty_database(connection: sqlite3.Connection) -> None:
    stats = repo.get_candidate_stats(connection)
    assert stats.total_count == 0
    assert stats.type_counts == {}
    assert stats.confidence_counts == {}
    assert stats.with_year_count == 0
    assert stats.without_year_count == 0


# --- bounded query counts -----------------------------------------------------------


def test_list_candidates_uses_a_bounded_number_of_queries(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    for i in range(15):
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name=f"F{i}.mkv")
        _insert_candidate_row(connection, media_file_id=media_file_id)

    executed: list[str] = []
    connection.set_trace_callback(executed.append)
    try:
        results = repo.list_candidates(connection)
    finally:
        connection.set_trace_callback(None)

    assert len(results) == 15
    select_statements = [sql for sql in executed if sql.strip().upper().startswith("SELECT")]
    assert len(select_statements) == 1, select_statements


def test_get_candidate_stats_uses_a_bounded_number_of_queries(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    for i in range(15):
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name=f"F{i}.mkv")
        _insert_candidate_row(connection, media_file_id=media_file_id)

    executed: list[str] = []
    connection.set_trace_callback(executed.append)
    try:
        repo.get_candidate_stats(connection)
    finally:
        connection.set_trace_callback(None)

    select_statements = [sql for sql in executed if sql.strip().upper().startswith("SELECT")]
    assert len(select_statements) <= 4, select_statements
