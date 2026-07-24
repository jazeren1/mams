"""Lifecycle tests for identification candidate reconciliation
(identification_service.py / identification_repository.py): creation,
no-duplicate re-evaluation, stable id, deterministic evidence, update
after a filename/path change, MISSING-file retention, and atomic
rollback.

Builds media_files rows directly via SQL (same approach as
test_findings_lifecycle.py) so each test manipulates exactly the
condition it cares about.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from mams import identification_repository
from mams.db import connect, migrate
from mams.identification_service import evaluate_candidates

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
    connection: sqlite3.Connection,
    *,
    library_id: int,
    scan_id: int,
    filename: str = "Alien (1979).mkv",
    parent_directory: str = "/Volumes/movies",
    layout: str = "movie_flat",
    state: str = "ACTIVE",
) -> int:
    absolute_path = f"{parent_directory}/{filename}"
    cursor = connection.execute(
        """
        INSERT INTO media_files (
            library_id, absolute_path, relative_path, filename, extension,
            parent_directory, layout, size_bytes, state, first_seen_scan_id, last_seen_scan_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            library_id,
            absolute_path,
            filename,
            filename,
            ".mkv",
            parent_directory,
            layout,
            5_000_000_000,
            state,
            scan_id,
            scan_id,
        ),
    )
    return _lastrowid(cursor)


def _get_candidate(connection: sqlite3.Connection, media_file_id: int) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM identification_candidates WHERE media_file_id = ?", (media_file_id,)
    ).fetchone()


def _fixture(connection: sqlite3.Connection) -> tuple[int, int]:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
    return library_id, media_file_id


# --- creation / no duplicates / stability -----------------------------------


def test_new_candidate_is_created(connection: sqlite3.Connection) -> None:
    _, media_file_id = _fixture(connection)
    result = evaluate_candidates(connection)
    assert result.created == 1

    row = _get_candidate(connection, media_file_id)
    assert row is not None
    assert row["candidate_type"] == "MOVIE"
    assert row["parsed_title"] == "Alien"
    assert row["parsed_year"] == 1979
    assert row["confidence"] == "HIGH"


def test_repeated_evaluation_creates_no_duplicate_and_stable_id(connection: sqlite3.Connection) -> None:
    _, media_file_id = _fixture(connection)
    evaluate_candidates(connection)
    first = _get_candidate(connection, media_file_id)
    assert first is not None

    result = evaluate_candidates(connection)
    assert result.created == 0
    assert result.unchanged == 1

    rows = connection.execute(
        "SELECT * FROM identification_candidates WHERE media_file_id = ?", (media_file_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == first["id"]


def test_created_at_preserved_across_repeated_evaluation(connection: sqlite3.Connection) -> None:
    _, media_file_id = _fixture(connection)
    evaluate_candidates(connection)
    first = _get_candidate(connection, media_file_id)
    assert first is not None

    evaluate_candidates(connection)
    second = _get_candidate(connection, media_file_id)
    assert second is not None
    assert second["created_at"] == first["created_at"]


def test_updated_at_does_not_churn_on_unchanged_reevaluation(connection: sqlite3.Connection) -> None:
    _, media_file_id = _fixture(connection)
    evaluate_candidates(connection)
    first = _get_candidate(connection, media_file_id)
    assert first is not None
    connection.execute(
        "UPDATE identification_candidates SET updated_at = '2000-01-01 00:00:00' WHERE id = ?", (first["id"],)
    )

    evaluate_candidates(connection)
    second = _get_candidate(connection, media_file_id)
    assert second is not None
    assert second["updated_at"] == "2000-01-01 00:00:00"


# --- update after filename/path change ----------------------------------------


def test_candidate_updates_when_filename_changes(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, filename="Alien.mkv")
    evaluate_candidates(connection)
    first = _get_candidate(connection, media_file_id)
    assert first is not None
    assert first["parsed_year"] is None
    assert first["confidence"] == "MEDIUM"

    connection.execute(
        "UPDATE media_files SET filename = ?, relative_path = ?, absolute_path = ? WHERE id = ?",
        ("Alien (1979).mkv", "Alien (1979).mkv", "/Volumes/movies/Alien (1979).mkv", media_file_id),
    )
    result = evaluate_candidates(connection)
    assert result.updated == 1

    second = _get_candidate(connection, media_file_id)
    assert second is not None
    assert second["id"] == first["id"]
    assert second["created_at"] == first["created_at"]
    assert second["parsed_year"] == 1979
    assert second["confidence"] == "HIGH"


def test_candidate_updates_when_layout_changes(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(
        connection, library_id=library_id, scan_id=scan_id, filename="movie.mkv", parent_directory="/Volumes/movies"
    )
    evaluate_candidates(connection)
    first = _get_candidate(connection, media_file_id)
    assert first is not None
    assert first["candidate_type"] == "UNKNOWN"

    connection.execute(
        "UPDATE media_files SET layout = ?, parent_directory = ?, absolute_path = ? WHERE id = ?",
        ("movie_folder", "/Volumes/movies/Alien (1979)", "/Volumes/movies/Alien (1979)/movie.mkv", media_file_id),
    )
    evaluate_candidates(connection)
    second = _get_candidate(connection, media_file_id)
    assert second is not None
    assert second["id"] == first["id"]
    assert second["candidate_type"] == "MOVIE"
    assert second["parsed_year"] == 1979


# --- MISSING-file retention ------------------------------------------------------


def test_missing_file_candidate_is_retained_untouched(connection: sqlite3.Connection) -> None:
    _, media_file_id = _fixture(connection)
    evaluate_candidates(connection)
    first = _get_candidate(connection, media_file_id)
    assert first is not None

    connection.execute("UPDATE media_files SET state = 'MISSING' WHERE id = ?", (media_file_id,))
    result = evaluate_candidates(connection)
    # Not visited at all this run -- not created, not updated, not even
    # counted as "unchanged" (that count is for files that WERE evaluated).
    assert result.created == 0
    assert result.updated == 0
    assert result.unchanged == 0

    second = _get_candidate(connection, media_file_id)
    assert second is not None
    assert second["id"] == first["id"]
    assert second["updated_at"] == first["updated_at"]
    assert second["parsed_title"] == first["parsed_title"]


def test_restored_file_keeps_its_original_candidate(connection: sqlite3.Connection) -> None:
    _, media_file_id = _fixture(connection)
    evaluate_candidates(connection)
    first = _get_candidate(connection, media_file_id)
    assert first is not None

    connection.execute("UPDATE media_files SET state = 'MISSING' WHERE id = ?", (media_file_id,))
    evaluate_candidates(connection)
    connection.execute("UPDATE media_files SET state = 'ACTIVE' WHERE id = ?", (media_file_id,))
    evaluate_candidates(connection)

    second = _get_candidate(connection, media_file_id)
    assert second is not None
    assert second["id"] == first["id"]


# --- atomicity -----------------------------------------------------------------


def test_evaluation_failure_leaves_no_partial_writes(connection: sqlite3.Connection) -> None:
    _, media_file_id = _fixture(connection)
    evaluate_candidates(connection)
    original = _get_candidate(connection, media_file_id)
    assert original is not None

    # Force this run to both update the existing candidate (filename
    # change) and create a new one (second file) -- if the failure isn't
    # atomic, one half would land and the other wouldn't.
    connection.execute(
        "UPDATE media_files SET filename = ?, relative_path = ?, absolute_path = ? WHERE id = ?",
        ("Alien (2000).mkv", "Alien (2000).mkv", "/Volumes/movies/Alien (2000).mkv", media_file_id),
    )
    library_id = connection.execute(
        "SELECT library_id FROM media_files WHERE id = ?", (media_file_id,)
    ).fetchone()[0]
    scan_id = _insert_scan_run(connection)
    second_media_file_id = _insert_media_file(
        connection, library_id=library_id, scan_id=scan_id, filename="Terminator (1984).mkv"
    )

    with mock.patch.object(identification_repository, "_insert_candidate", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            evaluate_candidates(connection)

    still_original = _get_candidate(connection, media_file_id)
    assert still_original is not None
    assert still_original["parsed_year"] == original["parsed_year"]
    assert still_original["updated_at"] == original["updated_at"]

    new_row = _get_candidate(connection, second_media_file_id)
    assert new_row is None


# --- deterministic evidence_json --------------------------------------------------


def test_evidence_json_is_deterministic_across_independent_runs(tmp_path: Path) -> None:
    counter = iter(range(2))

    def run() -> str:
        db_path = tmp_path / f"mams_{next(counter)}.db"
        migrate(db_path, REPO_MIGRATIONS_DIR)
        conn = connect(db_path)
        try:
            library_id = _insert_library(conn)
            scan_id = _insert_scan_run(conn)
            media_file_id = _insert_media_file(conn, library_id=library_id, scan_id=scan_id)
            evaluate_candidates(conn)
            row = _get_candidate(conn, media_file_id)
            assert row is not None
            return row["evidence_json"]
        finally:
            conn.close()

    first = run()
    second = run()
    assert first == second
    assert json.loads(first)["title_source"] == "filename"
