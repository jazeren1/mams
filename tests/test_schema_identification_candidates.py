"""Tests for the identification_candidates schema
(database/migrations/0005_identification_candidates.sql).

Exercises the migration file directly against a temp database, same
approach as test_schema_findings.py, so a constraint typo in the .sql file
itself gets caught.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mams.db import connect, current_schema_version, migrate

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"


def _migrations_dir_through(tmp_path: Path, *versions: str) -> Path:
    """A temp migrations dir containing only the named repo migration files.

    Scopes this file's tests to exactly the migrations it's testing (up
    through 0005), so a later migration (0006+) added elsewhere never
    changes the schema_version this file expects.
    """
    scoped = tmp_path / "scoped_migrations"
    scoped.mkdir(exist_ok=True)
    for version in versions:
        matches = list(REPO_MIGRATIONS_DIR.glob(f"{version}_*.sql"))
        assert len(matches) == 1, f"expected exactly one migration file for {version}"
        (scoped / matches[0].name).write_text(matches[0].read_text(encoding="utf-8"), encoding="utf-8")
    return scoped


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "mams.db"
    migrate(path, _migrations_dir_through(tmp_path, "0001", "0002", "0003", "0004", "0005"))
    return path


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
    connection: sqlite3.Connection, *, library_id: int, scan_id: int, name: str = "Movie (2001).mkv"
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO media_files (
            library_id, absolute_path, relative_path, filename, extension,
            parent_directory, layout, size_bytes, first_seen_scan_id, last_seen_scan_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            library_id,
            f"/Volumes/movies/{name}",
            name,
            name,
            ".mkv",
            "/Volumes/movies",
            "movie_flat",
            1234,
            scan_id,
            scan_id,
        ),
    )
    return _lastrowid(cursor)


def _insert_candidate(
    connection: sqlite3.Connection,
    *,
    media_file_id: int,
    candidate_type: str = "MOVIE",
    confidence: str = "HIGH",
    parser_version: int = 1,
    parsed_title: str | None = "Movie",
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO identification_candidates (
            media_file_id, candidate_type, parsed_title, confidence, parser_version
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (media_file_id, candidate_type, parsed_title, confidence, parser_version),
    )
    return _lastrowid(cursor)


def test_migrate_applies_identification_candidates_schema(db_path: Path) -> None:
    with connect(db_path) as connection:
        assert current_schema_version(connection) == 5
        tables = {
            row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "identification_candidates" in tables


def test_candidate_defaults(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        candidate_id = _insert_candidate(connection, media_file_id=media_file_id)

        row = connection.execute(
            "SELECT * FROM identification_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        assert row["created_at"] is not None
        assert row["updated_at"] is not None
        assert row["parsed_year"] is None
        assert row["evidence_json"] is None


@pytest.mark.parametrize("candidate_type", ["MOVIE", "EPISODE", "SPECIAL", "EXTRA", "UNKNOWN"])
def test_accepts_all_valid_candidate_types(db_path: Path, candidate_type: str) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        candidate_id = _insert_candidate(connection, media_file_id=media_file_id, candidate_type=candidate_type)
        row = connection.execute(
            "SELECT candidate_type FROM identification_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        assert row["candidate_type"] == candidate_type


def test_rejects_invalid_candidate_type(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_candidate(connection, media_file_id=media_file_id, candidate_type="TRAILER")


@pytest.mark.parametrize("confidence", ["HIGH", "MEDIUM", "LOW", "UNKNOWN"])
def test_accepts_all_valid_confidence_levels(db_path: Path, confidence: str) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        candidate_id = _insert_candidate(connection, media_file_id=media_file_id, confidence=confidence)
        row = connection.execute(
            "SELECT confidence FROM identification_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        assert row["confidence"] == confidence


def test_rejects_invalid_confidence(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_candidate(connection, media_file_id=media_file_id, confidence="CERTAIN")


def test_requires_media_file_id_candidate_type_confidence_parser_version(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO identification_candidates (candidate_type, confidence, parser_version) "
                "VALUES ('MOVIE', 'HIGH', 1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO identification_candidates (media_file_id, confidence, parser_version) VALUES (?, 'HIGH', 1)",
                (media_file_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO identification_candidates (media_file_id, candidate_type, parser_version) "
                "VALUES (?, 'MOVIE', 1)",
                (media_file_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO identification_candidates (media_file_id, candidate_type, confidence) "
                "VALUES (?, 'MOVIE', 'HIGH')",
                (media_file_id,),
            )


def test_uniqueness_rejects_second_candidate_for_same_media_file(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        _insert_candidate(connection, media_file_id=media_file_id)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_candidate(connection, media_file_id=media_file_id)


def test_uniqueness_allows_candidates_for_different_media_files(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id_1 = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="One.mkv")
        media_file_id_2 = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, name="Two.mkv")
        _insert_candidate(connection, media_file_id=media_file_id_1)
        _insert_candidate(connection, media_file_id=media_file_id_2)
        count = connection.execute("SELECT COUNT(*) FROM identification_candidates").fetchone()[0]
        assert count == 2


def test_deleting_media_file_cascades_to_candidate(db_path: Path) -> None:
    """FK retention deliberately differs from scan_changes/findings: a
    candidate is a live interpretation of a still-existing file, not
    historical evidence, so it is deleted along with its file rather than
    detached and preserved. See the migration file's header comment."""
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        candidate_id = _insert_candidate(connection, media_file_id=media_file_id)

        connection.execute("DELETE FROM media_files WHERE id = ?", (media_file_id,))

        row = connection.execute(
            "SELECT * FROM identification_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        assert row is None


@pytest.mark.parametrize(
    "index_name",
    [
        "idx_identification_candidates_media_file_id",
        "idx_identification_candidates_type",
        "idx_identification_candidates_confidence",
        "idx_identification_candidates_season_number",
        "idx_identification_candidates_parsed_year",
    ],
)
def test_required_index_exists(db_path: Path, index_name: str) -> None:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name = ?", (index_name,)
        ).fetchone()
        assert row is not None, f"missing index {index_name}"


def test_migrate_from_scratch_applies_at_least_through_0005(tmp_path: Path) -> None:
    """Sanity-checks the real repo migrations dir specifically (unlike the
    other tests in this file, which use a scoped copy so they don't break
    when a later migration is added). Only asserts a prefix, so this stays
    true after a future 0006+ lands."""
    path = tmp_path / "mams.db"
    applied = migrate(path, REPO_MIGRATIONS_DIR)
    assert applied[:5] == [1, 2, 3, 4, 5]


def test_migrate_is_idempotent_after_0005(tmp_path: Path, db_path: Path) -> None:
    migrations_dir = _migrations_dir_through(tmp_path, "0001", "0002", "0003", "0004", "0005")
    applied_again = migrate(db_path, migrations_dir)
    assert applied_again == []
