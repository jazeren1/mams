from __future__ import annotations

from pathlib import Path

import yaml

from mams.cli import build_parser, main
from mams.db import connect


def _write_config(tmp_path: Path, database_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "MAMS Test",
                    "database_path": str(database_path),
                    "log_level": "INFO",
                    "dry_run": True,
                },
                "nas": {"categories": {}},
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_parser_defaults_for_init_db() -> None:
    parser = build_parser()

    args = parser.parse_args(["init-db"])

    assert args.command == "init-db"
    assert args.migrations_dir == "database/migrations"


def test_init_db_applies_migrations(tmp_path: Path, monkeypatch, capsys) -> None:
    database_path = tmp_path / "database" / "mams.db"
    config_path = _write_config(tmp_path, database_path)
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "0001_test.sql").write_text(
        "CREATE TABLE IF NOT EXISTS widgets (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )

    monkeypatch.setattr(
        "sys.argv",
        ["mams", "--config", str(config_path), "init-db", "--migrations-dir", str(migrations_dir)],
    )
    main()

    with connect(database_path) as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "widgets" in tables

    output = capsys.readouterr().out
    assert "Applied migrations" in output


def test_init_db_is_idempotent_on_rerun(tmp_path: Path, monkeypatch, capsys) -> None:
    database_path = tmp_path / "database" / "mams.db"
    config_path = _write_config(tmp_path, database_path)
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "0001_test.sql").write_text(
        "CREATE TABLE IF NOT EXISTS widgets (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    argv = ["mams", "--config", str(config_path), "init-db", "--migrations-dir", str(migrations_dir)]

    monkeypatch.setattr("sys.argv", argv)
    main()
    capsys.readouterr()
    main()

    output = capsys.readouterr().out
    assert "already up to date" in output
