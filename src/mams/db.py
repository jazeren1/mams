from __future__ import annotations
import sqlite3
from pathlib import Path

def connect(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection

def initialize(database_path: str | Path, schema_path: str | Path) -> None:
    schema = Path(schema_path).read_text(encoding="utf-8")
    with connect(database_path) as connection:
        connection.executescript(schema)
