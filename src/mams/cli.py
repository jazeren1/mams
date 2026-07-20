from __future__ import annotations
import argparse
from pathlib import Path
from rich.console import Console
from .config import load_config, AppConfig
from .db import DEFAULT_MIGRATIONS_DIR, migrate
from . import inventory
from . import mediainfo
console = Console()

DEFAULT_INVENTORY_REPORT = "reports/library.json"

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mams")
    parser.add_argument("--config", default="config/config.yaml")
    subs = parser.add_subparsers(dest="command", required=True)
    init_parser = subs.add_parser("init-db")
    init_parser.add_argument("--migrations-dir", default=DEFAULT_MIGRATIONS_DIR)
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
    scan_parser.add_argument(
        "--metadata",
        action="store_true",
        help="Enrich each discovered file with MediaInfo technical metadata (slower; requires mediainfo).",
    )

    mediainfo_parser = subs.add_parser(
        "mediainfo", help="Show parsed MediaInfo metadata for a single file. Diagnostic only; read-only."
    )
    mediainfo_parser.add_argument("path", help="Path to a single media file.")
    mediainfo_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw parsed JSON instead of a formatted summary.",
    )
    return parser

def run_inventory_scan(
    config: AppConfig, *, json_output: bool, output: str, metadata: bool = False
) -> inventory.InventoryReport:
    """Scan configured NAS categories and write JSON + summary reports.

    Read-only: this only reads directory entries and file sizes (and, with
    `metadata=True`, invokes the read-only `mediainfo` tool), and writes the
    two report files below. It never touches the scanned media.
    """
    provider = mediainfo.MediaInfoProvider() if metadata else None
    report = inventory.scan_categories(config.nas_categories, metadata_provider=provider)

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


def run_mediainfo(path: str, *, json_output: bool) -> mediainfo.MediaInfoOutcome:
    """Diagnostic command: parse and display MediaInfo for a single file.

    Uses the same `MediaInfoProvider` and parser as `inventory scan
    --metadata`, so behavior matches what the scanner would record. Never
    modifies the target file.
    """
    provider = mediainfo.MediaInfoProvider()
    outcome = provider.probe(Path(path))
    if not outcome.ok:
        console.print(f"[red]MediaInfo error:[/red] {outcome.error}")
        return outcome

    assert outcome.media_info is not None
    if json_output:
        console.print_json(outcome.media_info.to_json())
    else:
        console.print(mediainfo.render_media_info(outcome.media_info))
    return outcome

def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    if args.command == "init-db":
        applied = migrate(config.database_path, Path(args.migrations_dir))
        if applied:
            versions = ", ".join(str(version) for version in applied)
            console.print(f"[green]Applied migrations:[/green] {versions} -> {config.database_path}")
        else:
            console.print(f"[dim]Database already up to date:[/dim] {config.database_path}")
    elif args.command == "status":
        console.print(f"Database: {config.database_path}")
        console.print(f"Dry run: {config.dry_run}")
    elif args.command == "inventory":
        if args.inventory_command == "scan":
            run_inventory_scan(config, json_output=args.json, output=args.output, metadata=args.metadata)
    elif args.command == "mediainfo":
        run_mediainfo(args.path, json_output=args.json)

if __name__ == "__main__":
    main()
