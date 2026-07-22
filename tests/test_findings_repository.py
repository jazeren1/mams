"""Tests for the findings query layer (list_findings, get_finding,
get_findings_stats) in findings_repository.py.

Builds findings rows directly via SQL (same approach as
test_schema_findings.py / test_findings_lifecycle.py) since these tests
are about querying already-persisted rows, not reconciliation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mams import findings_repository as repo
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


def _insert_finding(
    connection: sqlite3.Connection,
    *,
    rule_code: str,
    severity: str = "ERROR",
    status: str = "ACTIVE",
    media_file_id: int | None,
    library_id: int | None,
    summary: str = "summary",
    evidence_json: str | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO findings (rule_code, severity, status, media_file_id, library_id, summary, evidence_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (rule_code, severity, status, media_file_id, library_id, summary, evidence_json),
    )
    return _lastrowid(cursor)


# --- list_findings: filters ------------------------------------------------------


def test_list_findings_with_no_filters_returns_everything(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    _insert_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id, library_id=library_id)
    _insert_finding(connection, rule_code="unknown_layout", media_file_id=media_file_id, library_id=library_id)

    results = repo.list_findings(connection)
    assert len(results) == 2


def test_list_findings_filters_by_status(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    _insert_finding(
        connection, rule_code="zero_byte_file", status="ACTIVE", media_file_id=media_file_id, library_id=library_id
    )
    _insert_finding(
        connection, rule_code="unknown_layout", status="RESOLVED", media_file_id=media_file_id, library_id=library_id
    )

    results = repo.list_findings(connection, status="RESOLVED")
    assert len(results) == 1
    assert results[0].rule_code == "unknown_layout"


def test_list_findings_filters_by_severity(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    _insert_finding(
        connection, rule_code="zero_byte_file", severity="ERROR", media_file_id=media_file_id, library_id=library_id
    )
    _insert_finding(
        connection, rule_code="no_audio_track", severity="WARNING", media_file_id=media_file_id, library_id=library_id
    )

    results = repo.list_findings(connection, severity="WARNING")
    assert len(results) == 1
    assert results[0].rule_code == "no_audio_track"


def test_list_findings_filters_by_rule_code(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    _insert_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id, library_id=library_id)
    _insert_finding(connection, rule_code="unknown_layout", media_file_id=media_file_id, library_id=library_id)

    results = repo.list_findings(connection, rule_code="unknown_layout")
    assert len(results) == 1
    assert results[0].rule_code == "unknown_layout"


def test_list_findings_filters_by_category(connection: sqlite3.Connection) -> None:
    movies_id = _insert_library(connection, category="movies")
    tv_id = _insert_library(connection, category="tv")
    scan_id = _insert_scan_run(connection)
    movie_file = _insert_media_file(connection, library_id=movies_id, scan_id=scan_id, name="A.mkv")
    tv_file = _insert_media_file(connection, library_id=tv_id, scan_id=scan_id, name="B.mkv")
    _insert_finding(connection, rule_code="zero_byte_file", media_file_id=movie_file, library_id=movies_id)
    _insert_finding(connection, rule_code="zero_byte_file", media_file_id=tv_file, library_id=tv_id)

    results = repo.list_findings(connection, category="tv")
    assert len(results) == 1
    assert results[0].category == "tv"


def test_list_findings_filters_by_media_file_id(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    file_a = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    file_b = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="B.mkv")
    _insert_finding(connection, rule_code="zero_byte_file", media_file_id=file_a, library_id=library_id)
    _insert_finding(connection, rule_code="zero_byte_file", media_file_id=file_b, library_id=library_id)

    results = repo.list_findings(connection, media_file_id=file_a)
    assert len(results) == 1
    assert results[0].media_file_id == file_a


def test_list_findings_respects_limit(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    for i in range(5):
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name=f"F{i}.mkv")
        _insert_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id, library_id=library_id)

    results = repo.list_findings(connection, limit=2)
    assert len(results) == 2


def test_list_findings_combines_filters_with_and(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    _insert_finding(
        connection, rule_code="zero_byte_file", severity="ERROR", media_file_id=media_file_id, library_id=library_id
    )
    _insert_finding(
        connection, rule_code="unknown_layout", severity="WARNING", media_file_id=media_file_id, library_id=library_id
    )

    results = repo.list_findings(connection, severity="ERROR", rule_code="unknown_layout")
    assert results == []


def test_list_findings_on_empty_database_returns_empty_list(connection: sqlite3.Connection) -> None:
    assert repo.list_findings(connection) == []


# --- category/path resolution ----------------------------------------------------


def test_list_findings_resolves_category_and_path_via_join(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection, category="tv")
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(
        connection, library_id=library_id, scan_id=scan_id, name="Ep.mkv", relative_path="Show/Season 01/Ep.mkv"
    )
    _insert_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id, library_id=library_id)

    [result] = repo.list_findings(connection)
    assert result.category == "tv"
    assert result.relative_path == "Show/Season 01/Ep.mkv"
    assert result.absolute_path == "/Volumes/movies/Show/Season 01/Ep.mkv"


def test_get_finding_with_detached_media_file_id_has_no_path_but_keeps_the_row(
    connection: sqlite3.Connection,
) -> None:
    # media_file_id NULL (e.g. after a hypothetical future media_files
    # delete, ON DELETE SET NULL) -- path/category become unresolvable,
    # but the finding row and its own content survive.
    finding_id = _insert_finding(
        connection, rule_code="zero_byte_file", media_file_id=None, library_id=None, summary="orphaned"
    )
    record = repo.get_finding(connection, finding_id)
    assert record is not None
    assert record.summary == "orphaned"
    assert record.category is None
    assert record.absolute_path is None


# --- deterministic ordering --------------------------------------------------------


def test_list_findings_orders_by_file_then_severity_then_rule_declaration_order(
    connection: sqlite3.Connection,
) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="test2.mkv")
    # Insert in a deliberately scrambled order.
    _insert_finding(
        connection, rule_code="no_audio_track", severity="WARNING", media_file_id=media_file_id, library_id=library_id
    )
    _insert_finding(
        connection, rule_code="no_video_track", severity="ERROR", media_file_id=media_file_id, library_id=library_id
    )
    _insert_finding(
        connection, rule_code="zero_byte_file", severity="ERROR", media_file_id=media_file_id, library_id=library_id
    )

    results = repo.list_findings(connection)
    # Matches docs/CLI's suggested list output ordering: same file grouped
    # together, ERROR before WARNING, and within ERROR, zero_byte_file
    # before no_video_track (findings.ALL_RULES declaration order).
    assert [r.rule_code for r in results] == ["zero_byte_file", "no_video_track", "no_audio_track"]


def test_list_findings_orders_by_file_path_across_multiple_files(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    file_b = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="B.mkv")
    file_a = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    _insert_finding(connection, rule_code="zero_byte_file", media_file_id=file_b, library_id=library_id)
    _insert_finding(connection, rule_code="zero_byte_file", media_file_id=file_a, library_id=library_id)

    results = repo.list_findings(connection)
    assert [r.relative_path for r in results] == ["A.mkv", "B.mkv"]


def test_list_findings_ordering_is_stable_across_repeated_calls(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    for rule_code in ("unexpected_extension", "zero_byte_file", "unknown_layout"):
        _insert_finding(connection, rule_code=rule_code, media_file_id=media_file_id, library_id=library_id)

    first = [r.id for r in repo.list_findings(connection)]
    second = [r.id for r in repo.list_findings(connection)]
    assert first == second


# --- get_finding --------------------------------------------------------------------


def test_get_finding_returns_matching_record(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    finding_id = _insert_finding(
        connection, rule_code="zero_byte_file", media_file_id=media_file_id, library_id=library_id
    )

    record = repo.get_finding(connection, finding_id)
    assert record is not None
    assert record.id == finding_id
    assert record.rule_code == "zero_byte_file"


def test_get_finding_returns_none_for_unknown_id(connection: sqlite3.Connection) -> None:
    assert repo.get_finding(connection, 9999) is None


# --- stats --------------------------------------------------------------------------


def test_get_findings_stats_totals(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="A.mkv")
    _insert_finding(
        connection, rule_code="zero_byte_file", severity="ERROR", status="ACTIVE",
        media_file_id=media_file_id, library_id=library_id,
    )
    _insert_finding(
        connection, rule_code="no_audio_track", severity="WARNING", status="ACTIVE",
        media_file_id=media_file_id, library_id=library_id,
    )
    _insert_finding(
        connection, rule_code="unknown_layout", severity="WARNING", status="RESOLVED",
        media_file_id=media_file_id, library_id=library_id,
    )
    _insert_finding(
        connection, rule_code="metadata_error", severity="ERROR", status="IGNORED",
        media_file_id=media_file_id, library_id=library_id,
    )

    stats = repo.get_findings_stats(connection)
    assert stats.total_count == 4
    assert stats.active_count == 2
    assert stats.resolved_count == 1
    assert stats.ignored_count == 1
    # severity/rule counts are ACTIVE-only.
    assert stats.severity_counts == {"ERROR": 1, "WARNING": 1}
    assert stats.rule_counts == {"zero_byte_file": 1, "no_audio_track": 1}


def test_get_findings_stats_on_empty_database(connection: sqlite3.Connection) -> None:
    stats = repo.get_findings_stats(connection)
    assert stats.total_count == 0
    assert stats.active_count == 0
    assert stats.severity_counts == {}
    assert stats.rule_counts == {}


# --- bounded query counts -----------------------------------------------------------


def test_list_findings_uses_a_bounded_number_of_queries(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    for i in range(15):
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name=f"F{i}.mkv")
        _insert_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id, library_id=library_id)

    executed: list[str] = []
    connection.set_trace_callback(executed.append)
    try:
        results = repo.list_findings(connection)
    finally:
        connection.set_trace_callback(None)

    assert len(results) == 15
    select_statements = [sql for sql in executed if sql.strip().upper().startswith("SELECT")]
    assert len(select_statements) == 1, select_statements


def test_get_findings_stats_uses_a_bounded_number_of_queries(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    for i in range(15):
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name=f"F{i}.mkv")
        _insert_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id, library_id=library_id)

    executed = []
    connection.set_trace_callback(executed.append)
    try:
        repo.get_findings_stats(connection)
    finally:
        connection.set_trace_callback(None)

    select_statements = [sql for sql in executed if sql.strip().upper().startswith("SELECT")]
    assert len(select_statements) <= 3, select_statements
