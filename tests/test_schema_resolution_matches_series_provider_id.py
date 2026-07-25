"""Tests for the resolution_matches.series_provider_id column
(database/migrations/0010_resolution_matches_series_provider_id.sql).
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


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "mams.db"
    migrate(
        path,
        _migrations_dir_through(
            tmp_path, "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010"
        ),
    )
    return path


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _insert_attempt(connection: sqlite3.Connection) -> int:
    cursor = connection.execute(
        "INSERT INTO resolution_attempts (provider, status, algorithm_version) VALUES ('TMDB', 'PENDING', 1)"
    )
    return _lastrowid(cursor)


def test_migrate_applies_series_provider_id_column(db_path: Path) -> None:
    with connect(db_path) as connection:
        assert current_schema_version(connection) == 10
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(resolution_matches)")}
        assert "series_provider_id" in columns


def test_series_provider_id_defaults_to_null(db_path: Path) -> None:
    with connect(db_path) as connection:
        attempt_id = _insert_attempt(connection)
        cursor = connection.execute(
            """
            INSERT INTO resolution_matches (
                resolution_attempt_id, provider, provider_media_type, provider_id, score, rank, scoring_json
            ) VALUES (?, 'TMDB', 'MOVIE', 1, 0.9, 1, '{}')
            """,
            (attempt_id,),
        )
        match_id = _lastrowid(cursor)
        row = connection.execute(
            "SELECT series_provider_id FROM resolution_matches WHERE id = ?", (match_id,)
        ).fetchone()
        assert row["series_provider_id"] is None


def test_series_provider_id_can_be_set_for_episode_matches(db_path: Path) -> None:
    with connect(db_path) as connection:
        attempt_id = _insert_attempt(connection)
        cursor = connection.execute(
            """
            INSERT INTO resolution_matches (
                resolution_attempt_id, provider, provider_media_type, provider_id, series_provider_id,
                season_number, episode_number, score, rank, scoring_json
            ) VALUES (?, 'TMDB', 'EPISODE', 62085, 1396, 1, 1, 0.95, 1, '{}')
            """,
            (attempt_id,),
        )
        match_id = _lastrowid(cursor)
        row = connection.execute(
            "SELECT series_provider_id FROM resolution_matches WHERE id = ?", (match_id,)
        ).fetchone()
        assert row["series_provider_id"] == 1396


def test_required_index_exists(db_path: Path) -> None:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name = 'idx_resolution_matches_series_provider_id'"
        ).fetchone()
        assert row is not None


def test_migrate_from_scratch_applies_at_least_through_0010(tmp_path: Path) -> None:
    path = tmp_path / "mams.db"
    applied = migrate(path, REPO_MIGRATIONS_DIR)
    assert applied[:10] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


def test_migrate_is_idempotent_after_0010(tmp_path: Path, db_path: Path) -> None:
    migrations_dir = _migrations_dir_through(
        tmp_path, "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010"
    )
    applied_again = migrate(db_path, migrations_dir)
    assert applied_again == []
