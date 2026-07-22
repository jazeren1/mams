"""Tests for the findings schema (database/migrations/0004_findings.sql).

Exercises the migration file directly against a temp database, same
approach as test_schema_scan_changes.py, so a constraint typo in the .sql
file itself gets caught.
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
    through 0004), so a later migration (0005+) added elsewhere never
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
    migrate(path, _migrations_dir_through(tmp_path, "0001", "0002", "0003", "0004"))
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


def _insert_media_file(connection: sqlite3.Connection, *, library_id: int, scan_id: int) -> int:
    cursor = connection.execute(
        """
        INSERT INTO media_files (
            library_id, absolute_path, relative_path, filename, extension,
            parent_directory, layout, size_bytes, first_seen_scan_id, last_seen_scan_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            library_id,
            "/Volumes/movies/Movie (2001).mkv",
            "Movie (2001).mkv",
            "Movie (2001).mkv",
            ".mkv",
            "/Volumes/movies",
            "movie_flat",
            1234,
            scan_id,
            scan_id,
        ),
    )
    return _lastrowid(cursor)


def _insert_finding(
    connection: sqlite3.Connection,
    *,
    rule_code: str = "zero_byte_file",
    severity: str = "ERROR",
    status: str = "ACTIVE",
    media_file_id: int | None,
    library_id: int | None,
    summary: str = "File is zero bytes.",
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO findings (rule_code, severity, status, media_file_id, library_id, summary)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (rule_code, severity, status, media_file_id, library_id, summary),
    )
    return _lastrowid(cursor)


def test_migrate_applies_findings_schema(db_path: Path) -> None:
    with connect(db_path) as connection:
        assert current_schema_version(connection) == 4
        tables = {
            row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "findings" in tables


def test_finding_defaults(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        finding_id = _insert_finding(connection, media_file_id=media_file_id, library_id=library_id)

        row = connection.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
        assert row["status"] == "ACTIVE"
        assert row["resolved_at"] is None
        assert row["first_detected_at"] is not None
        assert row["last_detected_at"] is not None
        assert row["created_at"] is not None
        assert row["updated_at"] is not None
        assert row["evidence_json"] is None
        assert row["recommendation"] is None


@pytest.mark.parametrize("severity", ["INFO", "WARNING", "ERROR", "CRITICAL"])
def test_findings_accepts_all_valid_severities(db_path: Path, severity: str) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        finding_id = _insert_finding(
            connection, media_file_id=media_file_id, library_id=library_id, severity=severity
        )
        row = connection.execute("SELECT severity FROM findings WHERE id = ?", (finding_id,)).fetchone()
        assert row["severity"] == severity


def test_findings_rejects_invalid_severity(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_finding(connection, media_file_id=media_file_id, library_id=library_id, severity="SEVERE")


@pytest.mark.parametrize("status", ["ACTIVE", "RESOLVED", "IGNORED"])
def test_findings_accepts_all_valid_statuses(db_path: Path, status: str) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        finding_id = _insert_finding(
            connection, media_file_id=media_file_id, library_id=library_id, status=status
        )
        row = connection.execute("SELECT status FROM findings WHERE id = ?", (finding_id,)).fetchone()
        assert row["status"] == status


def test_findings_rejects_invalid_status(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_finding(connection, media_file_id=media_file_id, library_id=library_id, status="PENDING")


def test_findings_requires_rule_code_and_severity_and_summary(db_path: Path) -> None:
    with connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO findings (severity, summary) VALUES ('ERROR', 'x')")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO findings (rule_code, summary) VALUES ('zero_byte_file', 'x')")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO findings (rule_code, severity) VALUES ('zero_byte_file', 'ERROR')")


def test_uniqueness_rejects_duplicate_rule_code_and_media_file_id(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        _insert_finding(connection, media_file_id=media_file_id, library_id=library_id)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_finding(connection, media_file_id=media_file_id, library_id=library_id)


def test_uniqueness_allows_same_rule_code_for_different_media_files(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id_1 = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        connection.execute(
            "UPDATE media_files SET absolute_path = '/Volumes/movies/Other.mkv' WHERE id = ?", (media_file_id_1,)
        )
        media_file_id_2 = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        _insert_finding(connection, media_file_id=media_file_id_1, library_id=library_id)
        # Should not raise -- same rule_code, different media_file_id.
        _insert_finding(connection, media_file_id=media_file_id_2, library_id=library_id)
        count = connection.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
        assert count == 2


def test_deleting_media_file_sets_findings_media_file_id_null_but_preserves_the_row(db_path: Path) -> None:
    """FK retention behavior mirrors scan_changes: findings are historical
    evidence, so a media_files delete must detach, not cascade-delete."""
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        scan_id = _insert_scan_run(connection)
        media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id)
        finding_id = _insert_finding(connection, media_file_id=media_file_id, library_id=library_id)

        connection.execute("DELETE FROM media_files WHERE id = ?", (media_file_id,))

        row = connection.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
        assert row is not None
        assert row["media_file_id"] is None
        assert row["summary"] == "File is zero bytes."


def test_deleting_library_sets_findings_library_id_null_but_preserves_the_row(db_path: Path) -> None:
    with connect(db_path) as connection:
        library_id = _insert_library(connection)
        # No media_files row references this library -- media_files.library_id
        # has no ON DELETE clause (default RESTRICT), so a referencing file
        # would block the library delete before findings' own FK is reached.
        finding_id = _insert_finding(connection, media_file_id=None, library_id=library_id)

        connection.execute("DELETE FROM libraries WHERE id = ?", (library_id,))

        row = connection.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
        assert row is not None
        assert row["library_id"] is None


@pytest.mark.parametrize(
    "index_name",
    [
        "idx_findings_rule_media_file",
        "idx_findings_status",
        "idx_findings_severity",
        "idx_findings_rule_code",
        "idx_findings_media_file_id",
        "idx_findings_library_id",
    ],
)
def test_required_index_exists(db_path: Path, index_name: str) -> None:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name = ?", (index_name,)
        ).fetchone()
        assert row is not None, f"missing index {index_name}"


def test_migrate_from_scratch_applies_at_least_through_0004(tmp_path: Path) -> None:
    """Sanity-checks the real repo migrations dir specifically (unlike the
    other tests in this file, which use a scoped copy so they don't break
    when a later migration is added). Only asserts a prefix, so this stays
    true after a future 0005+ lands."""
    path = tmp_path / "mams.db"
    applied = migrate(path, REPO_MIGRATIONS_DIR)
    assert applied[:4] == [1, 2, 3, 4]


def test_migrate_is_idempotent_after_0004(tmp_path: Path, db_path: Path) -> None:
    migrations_dir = _migrations_dir_through(tmp_path, "0001", "0002", "0003", "0004")
    applied_again = migrate(db_path, migrations_dir)
    assert applied_again == []
