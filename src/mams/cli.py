from __future__ import annotations
import argparse
import sqlite3
from pathlib import Path
from typing import cast
from rich.console import Console
from .config import load_config, AppConfig
from .db import DEFAULT_MIGRATIONS_DIR, connect, migrate
from . import inventory
from . import inventory_repository
from . import mediainfo
from . import findings_repository
from . import findings_service
from . import identification_repository
from . import identification_service
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
    scan_parser.add_argument(
        "--no-db",
        action="store_true",
        help="Skip database persistence; only write the JSON/summary reports.",
    )

    list_parser = inventory_subs.add_parser("list", help="List media_files rows from the database. Read-only.")
    list_parser.add_argument("--category", default=None)
    list_parser.add_argument("--state", type=str.upper, choices=["ACTIVE", "MISSING"], default=None)
    list_parser.add_argument("--layout", default=None)
    list_parser.add_argument(
        "--metadata-error",
        dest="metadata_error",
        action="store_true",
        default=None,
        help="Only show files with a recorded MediaInfo probe error.",
    )
    list_parser.add_argument("--limit", type=int, default=None)
    list_parser.add_argument("--json", action="store_true")

    stats_parser = inventory_subs.add_parser("stats", help="Show current inventory statistics. Read-only.")
    stats_parser.add_argument("--json", action="store_true")

    find_parser = inventory_subs.add_parser(
        "find", help="Case-insensitive search over filename/path in the database. Read-only."
    )
    find_parser.add_argument("query")
    find_parser.add_argument("--category", default=None)
    find_parser.add_argument("--state", type=str.upper, choices=["ACTIVE", "MISSING"], default=None)
    find_parser.add_argument("--limit", type=int, default=None)
    find_parser.add_argument("--json", action="store_true")

    diff_parser = inventory_subs.add_parser(
        "diff", help="Show recorded scan change events (ADDED/UPDATED/MISSING/RESTORED). Read-only."
    )
    diff_parser.add_argument("--scan", type=int, default=None, help="Show events from exactly this scan.")
    diff_parser.add_argument(
        "--from-scan", dest="from_scan", type=int, default=None,
        help="With --to-scan, show events after this scan through --to-scan.",
    )
    diff_parser.add_argument(
        "--to-scan", dest="to_scan", type=int, default=None, help="With --from-scan, the end of the event range."
    )
    diff_parser.add_argument(
        "--type", dest="change_type", type=str.upper,
        choices=["ADDED", "UPDATED", "MISSING", "RESTORED"], default=None,
    )
    diff_parser.add_argument("--category", default=None)
    diff_parser.add_argument("--json", action="store_true")

    findings_parser = subs.add_parser("findings")
    findings_subs = findings_parser.add_subparsers(dest="findings_command", required=True)

    evaluate_findings_parser = findings_subs.add_parser(
        "evaluate",
        help="Evaluate findings rules against the current inventory and persist results. "
        "Writes only to the findings table; never touches the NAS.",
    )
    evaluate_findings_parser.add_argument("--json", action="store_true")

    list_findings_parser = findings_subs.add_parser("list", help="List findings. Read-only.")
    list_findings_parser.add_argument(
        "--status", type=str.upper, choices=["ACTIVE", "RESOLVED", "IGNORED"], default=None
    )
    list_findings_parser.add_argument(
        "--severity", type=str.upper, choices=["INFO", "WARNING", "ERROR", "CRITICAL"], default=None
    )
    list_findings_parser.add_argument("--rule", dest="rule_code", default=None)
    list_findings_parser.add_argument("--category", default=None)
    list_findings_parser.add_argument("--limit", type=int, default=None)
    list_findings_parser.add_argument("--json", action="store_true")

    stats_findings_parser = findings_subs.add_parser("stats", help="Show findings statistics. Read-only.")
    stats_findings_parser.add_argument("--json", action="store_true")

    show_findings_parser = findings_subs.add_parser("show", help="Show details for a single finding. Read-only.")
    show_findings_parser.add_argument("finding_id", type=int)
    show_findings_parser.add_argument("--json", action="store_true")

    identify_parser = subs.add_parser("identify")
    identify_subs = identify_parser.add_subparsers(dest="identify_command", required=True)

    evaluate_identify_parser = identify_subs.add_parser(
        "evaluate",
        help="Parse local identification candidates for every ACTIVE media file and persist results. "
        "Local interpretation only -- never calls TMDb/TVDB/Plex or any external service.",
    )
    evaluate_identify_parser.add_argument("--json", action="store_true")

    list_identify_parser = identify_subs.add_parser(
        "list", help="List identification candidates. Read-only; parsed interpretations, not confirmed identities."
    )
    list_identify_parser.add_argument(
        "--type", dest="candidate_type", type=str.upper,
        choices=["MOVIE", "EPISODE", "SPECIAL", "EXTRA", "UNKNOWN"], default=None,
    )
    list_identify_parser.add_argument(
        "--confidence", type=str.upper, choices=["HIGH", "MEDIUM", "LOW", "UNKNOWN"], default=None
    )
    list_identify_parser.add_argument("--category", default=None)
    list_identify_parser.add_argument("--limit", type=int, default=None)
    list_identify_parser.add_argument("--json", action="store_true")

    stats_identify_parser = identify_subs.add_parser(
        "stats", help="Show identification candidate statistics. Read-only."
    )
    stats_identify_parser.add_argument("--json", action="store_true")

    show_identify_parser = identify_subs.add_parser(
        "show", help="Show details for a single identification candidate. Read-only."
    )
    show_identify_parser.add_argument("candidate_id", type=int)
    show_identify_parser.add_argument("--json", action="store_true")

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
    config: AppConfig, *, json_output: bool, output: str, metadata: bool = False, use_db: bool = True
) -> inventory.InventoryReport:
    """Scan configured NAS categories and write JSON + summary reports.

    Read-only against the NAS: this only reads directory entries and file
    sizes (and, with `metadata=True`, invokes the read-only `mediainfo`
    tool). It never renames, moves, or deletes scanned media.

    Unless `use_db` is False, the scan result is persisted into the SQLite
    inventory schema via `inventory_repository.persist_scan()` (pending
    migrations are applied first). When that succeeds, the JSON/summary
    reports are generated by reading the canonical `InventoryReport` back
    from the database (`inventory_repository.read_inventory_report()`)
    rather than from this scan's in-memory result. With `use_db=False`, or
    if database persistence fails, the reports are generated from the
    in-memory scan result exactly as before — a database failure never
    prevents today's reports from being written, and a failed reconciliation
    is never read back (its writes were rolled back; there is nothing new to
    read). The function's return value is always this scan's in-memory
    result, regardless of which report was written to disk.
    """
    provider = mediainfo.MediaInfoProvider() if metadata else None
    report = inventory.scan_categories(config.nas_categories, metadata_provider=provider)
    output_report = report

    if use_db:
        migrate(config.database_path)
        mediainfo_version = provider.get_version() if provider else None
        connection = connect(config.database_path)
        try:
            scan_run_id = inventory_repository.persist_scan(
                connection,
                report,
                config.nas_categories,
                metadata_enabled=metadata,
                mediainfo_version=mediainfo_version,
            )
            console.print(f"[dim]Database scan run:[/dim] {scan_run_id} (COMPLETE)")
            output_report = inventory_repository.read_inventory_report(connection, config.nas_categories)
        except Exception as exc:  # noqa: BLE001 - reported, not fatal to report generation
            console.print(f"[yellow]Database persistence failed:[/yellow] {exc}")
        finally:
            connection.close()

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output_report.to_json(), encoding="utf-8")

    summary_path = output_path.with_name(f"{output_path.stem}-summary.txt")
    summary_text = inventory.render_summary(output_report)
    summary_path.write_text(summary_text, encoding="utf-8")

    if json_output:
        console.print_json(output_report.to_json())
    else:
        console.print(summary_text)
    console.print(f"\n[dim]JSON report:    {output_path}[/dim]")
    console.print(f"[dim]Summary report: {summary_path}[/dim]")
    return report


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def _render_media_file_list_text(records: list[inventory_repository.MediaFileRecord]) -> str:
    if not records:
        return "No matching files."
    lines = [f"{len(records)} file(s)", ""]
    for record in records:
        marker = "" if record.state == "ACTIVE" else f" [{record.state}]"
        error_marker = " [METADATA ERROR]" if record.media_info_error else ""
        lines.append(f"{record.category}/{record.relative_path}{marker}{error_marker}  ({_human_size(record.size_bytes)})")
    return "\n".join(lines)


def run_inventory_list(
    config: AppConfig,
    *,
    category: str | None = None,
    state: str | None = None,
    layout: str | None = None,
    metadata_error: bool | None = None,
    limit: int | None = None,
    json_output: bool = False,
) -> list[inventory_repository.MediaFileRecord]:
    """List current media_files rows. Read-only; applies pending migrations
    first so a fresh database shows "no matching files" instead of crashing."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        records = inventory_repository.list_media_files(
            connection, category=category, state=state, layout=layout, metadata_error=metadata_error, limit=limit
        )
    finally:
        connection.close()

    if json_output:
        console.print_json(data=[record.to_dict() for record in records])
    else:
        console.print(_render_media_file_list_text(records))
    return records


def run_inventory_stats(config: AppConfig, *, json_output: bool = False) -> inventory_repository.InventoryStats:
    """Show current inventory statistics. Read-only."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        stats = inventory_repository.get_inventory_stats(connection)
    finally:
        connection.close()

    if json_output:
        console.print_json(data=stats.to_dict())
    else:
        console.print(_render_stats_text(stats))
    return stats


def _render_stats_text(stats: inventory_repository.InventoryStats) -> str:
    lines = ["MAMS Inventory Statistics", "=" * 26, ""]
    for lib in stats.libraries:
        lines.append(
            f"{lib.category}: {lib.active_count} active, {lib.missing_count} missing, "
            f"{_human_size(lib.active_total_size_bytes)}"
        )
    lines.append("")
    lines.append(f"Total active:  {stats.active_file_count} ({_human_size(stats.active_total_size_bytes)})")
    lines.append(f"Total missing: {stats.missing_file_count}")
    lines.append("")
    lines.append("Layouts:    " + ", ".join(f"{k}={v}" for k, v in sorted(stats.layout_counts.items())))
    lines.append("Extensions: " + ", ".join(f"{k}={v}" for k, v in sorted(stats.extension_counts.items())))
    lines.append("")
    lines.append(
        f"Metadata: {stats.metadata_success_count} extracted, {stats.metadata_error_count} errors, "
        f"{stats.metadata_not_probed_count} not probed"
    )
    lines.append(
        f"Tracks:   {stats.video_track_count} video, {stats.audio_track_count} audio, "
        f"{stats.subtitle_track_count} subtitle"
    )
    scan = stats.most_recent_scan
    if scan is not None:
        lines.append("")
        timing = f"started {scan.started_at}"
        if scan.completed_at:
            timing += f", completed {scan.completed_at}"
        lines.append(f"Most recent scan: #{scan.id} {scan.status} ({timing})")
        if scan.status == "COMPLETE":
            lines.append(
                f"  {scan.file_count} files, {_human_size(scan.total_size_bytes or 0)} -- "
                f"+{scan.added_count or 0} ~{scan.updated_count or 0} "
                f"-{scan.missing_count or 0} restored={scan.restored_count or 0}"
            )
        elif scan.error_message:
            lines.append(f"  error: {scan.error_message}")
    return "\n".join(lines)


def run_inventory_find(
    config: AppConfig,
    query: str,
    *,
    category: str | None = None,
    state: str | None = None,
    limit: int | None = None,
    json_output: bool = False,
) -> list[inventory_repository.MediaFileRecord]:
    """Case-insensitive search over filename/relative_path/absolute_path. Read-only."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        records = inventory_repository.search_media_files(connection, query, category=category, state=state, limit=limit)
    finally:
        connection.close()

    if json_output:
        console.print_json(data=[record.to_dict() for record in records])
    else:
        console.print(_render_media_file_list_text(records))
    return records


class DiffSelectionError(ValueError):
    """Raised for an invalid/ambiguous mams inventory diff scan selection."""


class DiffScanNotFoundError(ValueError):
    """Raised when an explicitly requested --scan/--from-scan/--to-scan id doesn't exist."""


def _validate_diff_scan_args(scan: int | None, from_scan: int | None, to_scan: int | None) -> None:
    if scan is not None and (from_scan is not None or to_scan is not None):
        raise DiffSelectionError("--scan cannot be combined with --from-scan/--to-scan.")
    if (from_scan is None) != (to_scan is None):
        raise DiffSelectionError("--from-scan and --to-scan must be given together.")


def _build_diff_result(
    *,
    mode: str,
    scan: inventory_repository.ScanRunRecord | None,
    from_scan: inventory_repository.ScanRunRecord | None,
    to_scan: inventory_repository.ScanRunRecord | None,
    changes: list[inventory_repository.ScanChangeRecord],
) -> dict[str, object]:
    counts_by_type: dict[str, int] = {}
    for change in changes:
        counts_by_type[change.change_type] = counts_by_type.get(change.change_type, 0) + 1
    return {
        "mode": mode,
        "scan": scan.to_dict() if scan is not None else None,
        "from_scan": from_scan.to_dict() if from_scan is not None else None,
        "to_scan": to_scan.to_dict() if to_scan is not None else None,
        "total_changes": len(changes),
        "counts_by_type": counts_by_type,
        "changes": [change.to_dict() for change in changes],
    }


def _load_diff_result(
    connection: sqlite3.Connection,
    *,
    scan: int | None,
    from_scan: int | None,
    to_scan: int | None,
    change_type: str | None,
    category: str | None,
) -> dict[str, object]:
    """Resolve CLI scan-selection args into a diff result dict.

    Exactly one of `scan` or (`from_scan`, `to_scan`) is expected to be set
    by this point (see `_validate_diff_scan_args`); neither set defaults to
    the most recent COMPLETE scan. Raises DiffScanNotFoundError for any
    explicitly given scan id that doesn't exist -- never mutates anything.
    """
    if scan is not None:
        scan_record = inventory_repository.get_scan_run(connection, scan)
        if scan_record is None:
            raise DiffScanNotFoundError(f"Scan {scan} not found.")
        changes = inventory_repository.list_scan_changes(
            connection, scan_run_id=scan, change_type=change_type, category=category
        )
        return _build_diff_result(mode="single", scan=scan_record, from_scan=None, to_scan=None, changes=changes)

    if from_scan is not None and to_scan is not None:
        from_record = inventory_repository.get_scan_run(connection, from_scan)
        if from_record is None:
            raise DiffScanNotFoundError(f"Scan {from_scan} not found.")
        to_record = inventory_repository.get_scan_run(connection, to_scan)
        if to_record is None:
            raise DiffScanNotFoundError(f"Scan {to_scan} not found.")
        changes = inventory_repository.list_scan_changes(
            connection, from_scan_id=from_scan, to_scan_id=to_scan, change_type=change_type, category=category
        )
        return _build_diff_result(mode="range", scan=None, from_scan=from_record, to_scan=to_record, changes=changes)

    latest = inventory_repository.get_latest_completed_scan_run(connection)
    if latest is None:
        return _build_diff_result(mode="single", scan=None, from_scan=None, to_scan=None, changes=[])
    changes = inventory_repository.list_scan_changes(
        connection, scan_run_id=latest.id, change_type=change_type, category=category
    )
    return _build_diff_result(mode="single", scan=latest, from_scan=None, to_scan=None, changes=changes)


def _render_diff_text(result: dict[str, object]) -> str:
    lines = ["MAMS Inventory Diff", "=" * 20, ""]

    if result["mode"] == "single":
        scan = cast("dict[str, object] | None", result["scan"])
        if scan is None:
            lines.append("No completed scans yet.")
            return "\n".join(lines)
        lines.append(f"Scan #{scan['id']} -- {scan['status']}")
        lines.append(f"  started:   {scan['started_at']}")
        if scan["completed_at"]:
            lines.append(f"  completed: {scan['completed_at']}")
        if scan["error_message"]:
            lines.append(f"  error:     {scan['error_message']}")
    else:
        from_scan = cast("dict[str, object]", result["from_scan"])
        to_scan = cast("dict[str, object]", result["to_scan"])
        lines.append(
            f"Event range: after scan #{from_scan['id']} ({from_scan['status']}, {from_scan['started_at']}) "
            f"through scan #{to_scan['id']} ({to_scan['status']}, {to_scan['completed_at'] or to_scan['started_at']})"
        )
        lines.append("  (recorded change events in this range, not a reconstructed snapshot comparison)")

    lines.append("")
    lines.append(f"Total changes: {result['total_changes']}")
    counts = cast("dict[str, int]", result["counts_by_type"])
    for change_type in ("ADDED", "UPDATED", "MISSING", "RESTORED"):
        lines.append(f"  {change_type}: {counts.get(change_type, 0)}")

    changes = cast("list[dict[str, object]]", result["changes"])
    if changes:
        lines.append("")
        for change in changes:
            lines.append(f"[{change['change_type']}] {change['category'] or '?'}: {change['absolute_path']}")
            if change["change_type"] == "UPDATED" and change["details"]:
                details = cast("dict[str, object]", change["details"])
                for entry in cast("list[dict[str, object]]", details.get("changes", [])):
                    if "old_count" in entry:
                        lines.append(f"    {entry['field']}: {entry['old_count']} -> {entry['new_count']}")
                    else:
                        lines.append(f"    {entry['field']}: {entry['old']!r} -> {entry['new']!r}")

    return "\n".join(lines)


def run_inventory_diff(
    config: AppConfig,
    *,
    scan: int | None = None,
    from_scan: int | None = None,
    to_scan: int | None = None,
    change_type: str | None = None,
    category: str | None = None,
    json_output: bool = False,
) -> dict[str, object] | None:
    """Show recorded scan_changes events -- immutable evidence, not a
    reconstructed historical snapshot. Defaults to the most recent COMPLETE
    scan when no scan-selection args are given. Returns None (after
    printing a clear, non-destructive error) for an invalid combination of
    scan-selection args or an unknown scan id; never raises to the caller.
    """
    try:
        _validate_diff_scan_args(scan, from_scan, to_scan)
    except DiffSelectionError as exc:
        console.print(f"[red]{exc}[/red]")
        return None

    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        result = _load_diff_result(
            connection, scan=scan, from_scan=from_scan, to_scan=to_scan, change_type=change_type, category=category
        )
    except DiffScanNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        return None
    finally:
        connection.close()

    if json_output:
        console.print_json(data=result)
    else:
        console.print(_render_diff_text(result))
    return result


def run_findings_evaluate(
    config: AppConfig, *, json_output: bool = False
) -> findings_repository.ReconciliationResult:
    """Evaluate all findings rules against the current canonical inventory
    and persist the reconciled results. Writes only to the findings table;
    never touches the NAS or any other table. Applies pending migrations
    first, same as the inventory query commands."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        result = findings_service.evaluate_findings(connection)
    finally:
        connection.close()

    if json_output:
        console.print_json(data=result.to_dict())
    else:
        console.print(_render_findings_evaluate_text(result))
    return result


def _render_findings_evaluate_text(result: findings_repository.ReconciliationResult) -> str:
    lines = ["MAMS Findings Evaluation", "=" * 24, ""]
    lines.append(f"Created:     {result.created}")
    lines.append(f"Reactivated: {result.reactivated}")
    lines.append(f"Updated:     {result.updated}")
    lines.append(f"Unchanged:   {result.unchanged}")
    lines.append(f"Resolved:    {result.resolved}")
    if result.ignored_touched:
        lines.append(f"Ignored (preserved): {result.ignored_touched}")
    return "\n".join(lines)


def _finding_path(record: findings_repository.FindingRecord) -> str:
    if record.category is not None and record.relative_path is not None:
        return f"{record.category}/{record.relative_path}"
    return record.absolute_path or "(file no longer resolvable)"


def _render_findings_list_text(records: list[findings_repository.FindingRecord]) -> str:
    if not records:
        return "No matching findings."
    lines = [f"{len(records)} finding(s)", ""]
    lines.append(f"{'SEVERITY':<9} {'RULE':<26} {'CATEGORY':<10} PATH")
    for record in records:
        lines.append(
            f"{record.severity:<9} {record.rule_code:<26} {(record.category or '?'):<10} {_finding_path(record)}"
        )
    return "\n".join(lines)


def run_findings_list(
    config: AppConfig,
    *,
    status: str | None = None,
    severity: str | None = None,
    rule_code: str | None = None,
    category: str | None = None,
    limit: int | None = None,
    json_output: bool = False,
) -> list[findings_repository.FindingRecord]:
    """List findings rows from the database. Read-only."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        records = findings_repository.list_findings(
            connection, status=status, severity=severity, rule_code=rule_code, category=category, limit=limit
        )
    finally:
        connection.close()

    if json_output:
        console.print_json(data=[record.to_dict() for record in records])
    else:
        console.print(_render_findings_list_text(records))
    return records


def _render_findings_stats_text(stats: findings_repository.FindingsStats) -> str:
    lines = ["MAMS Findings Statistics", "=" * 24, ""]
    lines.append(f"Total:    {stats.total_count}")
    lines.append(f"Active:   {stats.active_count}")
    lines.append(f"Resolved: {stats.resolved_count}")
    lines.append(f"Ignored:  {stats.ignored_count}")
    lines.append("")
    lines.append(
        "By severity (active): "
        + (", ".join(f"{k}={v}" for k, v in sorted(stats.severity_counts.items())) or "none")
    )
    lines.append(
        "By rule (active):     " + (", ".join(f"{k}={v}" for k, v in sorted(stats.rule_counts.items())) or "none")
    )
    return "\n".join(lines)


def run_findings_stats(config: AppConfig, *, json_output: bool = False) -> findings_repository.FindingsStats:
    """Show current findings statistics. Read-only."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        stats = findings_repository.get_findings_stats(connection)
    finally:
        connection.close()

    if json_output:
        console.print_json(data=stats.to_dict())
    else:
        console.print(_render_findings_stats_text(stats))
    return stats


def _render_finding_text(record: findings_repository.FindingRecord) -> str:
    lines = [f"Finding #{record.id}", "=" * len(f"Finding #{record.id}")]
    lines.append(f"Status:   {record.status}")
    lines.append(f"Severity: {record.severity}")
    lines.append(f"Rule:     {record.rule_code}")
    lines.append(f"Path:     {_finding_path(record)}")
    lines.append("")
    lines.append(f"Summary: {record.summary}")
    if record.evidence:
        lines.append("")
        lines.append("Evidence:")
        for key, value in sorted(record.evidence.items()):
            lines.append(f"  {key}: {value}")
    if record.recommendation:
        lines.append("")
        lines.append(f"Recommendation: {record.recommendation}")
    lines.append("")
    lines.append(f"First detected: {record.first_detected_at}")
    lines.append(f"Last detected:  {record.last_detected_at}")
    if record.resolved_at:
        lines.append(f"Resolved:       {record.resolved_at}")
    return "\n".join(lines)


def run_findings_show(
    config: AppConfig, finding_id: int, *, json_output: bool = False
) -> findings_repository.FindingRecord | None:
    """Show details for a single finding. Read-only. Returns None (after
    printing a clear error, never mutating anything) for an unknown id."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        record = findings_repository.get_finding(connection, finding_id)
    finally:
        connection.close()

    if record is None:
        console.print(f"[red]Finding {finding_id} not found.[/red]")
        return None

    if json_output:
        console.print_json(data=record.to_dict())
    else:
        console.print(_render_finding_text(record))
    return record


def run_identify_evaluate(
    config: AppConfig, *, json_output: bool = False
) -> identification_repository.ReconciliationResult:
    """Parse local identification candidates for every ACTIVE media file
    and persist the reconciled results. Writes only to the
    identification_candidates table; never calls TMDb/TVDB/Plex or any
    other external service. Applies pending migrations first, same as
    `mams findings evaluate`."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        result = identification_service.evaluate_candidates(connection)
    finally:
        connection.close()

    if json_output:
        console.print_json(data=result.to_dict())
    else:
        console.print(_render_identify_evaluate_text(result))
    return result


def _render_identify_evaluate_text(result: identification_repository.ReconciliationResult) -> str:
    lines = ["MAMS Identification Evaluation", "=" * 30, ""]
    lines.append(f"Created:   {result.created}")
    lines.append(f"Updated:   {result.updated}")
    lines.append(f"Unchanged: {result.unchanged}")
    return "\n".join(lines)


def _parsed_identity(record: identification_repository.CandidateRecord) -> str:
    """A human-readable rendering of a candidate's parsed fields --
    deliberately never called "identity"/"identified" in isolation; every
    surface that shows this string labels it PARSED IDENTITY, not a
    confirmed one."""
    if record.candidate_type == "MOVIE":
        if record.parsed_title is None:
            return "(unparsed)"
        if record.parsed_year is not None:
            return f"{record.parsed_title} ({record.parsed_year})"
        return record.parsed_title
    if record.candidate_type in ("EPISODE", "SPECIAL"):
        series = record.parsed_series_title or "(unknown series)"
        if record.season_number is None or not record.episode_numbers:
            return series
        episodes = "".join(f"E{n:02d}" for n in record.episode_numbers)
        identity = f"{series} S{record.season_number:02d}{episodes}"
        if record.episode_title:
            identity += f" - {record.episode_title}"
        return identity
    if record.candidate_type == "EXTRA":
        label = record.special_type or "extra"
        return f"{record.parsed_series_title} {label}" if record.parsed_series_title else label
    return record.parsed_title or "(unparsed)"


def _render_identify_list_text(records: list[identification_repository.CandidateRecord]) -> str:
    if not records:
        return "No matching candidates."
    lines = [f"{len(records)} candidate(s) -- parsed local interpretations, not confirmed identities", ""]
    lines.append(f"{'CONFIDENCE':<10} {'TYPE':<9} {'CATEGORY':<10} {'PARSED IDENTITY':<32} PATH")
    for record in records:
        lines.append(
            f"{record.confidence:<10} {record.candidate_type:<9} {record.category:<10} "
            f"{_parsed_identity(record):<32} {record.category}/{record.relative_path}"
        )
    return "\n".join(lines)


def run_identify_list(
    config: AppConfig,
    *,
    candidate_type: str | None = None,
    confidence: str | None = None,
    category: str | None = None,
    limit: int | None = None,
    json_output: bool = False,
) -> list[identification_repository.CandidateRecord]:
    """List identification_candidates rows from the database. Read-only."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        records = identification_repository.list_candidates(
            connection, candidate_type=candidate_type, confidence=confidence, category=category, limit=limit
        )
    finally:
        connection.close()

    if json_output:
        console.print_json(data=[record.to_dict() for record in records])
    else:
        console.print(_render_identify_list_text(records))
    return records


def _render_identify_stats_text(stats: identification_repository.CandidateStats) -> str:
    lines = ["MAMS Identification Statistics", "=" * 30, ""]
    lines.append(f"Total: {stats.total_count}")
    lines.append("")
    lines.append("By type:       " + (", ".join(f"{k}={v}" for k, v in sorted(stats.type_counts.items())) or "none"))
    lines.append(
        "By confidence: " + (", ".join(f"{k}={v}" for k, v in sorted(stats.confidence_counts.items())) or "none")
    )
    lines.append("")
    lines.append(f"With parsed year:    {stats.with_year_count}")
    lines.append(f"Without parsed year: {stats.without_year_count}")
    return "\n".join(lines)


def run_identify_stats(config: AppConfig, *, json_output: bool = False) -> identification_repository.CandidateStats:
    """Show current identification candidate statistics. Read-only."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        stats = identification_repository.get_candidate_stats(connection)
    finally:
        connection.close()

    if json_output:
        console.print_json(data=stats.to_dict())
    else:
        console.print(_render_identify_stats_text(stats))
    return stats


def _render_identify_candidate_text(record: identification_repository.CandidateRecord) -> str:
    lines = [f"Candidate #{record.id}", "=" * len(f"Candidate #{record.id}")]
    lines.append("(a parsed local interpretation -- not a confirmed identity)")
    lines.append("")
    lines.append(f"Type:       {record.candidate_type}")
    lines.append(f"Confidence: {record.confidence}")
    lines.append(f"Path:       {record.category}/{record.relative_path}")
    lines.append(f"Parsed:     {_parsed_identity(record)}")
    lines.append("")
    if record.candidate_type == "MOVIE":
        if record.edition:
            lines.append(f"Edition:      {record.edition}")
        if record.part_number is not None:
            lines.append(f"Part number:  {record.part_number}")
    else:
        if record.special_type:
            lines.append(f"Special type: {record.special_type}")
    if record.evidence:
        lines.append("")
        lines.append("Evidence:")
        for key, value in sorted(record.evidence.items()):
            lines.append(f"  {key}: {value}")
    lines.append("")
    lines.append(f"Parser version: {record.parser_version}")
    lines.append(f"Created: {record.created_at}")
    lines.append(f"Updated: {record.updated_at}")
    return "\n".join(lines)


def run_identify_show(
    config: AppConfig, candidate_id: int, *, json_output: bool = False
) -> identification_repository.CandidateRecord | None:
    """Show details for a single identification candidate. Read-only.
    Returns None (after printing a clear error, never mutating anything)
    for an unknown id."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        record = identification_repository.get_candidate(connection, candidate_id)
    finally:
        connection.close()

    if record is None:
        console.print(f"[red]Candidate {candidate_id} not found.[/red]")
        return None

    if json_output:
        console.print_json(data=record.to_dict())
    else:
        console.print(_render_identify_candidate_text(record))
    return record


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
            run_inventory_scan(
                config,
                json_output=args.json,
                output=args.output,
                metadata=args.metadata,
                use_db=not args.no_db,
            )
        elif args.inventory_command == "list":
            run_inventory_list(
                config,
                category=args.category,
                state=args.state,
                layout=args.layout,
                metadata_error=args.metadata_error,
                limit=args.limit,
                json_output=args.json,
            )
        elif args.inventory_command == "stats":
            run_inventory_stats(config, json_output=args.json)
        elif args.inventory_command == "find":
            run_inventory_find(
                config, args.query, category=args.category, state=args.state, limit=args.limit, json_output=args.json
            )
        elif args.inventory_command == "diff":
            run_inventory_diff(
                config,
                scan=args.scan,
                from_scan=args.from_scan,
                to_scan=args.to_scan,
                change_type=args.change_type,
                category=args.category,
                json_output=args.json,
            )
    elif args.command == "findings":
        if args.findings_command == "evaluate":
            run_findings_evaluate(config, json_output=args.json)
        elif args.findings_command == "list":
            run_findings_list(
                config,
                status=args.status,
                severity=args.severity,
                rule_code=args.rule_code,
                category=args.category,
                limit=args.limit,
                json_output=args.json,
            )
        elif args.findings_command == "stats":
            run_findings_stats(config, json_output=args.json)
        elif args.findings_command == "show":
            run_findings_show(config, args.finding_id, json_output=args.json)
    elif args.command == "identify":
        if args.identify_command == "evaluate":
            run_identify_evaluate(config, json_output=args.json)
        elif args.identify_command == "list":
            run_identify_list(
                config,
                candidate_type=args.candidate_type,
                confidence=args.confidence,
                category=args.category,
                limit=args.limit,
                json_output=args.json,
            )
        elif args.identify_command == "stats":
            run_identify_stats(config, json_output=args.json)
        elif args.identify_command == "show":
            run_identify_show(config, args.candidate_id, json_output=args.json)
    elif args.command == "mediainfo":
        run_mediainfo(args.path, json_output=args.json)

if __name__ == "__main__":
    main()
