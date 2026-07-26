from __future__ import annotations
import argparse
import json
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
from . import ingest_repository
from . import ingest_service
from . import execution_repository
from . import execution_service
from . import provider_cache_repository
from . import readiness
from . import resolution_repository
from . import resolution_service
from .tmdb import TMDbAuthenticationError, TMDbConnectionError, TMDbRateLimitError, TMDbResponseError, TMDbTimeoutError
console = Console()

DEFAULT_INVENTORY_REPORT = "reports/library.json"


class _SingleCategoryAction(argparse.Action):
    """Rejects a repeated `--category` at parse time (exit code 2, via
    `parser.error`) rather than silently keeping the last occurrence --
    Milestone 8.2 supports scanning exactly one category per invocation,
    never a list."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} may be specified at most once.")
        setattr(namespace, self.dest, values)


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
        help="Probe media files with MediaInfo within the selected scan scope (slower; requires mediainfo).",
    )
    scan_parser.add_argument(
        "--no-db",
        action="store_true",
        help="Skip database persistence; only write the JSON/summary reports.",
    )
    scan_parser.add_argument(
        "--category",
        action=_SingleCategoryAction,
        default=None,
        help=(
            "Scan and reconcile only one configured inventory category. Other categories "
            "are not walked, probed, or reconciled. Valid values come from the configured "
            "nas/incoming categories, not a hard-coded list (e.g. incoming, movies, tv). "
            "May be given at most once; omit to run the existing full scan across every "
            "configured category."
        ),
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

    override_identify_parser = identify_subs.add_parser(
        "override",
        help="Manually set the effective identification for a media file. Database-only; the parsed "
        "candidate is preserved untouched. Replaces any existing active override for the same file.",
    )
    override_identify_parser.add_argument("media_file_id", type=int)
    override_identify_parser.add_argument(
        "--type", dest="candidate_type", type=str.upper, choices=["MOVIE", "EPISODE"], required=True
    )
    override_identify_parser.add_argument("--title", default=None, help="Required for --type MOVIE.")
    override_identify_parser.add_argument("--year", type=int, default=None)
    override_identify_parser.add_argument("--series", dest="series_title", default=None, help="Required for --type EPISODE.")
    override_identify_parser.add_argument(
        "--season", dest="season_number", type=int, default=None, help="Required for --type EPISODE."
    )
    override_identify_parser.add_argument(
        "--episode", dest="episode_number", type=int, default=None, help="Required for --type EPISODE."
    )
    override_identify_parser.add_argument("--episode-title", dest="episode_title", default=None)
    override_identify_parser.add_argument("--reason", default=None, help="Optional free-text note on why this override exists.")
    override_identify_parser.add_argument("--json", action="store_true")

    clear_override_identify_parser = identify_subs.add_parser(
        "clear-override",
        help="Clear the active override for a media file. Effective identification reverts to the parsed candidate.",
    )
    clear_override_identify_parser.add_argument("media_file_id", type=int)
    clear_override_identify_parser.add_argument("--json", action="store_true")

    show_effective_identify_parser = identify_subs.add_parser(
        "show-effective",
        help="Show the identification resolution/planning will actually use for a file: an active "
        "override if one exists, else the parsed candidate. Read-only.",
    )
    show_effective_identify_parser.add_argument("media_file_id", type=int)
    show_effective_identify_parser.add_argument("--json", action="store_true")

    resolve_parser = subs.add_parser("resolve")
    resolve_subs = resolve_parser.add_subparsers(dest="resolve_command", required=True)

    evaluate_resolve_parser = resolve_subs.add_parser(
        "evaluate",
        help="Resolve local identification candidates against TMDb and persist ranked matches. "
        "Requires TMDB_API_TOKEN to be configured.",
    )
    evaluate_resolve_parser.add_argument("--media-file-id", type=int, default=None)
    evaluate_resolve_parser.add_argument("--candidate-id", type=int, default=None)
    evaluate_resolve_parser.add_argument("--category", default=None)
    evaluate_resolve_parser.add_argument(
        "--limit", type=int, default=10,
        help="Maximum number of candidates to evaluate in one run (default: 10) -- unlike local "
        "identification, each evaluation costs a real TMDb request, so there is no unlimited default.",
    )
    evaluate_resolve_parser.add_argument(
        "--force", action="store_true", help="Re-evaluate candidates that already have a resolution attempt."
    )
    evaluate_resolve_parser.add_argument("--json", action="store_true")

    list_resolve_parser = resolve_subs.add_parser("list", help="List resolution attempts. Read-only.")
    list_resolve_parser.add_argument(
        "--status", type=str.upper,
        choices=["PENDING", "RESOLVED", "REVIEW_REQUIRED", "NO_MATCH", "FAILED", "SKIPPED"], default=None,
    )
    list_resolve_parser.add_argument("--media-file-id", type=int, default=None)
    list_resolve_parser.add_argument("--category", default=None)
    list_resolve_parser.add_argument("--limit", type=int, default=None)
    list_resolve_parser.add_argument("--json", action="store_true")

    show_resolve_parser = resolve_subs.add_parser("show", help="Show one resolution attempt with ranked alternatives. Read-only.")
    show_resolve_parser.add_argument("attempt_id", type=int)
    show_resolve_parser.add_argument("--json", action="store_true")

    select_resolve_parser = resolve_subs.add_parser(
        "select", help="Manually confirm one ranked match for a resolution attempt. Writes no media files."
    )
    select_resolve_parser.add_argument("attempt_id", type=int)
    select_resolve_parser.add_argument("match_id", type=int)
    select_resolve_parser.add_argument("--json", action="store_true")

    reject_resolve_parser = resolve_subs.add_parser(
        "reject", help="Manually reject every match for a resolution attempt. Creates no assignment."
    )
    reject_resolve_parser.add_argument("attempt_id", type=int)
    reject_resolve_parser.add_argument("--json", action="store_true")

    stats_resolve_parser = resolve_subs.add_parser("stats", help="Show resolution attempt statistics. Read-only.")
    stats_resolve_parser.add_argument("--json", action="store_true")

    provider_status_parser = resolve_subs.add_parser(
        "provider-status",
        help="Check TMDb configuration/authentication with one minimal, cached request. Never prints the token.",
    )
    provider_status_parser.add_argument("--json", action="store_true")

    cache_stats_parser = resolve_subs.add_parser("cache-stats", help="Show provider_cache statistics. Read-only.")
    cache_stats_parser.add_argument("--json", action="store_true")

    cache_clear_parser = resolve_subs.add_parser(
        "cache-clear", help="Delete expired provider_cache rows. Never touches external identities, assignments, or plans."
    )
    cache_clear_parser.add_argument(
        "--expired-only", action="store_true", required=True,
        help="Required: only expired rows are ever deleted; there is no broader cache-clear.",
    )
    cache_clear_parser.add_argument("--json", action="store_true")

    ingest_parser = subs.add_parser("ingest")
    ingest_subs = ingest_parser.add_subparsers(dest="ingest_command", required=True)

    plan_ingest_parser = ingest_subs.add_parser(
        "plan", help="Generate (or reconcile) a dry-run ingest plan for one media file. No filesystem changes."
    )
    plan_ingest_parser.add_argument("media_file_id", type=int)
    plan_ingest_parser.add_argument(
        "--destination-category", dest="destination_category", type=str.lower,
        choices=sorted(ingest_service.DESTINATION_CATEGORY_HINTS), default=None,
    )
    plan_ingest_parser.add_argument("--json", action="store_true")

    plans_ingest_parser = ingest_subs.add_parser("plans", help="List dry-run ingest plans. Read-only.")
    plans_ingest_parser.add_argument(
        "--status", type=str.upper,
        choices=["DRAFT", "READY_FOR_REVIEW", "REVIEW_REQUIRED", "BLOCKED", "APPROVED", "SUPERSEDED"], default=None,
    )
    plans_ingest_parser.add_argument("--category", default=None)
    plans_ingest_parser.add_argument("--limit", type=int, default=None)
    plans_ingest_parser.add_argument("--json", action="store_true")

    show_ingest_parser = ingest_subs.add_parser("show", help="Show one dry-run ingest plan with its proposed actions. Read-only.")
    show_ingest_parser.add_argument("plan_id", type=int)
    show_ingest_parser.add_argument("--json", action="store_true")

    stats_ingest_parser = ingest_subs.add_parser("stats", help="Show dry-run ingest plan statistics. Read-only.")
    stats_ingest_parser.add_argument("--json", action="store_true")

    approve_ingest_parser = ingest_subs.add_parser(
        "approve", help="Mark a READY_FOR_REVIEW plan APPROVED. Database state only -- no actions are executed."
    )
    approve_ingest_parser.add_argument("plan_id", type=int)
    approve_ingest_parser.add_argument("--json", action="store_true")

    audit_ingest_parser = ingest_subs.add_parser(
        "audit",
        help="Read-only execution-readiness audit for an approved dry-run plan. Never executes anything.",
    )
    audit_ingest_parser.add_argument("plan_id", type=int)
    audit_ingest_parser.add_argument("--json", action="store_true")

    execute_ingest_parser = ingest_subs.add_parser(
        "execute",
        help="Execute one APPROVED, READY_FOR_EXECUTOR ingest plan. Modifies the filesystem -- requires --confirm-plan.",
    )
    execute_ingest_parser.add_argument("plan_id", type=int)
    execute_ingest_parser.add_argument("--confirm-plan", dest="confirm_plan", type=int, default=None)
    execute_ingest_parser.add_argument("--json", action="store_true")

    executions_ingest_parser = ingest_subs.add_parser("executions", help="List Milestone 8 execution history. Read-only.")
    executions_ingest_parser.add_argument("--plan-id", dest="plan_id", type=int, default=None)
    executions_ingest_parser.add_argument(
        "--status", type=str.upper, choices=["EXECUTING", "SUCCEEDED", "FAILED", "RECOVERY_REQUIRED"], default=None,
    )
    executions_ingest_parser.add_argument(
        "--recovery-status",
        dest="recovery_status",
        type=str.upper,
        choices=[
            "NONE", "PARTIAL_DESTINATION_SOURCE_INTACT", "DESTINATION_VERIFIED_SOURCE_NOT_REMOVED",
            "DESTINATION_UNVERIFIED_SOURCE_REMOVED", "INVENTORY_REFRESH_INCOMPLETE",
            "INTERRUPTED_STATE_UNKNOWN", "OTHER_REQUIRES_MANUAL_INSPECTION",
        ],
        default=None,
    )
    executions_ingest_parser.add_argument("--limit", type=int, default=None)
    executions_ingest_parser.add_argument("--json", action="store_true")

    execution_ingest_parser = ingest_subs.add_parser("execution", help="Show one execution's full step history. Read-only.")
    execution_ingest_parser.add_argument("execution_id", type=int)
    execution_ingest_parser.add_argument("--json", action="store_true")

    recovery_ingest_parser = ingest_subs.add_parser(
        "recovery", help="Read-only recovery guidance for one execution. Never mutates the database or filesystem."
    )
    recovery_ingest_parser.add_argument("execution_id", type=int)
    recovery_ingest_parser.add_argument("--json", action="store_true")

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

class InventoryScanScopeError(ValueError):
    """Base for an invalid `mams inventory scan --category CATEGORY` request.

    `main()` catches this specifically, prints the message, and exits
    non-zero -- unlike most other `run_*` errors in this module, which
    print and return `None`, this one must be a clear command failure
    per the Milestone 8.2 CLI contract."""


class UnknownCategoryError(InventoryScanScopeError):
    """`--category CATEGORY` named a category absent from the currently
    configured `nas.categories`/`ingest.incoming_roots`. Lists the valid
    configured category names -- never a hard-coded CLI list."""


def _scoped_report_payload(
    report: inventory.InventoryReport,
    *,
    category: str,
    metadata_enabled: bool,
    scan_run: inventory_repository.ScanRunRecord | None,
) -> dict[str, object]:
    """Wrap a category-scoped `InventoryReport` with the scan-scope
    metadata a scoped report must carry (scan run id, scope, selected
    category, metadata flag, timing) -- keeps the existing
    `InventoryReport.to_dict()` shape (`file_count`/`total_size_bytes`/
    `categories`) nested inside, rather than inventing a second report
    format. `scan_run` is `None` when `--no-db` was given or persistence
    failed; the scan-run fields are simply `None` in that case."""
    payload: dict[str, object] = {
        "scan_scope": "CATEGORY",
        "scope_category": category,
        "metadata_enabled": metadata_enabled,
        "scan_run_id": scan_run.id if scan_run is not None else None,
        "started_at": scan_run.started_at if scan_run is not None else None,
        "completed_at": scan_run.completed_at if scan_run is not None else None,
    }
    payload.update(report.to_dict())
    return payload


def _render_scoped_scan_text(
    report: inventory.InventoryReport, *, category: str, metadata_enabled: bool
) -> str:
    """Human-readable summary for a category-scoped scan -- deliberately
    distinct from `inventory.render_summary()` (used for full scans) so a
    scoped scan can never be mistaken for a full-library report: it names
    its scope up front and states plainly that other categories were left
    untouched."""
    lines = ["MAMS Inventory Scan", "=" * 19, "", f"Scope: {category}", f"Metadata: {'enabled' if metadata_enabled else 'disabled'}"]
    cat = report.categories[0] if report.categories else None
    if cat is not None:
        lines.append("")
        status = "OK" if cat.exists else "MISSING"
        lines.append(f"{cat.category} [{status}] - {cat.root_path}")
        if not cat.exists:
            lines.append("  (directory not found; skipped)")
        else:
            lines.append(f"  files: {cat.file_count}")
            lines.append(f"  size:  {_human_size(cat.total_size_bytes)}")
            if metadata_enabled:
                probed = [f for f in cat.files if f.media_info is not None or f.media_info_error is not None]
                succeeded = sum(1 for f in probed if f.media_info is not None)
                failed = sum(1 for f in probed if f.media_info_error is not None)
                lines.append(f"  metadata extracted: {succeeded}")
                # No incremental metadata cache exists yet (see mediainfo.py):
                # every discovered file is re-probed, so nothing is ever
                # "unchanged" -- reported explicitly as 0 rather than omitted,
                # so this line never implies a snapshot was reused.
                lines.append("  metadata unchanged: 0")
                lines.append(f"  metadata errors:    {failed}")
    lines.append("")
    lines.append("Scoped scan complete.")
    lines.append("Other categories were not scanned or reconciled.")
    return "\n".join(lines)


def run_inventory_scan(
    config: AppConfig,
    *,
    json_output: bool,
    output: str = DEFAULT_INVENTORY_REPORT,
    metadata: bool = False,
    use_db: bool = True,
    category: str | None = None,
) -> inventory.InventoryReport:
    """Scan configured NAS/incoming categories and write JSON + summary reports.

    Read-only against the NAS: this only reads directory entries and file
    sizes (and, with `metadata=True`, invokes the read-only `mediainfo`
    tool). It never renames, moves, or deletes scanned media.

    `category=None` (the default) preserves the original full scan across
    every configured category, writing `reports/library.json` /
    `reports/library-summary.txt` exactly as before. `category="incoming"`
    (etc.) restricts *both* discovery and reconciliation to that one
    configured category: `categories` below becomes a single-entry dict,
    and every downstream call (`inventory.scan_categories()`,
    `inventory_repository.persist_scan()`,
    `inventory_repository.read_inventory_report()`) already only touches
    what's in that dict -- no separate scoped code path exists in any of
    those three functions. Raises `UnknownCategoryError` for a category not
    present in the configured `nas.categories`/`ingest.incoming_roots` --
    caught by `main()`, printed, and turned into a non-zero exit, never a
    stack trace. A category scan's `--output` defaults to
    `reports/library-{category}.json` rather than the full scan's default,
    so a bare `--category CATEGORY` (the common case) can never overwrite
    the full-library report; an explicit `--output` is used exactly as
    given, same as a full scan.

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
    # Incoming roots (Milestone 7B) are scanned as additional categories,
    # merged in alongside the NAS categories -- see AppConfig.incoming_categories
    # and docs/DATABASE.md ("Incoming as a category"). No change to
    # inventory.py itself: it has always operated on a plain category-name
    # -> root-path mapping.
    configured_categories = {**config.nas_categories, **config.incoming_categories}

    if category is not None and category not in configured_categories:
        valid = ", ".join(sorted(configured_categories)) or "(none configured)"
        raise UnknownCategoryError(f"Unknown category '{category}'. Valid categories: {valid}")

    if category is not None and output == DEFAULT_INVENTORY_REPORT:
        output = f"reports/library-{category}.json"

    categories = {category: configured_categories[category]} if category is not None else configured_categories
    provider = mediainfo.MediaInfoProvider() if metadata else None
    report = inventory.scan_categories(categories, metadata_provider=provider)
    output_report = report
    scan_run_record: inventory_repository.ScanRunRecord | None = None

    if use_db:
        migrate(config.database_path)
        mediainfo_version = provider.get_version() if provider else None
        connection = connect(config.database_path)
        try:
            scan_run_id = inventory_repository.persist_scan(
                connection,
                report,
                categories,
                metadata_enabled=metadata,
                mediainfo_version=mediainfo_version,
                scan_scope="CATEGORY" if category is not None else "FULL",
                scope_category=category,
            )
            console.print(f"[dim]Database scan run:[/dim] {scan_run_id} (COMPLETE)")
            output_report = inventory_repository.read_inventory_report(connection, categories)
            scan_run_record = inventory_repository.get_scan_run(connection, scan_run_id)
        except Exception as exc:  # noqa: BLE001 - reported, not fatal to report generation
            console.print(f"[yellow]Database persistence failed:[/yellow] {exc}")
        finally:
            connection.close()

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if category is not None and output_path.name == f"library-{category}.json":
        summary_path = output_path.with_name(f"library-summary-{category}.txt")
    else:
        summary_path = output_path.with_name(f"{output_path.stem}-summary.txt")

    if category is not None:
        payload = _scoped_report_payload(
            output_report, category=category, metadata_enabled=metadata, scan_run=scan_run_record
        )
        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        summary_text = _render_scoped_scan_text(output_report, category=category, metadata_enabled=metadata)
        summary_path.write_text(summary_text, encoding="utf-8")
        if json_output:
            console.print_json(data=payload)
        else:
            console.print(summary_text)
    else:
        output_path.write_text(output_report.to_json(), encoding="utf-8")
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


def _render_override_text(override: identification_repository.OverrideRecord) -> str:
    lines = [f"Override #{override.id}", "=" * len(f"Override #{override.id}")]
    lines.append(f"Media file: {override.media_file_id}")
    lines.append(f"Type:       {override.candidate_type}")
    if override.candidate_type == "MOVIE":
        label = override.title or "(no title)"
        if override.year is not None:
            label += f" ({override.year})"
        lines.append(f"Identity:   {label}")
    else:
        series = override.series_title or "(no series)"
        if override.season_number is not None and override.episode_numbers:
            episodes = "".join(f"E{n:02d}" for n in override.episode_numbers)
            label = f"{series} S{override.season_number:02d}{episodes}"
        else:
            label = series
        if override.episode_title:
            label += f" - {override.episode_title}"
        lines.append(f"Identity:   {label}")
    if override.reason:
        lines.append(f"Reason:     {override.reason}")
    lines.append(f"Created:    {override.created_at}")
    if override.cleared_at:
        lines.append(f"Cleared:    {override.cleared_at}")
    return "\n".join(lines)


def run_identify_override(
    config: AppConfig,
    media_file_id: int,
    *,
    candidate_type: str,
    title: str | None = None,
    year: int | None = None,
    series_title: str | None = None,
    season_number: int | None = None,
    episode_number: int | None = None,
    episode_title: str | None = None,
    reason: str | None = None,
    json_output: bool = False,
) -> identification_repository.OverrideRecord | None:
    """Create (or replace) the active manual override for a media file.
    Database-only: never renames or touches the file, never modifies the
    parsed identification_candidates row. Returns None (after printing a
    clear error, mutating nothing) for an unknown media_file_id or a
    request missing a field its --type requires."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        media_file = inventory_repository.get_media_file(connection, media_file_id)
        if media_file is None:
            console.print(f"[red]No media file with id {media_file_id}.[/red]")
            return None
        try:
            with connection:
                override = identification_repository.create_override(
                    connection,
                    media_file_id=media_file_id,
                    candidate_type=candidate_type,
                    title=title,
                    year=year,
                    series_title=series_title,
                    season_number=season_number,
                    episode_numbers=(episode_number,) if episode_number is not None else None,
                    episode_title=episode_title,
                    reason=reason,
                )
        except identification_repository.InvalidOverrideError as exc:
            console.print(f"[red]{exc}[/red]")
            return None
    finally:
        connection.close()

    if json_output:
        console.print_json(data=override.to_dict())
    else:
        console.print(f"[green]Override created for media file {media_file_id}.[/green]")
        console.print(_render_override_text(override))
    return override


def run_identify_clear_override(
    config: AppConfig, media_file_id: int, *, json_output: bool = False
) -> identification_repository.OverrideRecord | None:
    """Clear the active override for a media file, if any. Effective
    identification reverts to the current parsed candidate. Returns None
    (after printing a message, mutating nothing) for an unknown
    media_file_id or when no override is currently active."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        media_file = inventory_repository.get_media_file(connection, media_file_id)
        if media_file is None:
            console.print(f"[red]No media file with id {media_file_id}.[/red]")
            return None
        with connection:
            cleared = identification_repository.clear_override(connection, media_file_id)
    finally:
        connection.close()

    if cleared is None:
        console.print(f"No active override for media file {media_file_id}; nothing to clear.")
        return None
    if json_output:
        console.print_json(data=cleared.to_dict())
    else:
        console.print(
            f"[yellow]Override cleared for media file {media_file_id}.[/yellow] "
            "Effective identification now reverts to the parsed candidate."
        )
    return cleared


def _render_effective_candidate_text(effective: identification_repository.EffectiveCandidate) -> str:
    lines = [f"Effective Identification -- Media File #{effective.media_file_id}", "=" * 46]
    lines.append(f"Source:     {effective.source}")
    lines.append(f"Type:       {effective.candidate_type}")
    if effective.candidate_type == "MOVIE":
        label = effective.parsed_title or "(unparsed)"
        if effective.parsed_year is not None:
            label += f" ({effective.parsed_year})"
        lines.append(f"Identity:   {label}")
    elif effective.candidate_type in ("EPISODE", "SPECIAL"):
        series = effective.parsed_series_title or "(unknown series)"
        if effective.season_number is not None and effective.episode_numbers:
            episodes = "".join(f"E{n:02d}" for n in effective.episode_numbers)
            label = f"{series} S{effective.season_number:02d}{episodes}"
        else:
            label = series
        if effective.episode_title:
            label += f" - {effective.episode_title}"
        lines.append(f"Identity:   {label}")
    lines.append(f"Confidence: {effective.confidence}")
    if effective.override_id is not None:
        lines.append(f"Override:   #{effective.override_id}")
    return "\n".join(lines)


def run_identify_show_effective(
    config: AppConfig, media_file_id: int, *, json_output: bool = False
) -> identification_repository.EffectiveCandidate | None:
    """Show the identification resolution/planning will actually use for a
    file right now: its active override if one exists, else its parsed
    candidate. Read-only. Returns None (after printing a clear error) if
    neither exists yet."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        effective = identification_repository.get_effective_candidate(connection, media_file_id)
    finally:
        connection.close()

    if effective is None:
        console.print(
            f"[red]No identification candidate for media file {media_file_id}. "
            "Run 'mams identify evaluate' first.[/red]"
        )
        return None
    if json_output:
        console.print_json(data=effective.to_dict())
    else:
        console.print(_render_effective_candidate_text(effective))
    return effective


def run_resolve_evaluate(
    config: AppConfig,
    *,
    media_file_id: int | None = None,
    candidate_id: int | None = None,
    category: str | None = None,
    limit: int = 10,
    force: bool = False,
    json_output: bool = False,
) -> list[resolution_repository.AttemptRecord]:
    """Resolve local identification candidates against TMDb, search ->
    score -> decide -> persist. Requires a configured TMDb token; prints
    a clear error and performs no writes at all (not even a FAILED
    attempt) if none is configured -- every other existing command is
    completely unaffected. Unlike `identify evaluate` (free, local),
    each candidate here costs a real TMDb request, so `--limit` defaults
    to a small number rather than evaluating unboundedly.
    """
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        try:
            provider = resolution_service.build_provider(config, connection)
        except resolution_service.TMDbNotConfiguredError as exc:
            console.print(f"[red]{exc}[/red]")
            return []

        if candidate_id is not None:
            single = identification_repository.get_candidate(connection, candidate_id)
            candidates = [single] if single is not None else []
        else:
            candidates = identification_repository.list_candidates(
                connection, media_file_id=media_file_id, category=category
            )

        if not force:
            candidates = [
                c for c in candidates
                if resolution_repository.get_latest_attempt_for_candidate(connection, c.id) is None
            ]
        candidates = candidates[:limit]

        attempts: list[resolution_repository.AttemptRecord] = []
        for candidate in candidates:
            media_file = inventory_repository.get_media_file(connection, candidate.media_file_id)
            local_runtime = media_file.duration_seconds if media_file is not None else None
            # Resolve against the effective candidate (an active manual
            # override, if any, else the plain parsed candidate) -- see
            # identification_repository.get_effective_candidate (Milestone
            # 7C, Phase D). `candidate` (the parsed row) still supplies the
            # attempt's id/media_file_id FK columns either way.
            effective = identification_repository.get_effective_candidate(connection, candidate.media_file_id)
            effective_ic = identification_repository.to_identification_candidate(effective) if effective else None
            override_id = effective.override_id if effective is not None else None
            attempts.append(
                resolution_service.evaluate_candidate(
                    connection,
                    provider,
                    candidate_record=candidate,
                    local_runtime_seconds=local_runtime,
                    effective_candidate=effective_ic,
                    override_id=override_id,
                )
            )
    finally:
        connection.close()

    if json_output:
        console.print_json(data=[attempt.to_dict() for attempt in attempts])
    else:
        console.print(_render_resolve_evaluate_text(attempts))
    return attempts


def _render_resolve_evaluate_text(attempts: list[resolution_repository.AttemptRecord]) -> str:
    lines = ["MAMS Resolution Evaluation", "=" * 26, "", f"Evaluated: {len(attempts)}"]
    if attempts:
        lines.append("")
        for attempt in attempts:
            top = attempt.matches[0] if attempt.matches else None
            top_label = f" -> {top.title or top.series_title}" if top is not None else ""
            lines.append(f"#{attempt.id} [{attempt.status}] {attempt.query_text or '(no query)'}{top_label}")
    return "\n".join(lines)


def _render_resolve_list_text(attempts: list[resolution_repository.AttemptRecord]) -> str:
    if not attempts:
        return "No matching resolution attempts."
    lines = [f"{len(attempts)} attempt(s)", "", f"{'STATUS':<16} {'CATEGORY':<10} {'QUERY':<30} PATH"]
    for attempt in attempts:
        query = (attempt.query_text or "")[:30]
        lines.append(f"{attempt.status:<16} {(attempt.category or '?'):<10} {query:<30} {attempt.absolute_path or '?'}")
    return "\n".join(lines)


def run_resolve_list(
    config: AppConfig,
    *,
    status: str | None = None,
    media_file_id: int | None = None,
    category: str | None = None,
    limit: int | None = None,
    json_output: bool = False,
) -> list[resolution_repository.AttemptRecord]:
    """List resolution attempts. Read-only."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        attempts = resolution_repository.list_attempts(
            connection, status=status, media_file_id=media_file_id, category=category, limit=limit
        )
    finally:
        connection.close()

    if json_output:
        console.print_json(data=[attempt.to_dict() for attempt in attempts])
    else:
        console.print(_render_resolve_list_text(attempts))
    return attempts


def _render_resolve_attempt_text(attempt: resolution_repository.AttemptRecord) -> str:
    lines = [f"Resolution Attempt #{attempt.id}", "=" * len(f"Resolution Attempt #{attempt.id}")]
    lines.append(f"Status:   {attempt.status}")
    lines.append(f"Provider: {attempt.provider}")
    query_year = f" ({attempt.query_year})" if attempt.query_year else ""
    lines.append(f"Query:    {attempt.query_text or '(none)'}{query_year}")
    if attempt.absolute_path:
        category = f"[{attempt.category}] " if attempt.category else ""
        lines.append(f"Path:     {category}{attempt.absolute_path}")
    if attempt.error_message:
        lines.append(f"Error:    {attempt.error_message}")
    lines.append("")
    lines.append("Ranked matches:")
    if not attempt.matches:
        lines.append("  (none)")
    for match in attempt.matches:
        marker = " [SELECTED]" if match.selected else ""
        label = match.title or match.series_title or "(untitled)"
        if match.season_number is not None and match.episode_number is not None:
            label += f" S{match.season_number:02d}E{match.episode_number:02d}"
        lines.append(f"  #{match.rank} match_id={match.id} score={match.score:.4f} {label}{marker}")
        for reason in cast("list[str]", match.scoring.get("reasons", [])):
            lines.append(f"      - {reason}")
    lines.append("")
    lines.append(f"Started:   {attempt.started_at}")
    if attempt.completed_at:
        lines.append(f"Completed: {attempt.completed_at}")
    return "\n".join(lines)


def run_resolve_show(
    config: AppConfig, attempt_id: int, *, json_output: bool = False
) -> resolution_repository.AttemptRecord | None:
    """Show one resolution attempt with its ranked alternatives. Read-only."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        attempt = resolution_repository.get_attempt(connection, attempt_id)
    finally:
        connection.close()

    if attempt is None:
        console.print(f"[red]Resolution attempt {attempt_id} not found.[/red]")
        return None

    if json_output:
        console.print_json(data=attempt.to_dict())
    else:
        console.print(_render_resolve_attempt_text(attempt))
    return attempt


def run_resolve_select(
    config: AppConfig, attempt_id: int, match_id: int, *, json_output: bool = False
) -> resolution_repository.AttemptRecord | None:
    """Manually confirm one ranked match as the file's identity. Marks
    the attempt RESOLVED and creates/updates the ACTIVE
    media_identity_assignment with assignment_method=MANUAL. Writes no
    media files."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        attempt = resolution_repository.get_attempt(connection, attempt_id)
        if attempt is None:
            console.print(f"[red]Resolution attempt {attempt_id} not found.[/red]")
            return None
        if not any(match.id == match_id for match in attempt.matches):
            console.print(f"[red]Match {match_id} does not belong to attempt {attempt_id}.[/red]")
            return None
        updated = resolution_service.select_match_manually(connection, attempt_id=attempt_id, match_id=match_id)
    finally:
        connection.close()

    if json_output:
        console.print_json(data=updated.to_dict())
    else:
        console.print(f"[green]Attempt {attempt_id} resolved via manual selection of match {match_id}.[/green]")
        console.print(_render_resolve_attempt_text(updated))
    return updated


def run_resolve_reject(
    config: AppConfig, attempt_id: int, *, json_output: bool = False
) -> resolution_repository.AttemptRecord | None:
    """Manually reject every match for a resolution attempt. Marks the
    attempt NO_MATCH, creates no assignment, and preserves the ranked
    alternatives."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        attempt = resolution_repository.get_attempt(connection, attempt_id)
        if attempt is None:
            console.print(f"[red]Resolution attempt {attempt_id} not found.[/red]")
            return None
        updated = resolution_service.reject_match_manually(connection, attempt_id=attempt_id)
    finally:
        connection.close()

    if json_output:
        console.print_json(data=updated.to_dict())
    else:
        console.print(f"[yellow]Attempt {attempt_id} marked NO_MATCH (manually rejected).[/yellow]")
        console.print(_render_resolve_attempt_text(updated))
    return updated


def _render_resolve_stats_text(stats: resolution_repository.AttemptStats) -> str:
    lines = ["MAMS Resolution Statistics", "=" * 26, "", f"Total: {stats.total_count}"]
    lines.append("By status: " + (", ".join(f"{k}={v}" for k, v in sorted(stats.status_counts.items())) or "none"))
    return "\n".join(lines)


def run_resolve_stats(config: AppConfig, *, json_output: bool = False) -> resolution_repository.AttemptStats:
    """Show resolution attempt statistics. Read-only."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        stats = resolution_repository.get_attempt_stats(connection)
    finally:
        connection.close()

    if json_output:
        console.print_json(data=stats.to_dict())
    else:
        console.print(_render_resolve_stats_text(stats))
    return stats


def _render_provider_status_text(result: dict[str, object]) -> str:
    lines = ["TMDb Provider Status", "=" * 21, ""]
    lines.append(f"Configured: {'yes' if result['configured'] else 'no'}")
    if result.get("authentication") is not None:
        lines.append(f"Authentication: {result['authentication']}")
    if result.get("cache_available") is not None:
        lines.append(f"Cache: {'available' if result['cache_available'] else 'unavailable'}")
    if result.get("network_ok") is not None:
        lines.append(f"Network request: {'successful' if result['network_ok'] else 'failed'}")
    lines.append(f"Provider: {result['provider']}")
    if result.get("error"):
        lines.append("")
        lines.append(str(result["error"]))
    return "\n".join(lines)


def run_resolve_provider_status(config: AppConfig, *, json_output: bool = False) -> dict[str, object]:
    """Diagnose TMDb configuration/authentication with at most one minimal,
    cached HTTP request (`TMDbClient.verify_credentials`). Never prints,
    logs, or returns the token itself -- distinguishes not-configured,
    invalid token, rate limited, network failure, malformed response, and
    success. Applies pending migrations first (a cache-backed provider
    needs a database), but writes nothing beyond whatever
    `verify_credentials()` itself caches."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        result: dict[str, object] = {"provider": "TMDb", "configured": config.tmdb_token is not None}
        if not result["configured"]:
            result["authentication"] = None
            result["cache_available"] = None
            result["network_ok"] = None
            result["error"] = (
                "No TMDb API token configured. Set the environment variable named by "
                "config.yaml's tmdb.token_env_var (default TMDB_API_TOKEN)."
            )
        else:
            provider = resolution_service.build_provider(config, connection)
            result["cache_available"] = True
            try:
                provider.verify_credentials()
            except TMDbAuthenticationError as exc:
                result["authentication"] = "invalid"
                result["network_ok"] = True
                result["error"] = str(exc)
            except TMDbRateLimitError as exc:
                result["authentication"] = "unknown"
                result["network_ok"] = False
                result["error"] = str(exc)
            except (TMDbTimeoutError, TMDbConnectionError, TMDbResponseError) as exc:
                result["authentication"] = "unknown"
                result["network_ok"] = False
                result["error"] = str(exc)
            else:
                result["authentication"] = "valid"
                result["network_ok"] = True
    finally:
        connection.close()

    if json_output:
        console.print_json(data=result)
    else:
        console.print(_render_provider_status_text(result))
    return result


def _render_cache_stats_text(stats: provider_cache_repository.CacheStats) -> str:
    lines = ["MAMS Provider Cache Statistics", "=" * 30, ""]
    lines.append(f"Total:   {stats.total_count}")
    lines.append(f"Fresh:   {stats.fresh_count}")
    lines.append(f"Expired: {stats.expired_count}")
    return "\n".join(lines)


def run_resolve_cache_stats(config: AppConfig, *, json_output: bool = False) -> provider_cache_repository.CacheStats:
    """Show provider_cache statistics. Read-only."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        stats = provider_cache_repository.get_cache_stats(connection)
    finally:
        connection.close()

    if json_output:
        console.print_json(data=stats.to_dict())
    else:
        console.print(_render_cache_stats_text(stats))
    return stats


def run_resolve_cache_clear(config: AppConfig, *, json_output: bool = False) -> int:
    """Delete expired provider_cache rows only. Never touches external
    identities, resolution attempts/assignments, or ingest plans -- those
    tables have no foreign key to provider_cache at all (see
    docs/DATABASE.md)."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        deleted = provider_cache_repository.clear_expired_entries(connection)
    finally:
        connection.close()

    if json_output:
        console.print_json(data={"deleted": deleted})
    else:
        console.print(f"Deleted {deleted} expired cache entr{'y' if deleted == 1 else 'ies'}.")
    return deleted


def _render_ingest_plan_text(plan: ingest_repository.PlanRecord) -> str:
    lines = ["MAMS Dry-Run Ingest Plan", "=" * 24, "", f"Plan #{plan.id}"]
    lines.append(f"Status: {plan.status}")
    lines.append("Execution: NOT PERFORMED")
    lines.append("")
    lines.append("Source:")
    lines.append(f"  {plan.source_path}")
    if plan.verification_status:
        lines.append("")
        lines.append(f"Verification: {plan.verification_status}")
        if plan.verification:
            for check in cast("list[dict[str, object]]", plan.verification.get("checks", [])):
                lines.append(f"  {check['code']}: {check['status']}")
    if plan.destination_directory and plan.destination_filename:
        lines.append("")
        lines.append("Proposed destination:")
        lines.append(f"  {plan.destination_directory}/{plan.destination_filename}")
    if plan.blocking_reasons:
        lines.append("")
        lines.append("Reasons:")
        for reason in plan.blocking_reasons:
            lines.append(f"  - {reason}")
    if plan.actions:
        lines.append("")
        lines.append("Proposed actions:")
        for action in plan.actions:
            lines.append(f"  {action.action_order}. {action.action_type} (PROPOSED -- NOT EXECUTED)")
    lines.append("")
    lines.append("No actions were executed.")
    return "\n".join(lines)


def run_ingest_plan(
    config: AppConfig, media_file_id: int, *, destination_category: str | None = None, json_output: bool = False
) -> ingest_repository.PlanRecord | None:
    """Generate (or reconcile) the current dry-run ingest plan for one
    media file. No filesystem changes; no Plex call. Returns None (after
    printing a clear error) for a usage-level rejection -- unknown id,
    not under a configured incoming root, or an invalid destination
    category."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        try:
            plan = ingest_service.generate_plan(
                connection, config, media_file_id=media_file_id, destination_category=destination_category
            )
        except ingest_service.IngestPlanError as exc:
            console.print(f"[red]{exc}[/red]")
            return None
    finally:
        connection.close()

    if json_output:
        console.print_json(data=plan.to_dict())
    else:
        console.print(_render_ingest_plan_text(plan))
    return plan


def _render_ingest_plans_text(plans: list[ingest_repository.PlanRecord]) -> str:
    if not plans:
        return "No matching plans."
    lines = [f"{len(plans)} plan(s)", "", f"{'STATUS':<18} {'CATEGORY':<10} {'DESTINATION':<40} SOURCE"]
    for plan in plans:
        destination = f"{plan.destination_directory}/{plan.destination_filename}" if plan.destination_filename else "(none)"
        lines.append(f"{plan.status:<18} {(plan.category or '?'):<10} {destination[:40]:<40} {plan.source_path}")
    return "\n".join(lines)


def run_ingest_plans(
    config: AppConfig,
    *,
    status: str | None = None,
    category: str | None = None,
    limit: int | None = None,
    json_output: bool = False,
) -> list[ingest_repository.PlanRecord]:
    """List dry-run ingest plans. Read-only."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        plans = ingest_repository.list_plans(connection, status=status, category=category, limit=limit)
    finally:
        connection.close()

    if json_output:
        console.print_json(data=[plan.to_dict() for plan in plans])
    else:
        console.print(_render_ingest_plans_text(plans))
    return plans


def run_ingest_show(config: AppConfig, plan_id: int, *, json_output: bool = False) -> ingest_repository.PlanRecord | None:
    """Show one dry-run ingest plan with its proposed actions. Read-only."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        plan = ingest_repository.get_plan(connection, plan_id)
    finally:
        connection.close()

    if plan is None:
        console.print(f"[red]Ingest plan {plan_id} not found.[/red]")
        return None

    if json_output:
        console.print_json(data=plan.to_dict())
    else:
        console.print(_render_ingest_plan_text(plan))
    return plan


def _render_ingest_stats_text(stats: ingest_repository.PlanStats) -> str:
    lines = ["MAMS Ingest Plan Statistics", "=" * 27, "", f"Total: {stats.total_count}"]
    lines.append("By status: " + (", ".join(f"{k}={v}" for k, v in sorted(stats.status_counts.items())) or "none"))
    return "\n".join(lines)


def run_ingest_stats(config: AppConfig, *, json_output: bool = False) -> ingest_repository.PlanStats:
    """Show dry-run ingest plan statistics. Read-only."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        stats = ingest_repository.get_plan_stats(connection)
    finally:
        connection.close()

    if json_output:
        console.print_json(data=stats.to_dict())
    else:
        console.print(_render_ingest_stats_text(stats))
    return stats


def run_ingest_approve(config: AppConfig, plan_id: int, *, json_output: bool = False) -> ingest_repository.PlanRecord | None:
    """Mark a READY_FOR_REVIEW plan APPROVED. Database state only -- no
    filesystem change, no Plex call. Returns None (after printing a clear
    error) if the plan doesn't exist or isn't READY_FOR_REVIEW."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        try:
            plan = ingest_service.approve(connection, plan_id)
        except ingest_repository.PlanNotApprovableError as exc:
            console.print(f"[red]{exc}[/red]")
            return None
    finally:
        connection.close()

    if json_output:
        console.print_json(data=plan.to_dict())
    else:
        console.print("Plan approved. No actions executed.")
    return plan


def _render_readiness_text(
    plan_id: int,
    result: readiness.ReadinessResult,
    *,
    last_execution: execution_repository.ExecutionRecord | None = None,
) -> str:
    lines = ["MAMS Execution-Readiness Audit", "=" * 30, "", f"Plan #{plan_id}", f"Status: {result.readiness_status.value}"]
    lines.append("")
    lines.append("Checks")
    for check in result.checks:
        marker = "PASS" if check.passed else "FAIL"
        lines.append(f"  {marker:<4}  {check.code}")
    lines.append("")
    if last_execution is not None:
        lines.append(f"Last execution: #{last_execution.id} ({last_execution.status})")
    else:
        lines.append("Last execution: none")
    lines.append("")
    lines.append("EXECUTION WAS NOT PERFORMED.")
    return "\n".join(lines)


def run_ingest_audit(
    config: AppConfig, plan_id: int, *, json_output: bool = False
) -> readiness.ReadinessResult | None:
    """Read-only execution-readiness audit for an approved dry-run ingest
    plan (Milestone 7C). Never executes anything -- see
    ingest_service.audit_plan/readiness.evaluate_readiness. Returns None
    (after printing a clear error, mutating nothing) for an unknown
    plan_id.

    `readiness.py` stays pure and unaware of Milestone 8's execution
    tables by design, so this function separately looks up the plan's
    most recent execution (if any) and displays it alongside the
    readiness result, rather than the audit itself gaining execution
    awareness."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        try:
            result = ingest_service.audit_plan(connection, config, plan_id=plan_id)
        except ingest_service.IngestPlanError as exc:
            console.print(f"[red]{exc}[/red]")
            return None
        last_execution = execution_repository.get_current_execution_for_plan(connection, plan_id)
    finally:
        connection.close()

    if json_output:
        data = result.to_dict()
        data["last_execution"] = last_execution.to_dict() if last_execution is not None else None
        console.print_json(data=data)
    else:
        console.print(_render_readiness_text(plan_id, result, last_execution=last_execution))
    return result


def _render_execution_preview_text(preview: execution_service.ExecutionPreview) -> str:
    lines = [
        "MAMS Approved-Plan Execution",
        "=" * 29,
        "",
        f"Plan:        #{preview.plan_id}",
        f"Plan status: {preview.plan_status}",
        f"Source:      {preview.source_path}",
        f"Destination: {preview.destination_path or '(none)'}",
        f"Strategy:    {preview.transfer_strategy or '(unknown -- source or destination root not reachable)'}",
        "Overwrite:   DISABLED",
        f"Audit:       {preview.readiness_status}",
        "",
        "This command will modify the filesystem.",
        "",
        "NO ACTIONS WERE EXECUTED.",
        "",
        "Re-run with:",
        "",
        f"  mams ingest execute {preview.plan_id} --confirm-plan {preview.plan_id}",
        "",
        "to execute this exact plan.",
    ]
    return "\n".join(lines)


def _render_execution_result_text(execution: execution_repository.ExecutionRecord) -> str:
    lines = [
        "MAMS Ingest Execution",
        "=" * 22,
        "",
        f"Execution: #{execution.id}",
        f"Plan:      #{execution.ingest_plan_id}",
        f"Status:    {execution.status}",
        "",
        "Source:",
        f"  {execution.source_path}",
        "",
        "Destination:",
        f"  {execution.destination_path}",
        "",
        "Strategy:",
        f"  {execution.transfer_strategy}",
    ]
    if execution.status == "SUCCEEDED":
        plex_line = execution.plex_refresh_status or "SKIPPED"
        if plex_line == "SKIPPED":
            plex_line += " -- disabled by configuration"
        lines += [
            "",
            "Verification:",
            "  PASS",
            "",
            "Inventory:",
            "  Destination ACTIVE",
            "  Source reconciled",
            "",
            "Plex:",
            f"  {plex_line}",
            "",
            "Execution completed successfully.",
        ]
    else:
        lines += [
            "",
            f"Execution {execution.status}.",
            "",
            f"Failed step:      {execution.failure_step}",
            f"Recovery status:  {execution.recovery_status}",
            "",
            f"Run `mams ingest recovery {execution.id}` for full evidence and guidance.",
            "Nothing was auto-retried.",
        ]
    return "\n".join(lines)


def run_ingest_execute(
    config: AppConfig, plan_id: int, *, confirm_plan: int | None, json_output: bool = False
) -> execution_repository.ExecutionRecord | None:
    """Execute one APPROVED, READY_FOR_EXECUTOR ingest plan. Requires an
    exact `--confirm-plan` match for the same plan id; without it,
    prints the preview and performs no filesystem or database mutation.
    Returns None (after printing a clear error) for any usage-level
    rejection -- see execution_service.ExecutionUsageError."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        try:
            preview = execution_service.preview_execution(connection, config, plan_id=plan_id)
        except execution_service.PlanNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            return None

        if confirm_plan != plan_id:
            if json_output:
                data = preview.to_dict()
                data["confirmed"] = False
                data["execution_status"] = "NOT_EXECUTED"
                console.print_json(data=data)
            else:
                console.print(_render_execution_preview_text(preview))
            return None

        try:
            execution = execution_service.execute_plan(connection, config, plan_id=plan_id)
        except execution_service.ExecutionUsageError as exc:
            console.print(f"[red]{exc}[/red]")
            return None
    finally:
        connection.close()

    if json_output:
        console.print_json(data=execution.to_dict())
    else:
        console.print(_render_execution_result_text(execution))
    return execution


def _render_executions_text(executions: list[execution_repository.ExecutionRecord]) -> str:
    if not executions:
        return "No matching executions."
    lines = [f"{len(executions)} execution(s)", "", f"{'ID':<6} {'PLAN':<6} {'STATUS':<18} {'RECOVERY':<40} DESTINATION"]
    for execution in executions:
        lines.append(
            f"{execution.id:<6} {execution.ingest_plan_id:<6} {execution.status:<18} "
            f"{(execution.recovery_status or '-'):<40} {execution.destination_path}"
        )
    return "\n".join(lines)


def run_ingest_executions(
    config: AppConfig,
    *,
    plan_id: int | None = None,
    status: str | None = None,
    recovery_status: str | None = None,
    limit: int | None = None,
    json_output: bool = False,
) -> list[execution_repository.ExecutionRecord]:
    """List Milestone 8 execution history. Read-only."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        executions = execution_repository.list_executions(
            connection, status=status, recovery_status=recovery_status, plan_id=plan_id, limit=limit
        )
    finally:
        connection.close()

    if json_output:
        console.print_json(data=[execution.to_dict() for execution in executions])
    else:
        console.print(_render_executions_text(executions))
    return executions


def run_ingest_execution(
    config: AppConfig, execution_id: int, *, json_output: bool = False
) -> execution_repository.ExecutionRecord | None:
    """Show one execution's full step history. Read-only."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        execution = execution_repository.get_execution(connection, execution_id)
    finally:
        connection.close()

    if execution is None:
        console.print(f"[red]Execution {execution_id} not found.[/red]")
        return None

    if json_output:
        console.print_json(data=execution.to_dict())
    else:
        console.print(_render_execution_result_text(execution))
    return execution


def _render_recovery_text(guidance: execution_service.RecoveryGuidance) -> str:
    lines = [
        "MAMS Execution Recovery",
        "=" * 24,
        "",
        f"Execution: #{guidance.execution_id}",
        f"Plan:      #{guidance.plan_id}",
        f"Execution status: {guidance.execution_status}",
        f"Recovery status:  {guidance.recovery_status}",
        "",
        f"Source exists:               {guidance.source_exists}",
        f"Destination exists:          {guidance.destination_exists}",
        f"Temp file exists:            {guidance.temp_file_exists}",
        f"Temp file path(s):           {', '.join(guidance.temp_file_paths) if guidance.temp_file_paths else '(none found)'}",
        f"Lock held:                   {guidance.lock_held}",
        f"Lock matches this execution: {guidance.lock_matches_execution}",
        "",
        "Recommendation:",
        f"  {guidance.recommendation}",
        "",
        "NO ACTIONS WERE TAKEN. This command is read-only.",
    ]
    return "\n".join(lines)


def run_ingest_recovery(
    config: AppConfig, execution_id: int, *, json_output: bool = False
) -> execution_service.RecoveryGuidance | None:
    """Read-only recovery guidance for one execution. Never mutates the
    database or filesystem -- see execution_service.inspect_recovery."""
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        guidance = execution_service.inspect_recovery(connection, config, execution_id=execution_id)
    finally:
        connection.close()

    if guidance is None:
        console.print(f"[red]Execution {execution_id} not found.[/red]")
        return None

    if json_output:
        console.print_json(data=guidance.to_dict())
    else:
        console.print(_render_recovery_text(guidance))
    return guidance


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
            try:
                run_inventory_scan(
                    config,
                    json_output=args.json,
                    output=args.output,
                    metadata=args.metadata,
                    use_db=not args.no_db,
                    category=args.category,
                )
            except InventoryScanScopeError as exc:
                console.print(f"[red]{exc}[/red]")
                raise SystemExit(1) from None
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
        elif args.identify_command == "override":
            run_identify_override(
                config,
                args.media_file_id,
                candidate_type=args.candidate_type,
                title=args.title,
                year=args.year,
                series_title=args.series_title,
                season_number=args.season_number,
                episode_number=args.episode_number,
                episode_title=args.episode_title,
                reason=args.reason,
                json_output=args.json,
            )
        elif args.identify_command == "clear-override":
            run_identify_clear_override(config, args.media_file_id, json_output=args.json)
        elif args.identify_command == "show-effective":
            run_identify_show_effective(config, args.media_file_id, json_output=args.json)
    elif args.command == "resolve":
        if args.resolve_command == "evaluate":
            run_resolve_evaluate(
                config,
                media_file_id=args.media_file_id,
                candidate_id=args.candidate_id,
                category=args.category,
                limit=args.limit,
                force=args.force,
                json_output=args.json,
            )
        elif args.resolve_command == "list":
            run_resolve_list(
                config,
                status=args.status,
                media_file_id=args.media_file_id,
                category=args.category,
                limit=args.limit,
                json_output=args.json,
            )
        elif args.resolve_command == "show":
            run_resolve_show(config, args.attempt_id, json_output=args.json)
        elif args.resolve_command == "select":
            run_resolve_select(config, args.attempt_id, args.match_id, json_output=args.json)
        elif args.resolve_command == "reject":
            run_resolve_reject(config, args.attempt_id, json_output=args.json)
        elif args.resolve_command == "stats":
            run_resolve_stats(config, json_output=args.json)
        elif args.resolve_command == "provider-status":
            run_resolve_provider_status(config, json_output=args.json)
        elif args.resolve_command == "cache-stats":
            run_resolve_cache_stats(config, json_output=args.json)
        elif args.resolve_command == "cache-clear":
            run_resolve_cache_clear(config, json_output=args.json)
    elif args.command == "ingest":
        if args.ingest_command == "plan":
            run_ingest_plan(
                config, args.media_file_id, destination_category=args.destination_category, json_output=args.json
            )
        elif args.ingest_command == "plans":
            run_ingest_plans(config, status=args.status, category=args.category, limit=args.limit, json_output=args.json)
        elif args.ingest_command == "show":
            run_ingest_show(config, args.plan_id, json_output=args.json)
        elif args.ingest_command == "stats":
            run_ingest_stats(config, json_output=args.json)
        elif args.ingest_command == "approve":
            run_ingest_approve(config, args.plan_id, json_output=args.json)
        elif args.ingest_command == "audit":
            run_ingest_audit(config, args.plan_id, json_output=args.json)
        elif args.ingest_command == "execute":
            run_ingest_execute(config, args.plan_id, confirm_plan=args.confirm_plan, json_output=args.json)
        elif args.ingest_command == "executions":
            run_ingest_executions(
                config, plan_id=args.plan_id, status=args.status, recovery_status=args.recovery_status,
                limit=args.limit, json_output=args.json,
            )
        elif args.ingest_command == "execution":
            run_ingest_execution(config, args.execution_id, json_output=args.json)
        elif args.ingest_command == "recovery":
            run_ingest_recovery(config, args.execution_id, json_output=args.json)
    elif args.command == "mediainfo":
        run_mediainfo(args.path, json_output=args.json)

if __name__ == "__main__":
    main()
