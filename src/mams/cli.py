from __future__ import annotations
import argparse
from pathlib import Path
from rich.console import Console
from .config import load_config, AppConfig
from .db import initialize
from . import inventory
console = Console()

DEFAULT_INVENTORY_REPORT = "reports/library.json"

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mams")
    parser.add_argument("--config", default="config/config.yaml")
    subs = parser.add_subparsers(dest="command", required=True)
    init_parser = subs.add_parser("init-db")
    init_parser.add_argument("--schema", default="database/schema.sql")
    subs.add_parser("status")

    inventory_parser = subs.add_parser("inventory")
    inventory_subs = inventory_parser.add_subparsers(dest="inventory_command", required=True)
    scan_parser = inventory_subs.add_parser("scan")
    scan_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the JSON report to stdout instead of the human-readable summary.",
    )
    scan_parser.add_argument(
        "--output",
        default=DEFAULT_INVENTORY_REPORT,
        help=f"Path to write the JSON report (default: {DEFAULT_INVENTORY_REPORT}).",
    )
    return parser

def run_inventory_scan(config: AppConfig, *, json_output: bool, output: str) -> inventory.InventoryReport:
    """Scan configured NAS categories and write JSON + summary reports.

    Read-only: this only reads directory entries and file sizes, and writes
    the two report files below. It never touches the scanned media.
    """
    report = inventory.scan_categories(config.nas_categories)

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report.to_json(), encoding="utf-8")

    summary_path = output_path.with_name(f"{output_path.stem}-summary.txt")
    summary_text = inventory.render_summary(report)
    summary_path.write_text(summary_text, encoding="utf-8")

    if json_output:
        console.print_json(report.to_json())
    else:
        console.print(summary_text)
    console.print(f"\n[dim]JSON report:    {output_path}[/dim]")
    console.print(f"[dim]Summary report: {summary_path}[/dim]")
    return report

def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    if args.command == "init-db":
        initialize(config.database_path, Path(args.schema))
        console.print(f"[green]Initialized database:[/green] {config.database_path}")
    elif args.command == "status":
        console.print(f"Database: {config.database_path}")
        console.print(f"Dry run: {config.dry_run}")
    elif args.command == "inventory":
        if args.inventory_command == "scan":
            run_inventory_scan(config, json_output=args.json, output=args.output)

if __name__ == "__main__":
    main()
