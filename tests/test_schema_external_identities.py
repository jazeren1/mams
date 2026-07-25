"""Tests for the external_identities schema
(database/migrations/0007_external_identities.sql).
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
    migrate(path, _migrations_dir_through(tmp_path, "0001", "0002", "0003", "0004", "0005", "0006", "0007"))
    return path


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _insert_identity(
    connection: sqlite3.Connection,
    *,
    provider: str = "TMDB",
    media_type: str = "MOVIE",
    provider_id: int = 348,
    title: str | None = "Alien",
    release_year: int | None = 1979,
    series_provider_id: int | None = None,
    season_number: int | None = None,
    episode_number: int | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO external_identities (
            provider, media_type, provider_id, title, release_year,
            series_provider_id, season_number, episode_number
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (provider, media_type, provider_id, title, release_year, series_provider_id, season_number, episode_number),
    )
    return _lastrowid(cursor)


def test_migrate_applies_external_identities_schema(db_path: Path) -> None:
    with connect(db_path) as connection:
        assert current_schema_version(connection) == 7
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "external_identities" in tables


def test_identity_defaults(db_path: Path) -> None:
    with connect(db_path) as connection:
        identity_id = _insert_identity(connection)
        row = connection.execute("SELECT * FROM external_identities WHERE id = ?", (identity_id,)).fetchone()
        assert row["created_at"] is not None
        assert row["updated_at"] is not None
        assert row["original_title"] is None


@pytest.mark.parametrize("media_type", ["MOVIE", "SERIES", "EPISODE", "SPECIAL"])
def test_accepts_all_valid_media_types(db_path: Path, media_type: str) -> None:
    with connect(db_path) as connection:
        identity_id = _insert_identity(connection, media_type=media_type)
        row = connection.execute(
            "SELECT media_type FROM external_identities WHERE id = ?", (identity_id,)
        ).fetchone()
        assert row["media_type"] == media_type


def test_rejects_invalid_media_type(db_path: Path) -> None:
    with connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_identity(connection, media_type="TRAILER")


def test_rejects_invalid_provider(db_path: Path) -> None:
    with connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_identity(connection, provider="TVDB")


def test_requires_provider_media_type_provider_id(db_path: Path) -> None:
    with connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO external_identities (media_type, provider_id) VALUES ('MOVIE', 1)")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO external_identities (provider, provider_id) VALUES ('TMDB', 1)")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO external_identities (provider, media_type) VALUES ('TMDB', 'MOVIE')")


def test_uniqueness_rejects_duplicate_provider_type_id(db_path: Path) -> None:
    with connect(db_path) as connection:
        _insert_identity(connection, media_type="MOVIE", provider_id=348)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_identity(connection, media_type="MOVIE", provider_id=348)


def test_uniqueness_allows_same_provider_id_across_different_media_types(db_path: Path) -> None:
    """TMDb id namespaces are per-endpoint -- a movie id and a tv id can
    numerically collide, so media_type must be part of the natural key."""
    with connect(db_path) as connection:
        _insert_identity(connection, media_type="MOVIE", provider_id=550)
        _insert_identity(connection, media_type="SERIES", provider_id=550)
        count = connection.execute("SELECT COUNT(*) FROM external_identities").fetchone()[0]
        assert count == 2


def test_episode_identity_denormalizes_series_provider_id(db_path: Path) -> None:
    with connect(db_path) as connection:
        identity_id = _insert_identity(
            connection,
            media_type="EPISODE",
            provider_id=9999,
            title=None,
            release_year=None,
            series_provider_id=1396,
            season_number=1,
            episode_number=1,
        )
        row = connection.execute("SELECT * FROM external_identities WHERE id = ?", (identity_id,)).fetchone()
        assert row["series_provider_id"] == 1396
        assert row["season_number"] == 1
        assert row["episode_number"] == 1


@pytest.mark.parametrize(
    "index_name",
    [
        "idx_external_identities_provider_type_id",
        "idx_external_identities_series_provider_id",
        "idx_external_identities_media_type",
        "idx_external_identities_release_year",
    ],
)
def test_required_index_exists(db_path: Path, index_name: str) -> None:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name = ?", (index_name,)
        ).fetchone()
        assert row is not None, f"missing index {index_name}"


def test_migrate_from_scratch_applies_at_least_through_0007(tmp_path: Path) -> None:
    path = tmp_path / "mams.db"
    applied = migrate(path, REPO_MIGRATIONS_DIR)
    assert applied[:7] == [1, 2, 3, 4, 5, 6, 7]


def test_migrate_is_idempotent_after_0007(tmp_path: Path, db_path: Path) -> None:
    migrations_dir = _migrations_dir_through(tmp_path, "0001", "0002", "0003", "0004", "0005", "0006", "0007")
    applied_again = migrate(db_path, migrations_dir)
    assert applied_again == []
