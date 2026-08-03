"""Tests for media_identity_assignments.confirmed_for_ingest_at/confirmed_by
(database/migrations/0016_media_identity_assignment_confirmation.sql).

Exercises the migration file directly against a temp database, same
approach as test_schema_scan_scope.py, and specifically checks both a
fresh database (every migration applied from scratch) and an upgrade
from an existing 0.8.2 database (migrated only through 0015, matching
production before this fix) so a crash-unsafe ALTER TABLE doesn't
silently corrupt pre-existing assignment rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mams.db import connect, current_schema_version, migrate

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"


def _migrations_dir_through(tmp_path: Path, *versions: str) -> Path:
    scoped = tmp_path / "scoped_migrations"
    scoped.mkdir(exist_ok=True)
    for version in versions:
        matches = list(REPO_MIGRATIONS_DIR.glob(f"{version}_*.sql"))
        assert len(matches) == 1, f"expected exactly one migration file for {version}"
        (scoped / matches[0].name).write_text(matches[0].read_text(encoding="utf-8"), encoding="utf-8")
    return scoped


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _seed_assignment_dependencies(connection: sqlite3.Connection) -> tuple[int, int]:
    """Insert the minimum rows required for a media_identity_assignments
    row: a library, a scan_run, a media_file, a candidate, an external
    identity. Returns (media_file_id, candidate_id)."""
    library_id = _lastrowid(
        connection.execute("INSERT INTO libraries (category, root_path) VALUES ('movies', '/Volumes/movies')")
    )
    scan_id = _lastrowid(connection.execute("INSERT INTO scan_runs DEFAULT VALUES"))
    media_file_id = _lastrowid(
        connection.execute(
            """
            INSERT INTO media_files (
                library_id, absolute_path, relative_path, filename, extension,
                parent_directory, layout, size_bytes, first_seen_scan_id, last_seen_scan_id
            ) VALUES (?, '/Volumes/movies/Alien 3 (1992).mkv', 'Alien 3 (1992).mkv', 'Alien 3 (1992).mkv', '.mkv',
                      '/Volumes/movies', 'movie_flat', 1234, ?, ?)
            """,
            (library_id, scan_id, scan_id),
        )
    )
    candidate_id = _lastrowid(
        connection.execute(
            "INSERT INTO identification_candidates (media_file_id, candidate_type, parsed_title, confidence, parser_version) "
            "VALUES (?, 'MOVIE', 'Alien 3', 'HIGH', 1)",
            (media_file_id,),
        )
    )
    return media_file_id, candidate_id


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "mams.db"
    migrate(path, REPO_MIGRATIONS_DIR)
    return path


def test_migrate_applies_confirmation_schema(db_path: Path) -> None:
    with connect(db_path) as connection:
        assert current_schema_version(connection) == 16


def test_fresh_assignment_defaults_confirmation_columns_to_null(db_path: Path) -> None:
    with connect(db_path) as connection:
        media_file_id, candidate_id = _seed_assignment_dependencies(connection)
        identity_id = _lastrowid(
            connection.execute(
                "INSERT INTO external_identities (provider, media_type, provider_id, title, release_year) "
                "VALUES ('TMDB', 'MOVIE', 8077, 'Alien³', 1992)"
            )
        )
        assignment_id = _lastrowid(
            connection.execute(
                "INSERT INTO media_identity_assignments ("
                "media_file_id, identification_candidate_id, external_identity_id, assignment_method, confidence"
                ") VALUES (?, ?, ?, 'MANUAL', 'MEDIUM')",
                (media_file_id, candidate_id, identity_id),
            )
        )
        row = connection.execute(
            "SELECT confirmed_for_ingest_at, confirmed_by FROM media_identity_assignments WHERE id = ?", (assignment_id,)
        ).fetchone()
        assert row["confirmed_for_ingest_at"] is None
        assert row["confirmed_by"] is None


def test_upgrade_from_0_8_2_preserves_existing_assignment_rows(tmp_path: Path) -> None:
    """Simulates the real production upgrade path: a database already at
    schema version 15 (0.8.2, before this fix) with a real MANUAL
    assignment row, then migrated forward to 16. The pre-existing row
    must survive untouched with the two new columns NULL."""
    path = tmp_path / "mams.db"
    versions_through_15 = [f"{n:04d}" for n in range(1, 16)]
    migrate(path, _migrations_dir_through(tmp_path, *versions_through_15))

    with connect(path) as connection:
        assert current_schema_version(connection) == 15
        media_file_id, candidate_id = _seed_assignment_dependencies(connection)
        identity_id = _lastrowid(
            connection.execute(
                "INSERT INTO external_identities (provider, media_type, provider_id, title, release_year) "
                "VALUES ('TMDB', 'MOVIE', 8077, 'Alien³', 1992)"
            )
        )
        assignment_id = _lastrowid(
            connection.execute(
                "INSERT INTO media_identity_assignments ("
                "media_file_id, identification_candidate_id, external_identity_id, assignment_method, confidence"
                ") VALUES (?, ?, ?, 'MANUAL', 'MEDIUM')",
                (media_file_id, candidate_id, identity_id),
            )
        )

    applied = migrate(path, REPO_MIGRATIONS_DIR)
    assert 16 in applied

    with connect(path) as connection:
        assert current_schema_version(connection) == 16
        row = connection.execute(
            "SELECT media_file_id, assignment_method, confidence, status, confirmed_for_ingest_at, confirmed_by "
            "FROM media_identity_assignments WHERE id = ?",
            (assignment_id,),
        ).fetchone()
        assert row["media_file_id"] == media_file_id
        assert row["assignment_method"] == "MANUAL"
        assert row["confidence"] == "MEDIUM"
        assert row["status"] == "ACTIVE"
        assert row["confirmed_for_ingest_at"] is None
        assert row["confirmed_by"] is None


def test_migrate_from_scratch_applies_at_least_through_0016(tmp_path: Path) -> None:
    path = tmp_path / "mams.db"
    applied = migrate(path, REPO_MIGRATIONS_DIR)
    assert 16 in applied


def test_migrate_is_idempotent_after_0016(tmp_path: Path, db_path: Path) -> None:
    versions = [f"{n:04d}" for n in range(1, 17)]
    migrations_dir = _migrations_dir_through(tmp_path, *versions)
    applied_again = migrate(db_path, migrations_dir)
    assert applied_again == []
