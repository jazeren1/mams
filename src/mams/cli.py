from __future__ import annotations
import argparse
from pathlib import Path
from rich.console import Console
from .config import load_config
from .db import initialize
console = Console()

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mams")
    parser.add_argument("--config", default="config/config.yaml")
    subs = parser.add_subparsers(dest="command", required=True)
    init_parser = subs.add_parser("init-db")
    init_parser.add_argument("--schema", default="database/schema.sql")
    subs.add_parser("status")
    return parser

def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    if args.command == "init-db":
        initialize(config.database_path, Path(args.schema))
        console.print(f"[green]Initialized database:[/green] {config.database_path}")
    elif args.command == "status":
        console.print(f"Database: {config.database_path}")
        console.print(f"Dry run: {config.dry_run}")

if __name__ == "__main__":
    main()
