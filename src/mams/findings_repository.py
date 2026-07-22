"""Persistence for findings: reconciles FindingCandidate objects produced by
`findings.evaluate_all()` into the `findings` table, and (see the query
layer section below) provides read-only list/get/stats access for the CLI.

This module owns all findings-related SQL, mirroring inventory_repository.
py's role for the inventory schema. Rule evaluation itself lives in
findings.py and never touches SQLite; this module never evaluates rules,
only reconciles and reads already-computed candidates/rows.

## Reconciliation lifecycle

`reconcile_findings()` is given the full candidate set for *all* rules
against *all* current media_files rows (both ACTIVE and MISSING -- see
findings_service.py). It is expected to run to completion or not at all;
callers wrap it in `with connection:` so a mid-reconciliation exception
rolls back every write made so far (see findings_service.evaluate_findings).

For each candidate, keyed by (rule_code, media_file_id):

- No existing row -> INSERT a new ACTIVE finding. first_detected_at and
  last_detected_at both default to CURRENT_TIMESTAMP.
- Existing row, status ACTIVE -> preserve id and first_detected_at. Always
  bump last_detected_at (the condition was reconfirmed this run). Only
  bump updated_at -- and only touch severity/summary/evidence_json/
  recommendation/library_id -- if that content actually changed, so a
  repeated evaluation against unchanged inventory produces no unnecessary
  updated_at churn.
- Existing row, status RESOLVED -> reactivate: status back to ACTIVE,
  resolved_at cleared, content refreshed to the current candidate,
  last_detected_at and updated_at bumped. first_detected_at is
  deliberately left untouched -- the finding's original detection time is
  preserved across a resolve/reactivate cycle.
- Existing row, status IGNORED -> never touch status or resolved_at (see
  below). Only last_detected_at is bumped, recording that the condition is
  still present without disturbing the user's ignore decision.

Any existing ACTIVE row whose (rule_code, media_file_id) key did not appear
in this run's candidates (the condition is no longer present) is marked
RESOLVED with resolved_at set. RESOLVED rows not in the candidate set are
already resolved and are left alone; IGNORED rows not in the candidate set
are also left alone -- see "IGNORED findings" below.

## IGNORED findings

No CLI command in this milestone sets a finding to IGNORED (see
docs/DATABASE.md and cli.py's `findings` subcommands) -- schema support
and this reconciliation behavior exist so a future milestone can add one
without a schema or lifecycle change. Whichever way an IGNORED row gets
there, reconciliation never flips it back to ACTIVE or forward to RESOLVED
automatically in either direction (condition still present, or condition
gone): that would silently erase a user's explicit decision to suppress a
finding. Only last_detected_at moves, and only while the condition is
still detected.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass

from .findings import ALL_RULES, FindingCandidate

_SEVERITY_ORDER: dict[str, int] = {"CRITICAL": 0, "ERROR": 1, "WARNING": 2, "INFO": 3}
_RULE_ORDER: dict[str, int] = {rule.code: index for index, rule in enumerate(ALL_RULES)}


def _serialize_evidence(evidence: dict[str, object]) -> str | None:
    """NULL for no evidence, otherwise a stable, sorted, compact encoding --
    same convention as scan_changes.details_json (see docs/DATABASE.md)."""
    if not evidence:
        return None
    return json.dumps(evidence, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ReconciliationResult:
    """Summary of one `reconcile_findings()` call, for CLI display and
    lifecycle-determinism tests."""

    created: int
    reactivated: int
    updated: int
    unchanged: int
    resolved: int
    ignored_touched: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _existing_findings_by_key(connection: sqlite3.Connection) -> dict[tuple[str, int | None], sqlite3.Row]:
    rows = connection.execute("SELECT * FROM findings").fetchall()
    return {(row["rule_code"], row["media_file_id"]): row for row in rows}


def _content_changed(existing: sqlite3.Row, candidate: FindingCandidate, evidence_json: str | None) -> bool:
    return (
        existing["severity"] != candidate.severity.value
        or existing["library_id"] != candidate.library_id
        or existing["summary"] != candidate.summary
        or existing["evidence_json"] != evidence_json
        or existing["recommendation"] != candidate.recommendation
    )


def _insert_finding(connection: sqlite3.Connection, candidate: FindingCandidate, evidence_json: str | None) -> None:
    connection.execute(
        """
        INSERT INTO findings (
            rule_code, severity, status, media_file_id, library_id,
            summary, evidence_json, recommendation
        ) VALUES (?, ?, 'ACTIVE', ?, ?, ?, ?, ?)
        """,
        (
            candidate.rule_code,
            candidate.severity.value,
            candidate.media_file_id,
            candidate.library_id,
            candidate.summary,
            evidence_json,
            candidate.recommendation,
        ),
    )


def _touch_last_detected(connection: sqlite3.Connection, finding_id: int) -> None:
    connection.execute("UPDATE findings SET last_detected_at = CURRENT_TIMESTAMP WHERE id = ?", (finding_id,))


def _update_active_content(
    connection: sqlite3.Connection, finding_id: int, candidate: FindingCandidate, evidence_json: str | None
) -> None:
    connection.execute(
        """
        UPDATE findings
        SET severity = ?, library_id = ?, summary = ?, evidence_json = ?, recommendation = ?,
            last_detected_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            candidate.severity.value,
            candidate.library_id,
            candidate.summary,
            evidence_json,
            candidate.recommendation,
            finding_id,
        ),
    )


def _reactivate(
    connection: sqlite3.Connection, finding_id: int, candidate: FindingCandidate, evidence_json: str | None
) -> None:
    connection.execute(
        """
        UPDATE findings
        SET status = 'ACTIVE', resolved_at = NULL, severity = ?, library_id = ?,
            summary = ?, evidence_json = ?, recommendation = ?,
            last_detected_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            candidate.severity.value,
            candidate.library_id,
            candidate.summary,
            evidence_json,
            candidate.recommendation,
            finding_id,
        ),
    )


def _resolve(connection: sqlite3.Connection, finding_id: int) -> None:
    connection.execute(
        """
        UPDATE findings
        SET status = 'RESOLVED', resolved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (finding_id,),
    )


def reconcile_findings(connection: sqlite3.Connection, candidates: list[FindingCandidate]) -> ReconciliationResult:
    """Reconcile a full-inventory candidate set into the findings table.

    Not itself transactional -- callers run this inside `with connection:`
    (see findings_service.evaluate_findings) so a failure partway through
    rolls back every write this call has made, leaving no partially
    updated findings set. See the module docstring for the full lifecycle.
    """
    existing_by_key = _existing_findings_by_key(connection)

    created = reactivated = updated = unchanged = ignored_touched = 0
    seen_keys: set[tuple[str, int | None]] = set()

    for candidate in candidates:
        key = (candidate.rule_code, candidate.media_file_id)
        seen_keys.add(key)
        evidence_json = _serialize_evidence(candidate.evidence)
        existing = existing_by_key.get(key)

        if existing is None:
            _insert_finding(connection, candidate, evidence_json)
            created += 1
            continue

        if existing["status"] == "IGNORED":
            _touch_last_detected(connection, existing["id"])
            ignored_touched += 1
            continue

        if existing["status"] == "RESOLVED":
            _reactivate(connection, existing["id"], candidate, evidence_json)
            reactivated += 1
            continue

        # status == "ACTIVE"
        if _content_changed(existing, candidate, evidence_json):
            _update_active_content(connection, existing["id"], candidate, evidence_json)
            updated += 1
        else:
            _touch_last_detected(connection, existing["id"])
            unchanged += 1

    resolved = 0
    for existing_key, row in existing_by_key.items():
        if existing_key in seen_keys or row["status"] != "ACTIVE":
            continue
        _resolve(connection, row["id"])
        resolved += 1

    return ReconciliationResult(
        created=created,
        reactivated=reactivated,
        updated=updated,
        unchanged=unchanged,
        resolved=resolved,
        ignored_touched=ignored_touched,
    )


# --- query layer (list / get / stats) ---------------------------------------
#
# Read-only access for `mams findings list/show/stats`. Every function here
# runs a fixed, small number of queries regardless of how many findings
# rows match -- never one query per row. Path/category are resolved via
# LEFT JOIN at read time (never stored on findings -- see docs/DATABASE.md),
# the same pattern scan_changes uses for category.


def _case_rank_sql(column: str, order: dict[str, int]) -> str:
    """A CASE expression ranking `column`'s values by `order`, for a stable
    ORDER BY that doesn't match SQL's default alphabetical text ordering.
    `order`'s keys are this module's own fixed constants, never external
    input, so direct interpolation into the SQL text is safe."""
    cases = " ".join(f"WHEN '{value}' THEN {rank}" for value, rank in order.items())
    return f"CASE {column} {cases} ELSE {len(order)} END"


_SEVERITY_RANK_SQL = _case_rank_sql("findings.severity", _SEVERITY_ORDER)
_RULE_RANK_SQL = _case_rank_sql("findings.rule_code", _RULE_ORDER)

_FINDING_BASE_SELECT = f"""
    SELECT
        findings.*,
        libraries.category AS category,
        media_files.absolute_path AS absolute_path,
        media_files.relative_path AS relative_path,
        {_SEVERITY_RANK_SQL} AS severity_rank,
        {_RULE_RANK_SQL} AS rule_rank
    FROM findings
    LEFT JOIN libraries ON libraries.id = findings.library_id
    LEFT JOIN media_files ON media_files.id = findings.media_file_id
"""

_FINDING_ORDER_BY = """
    ORDER BY
        COALESCE(libraries.category, ''),
        COALESCE(media_files.relative_path, ''),
        severity_rank,
        rule_rank,
        findings.id
"""


@dataclass(frozen=True)
class FindingRecord:
    """One findings row, with category/path resolved via join and
    evidence_json parsed back into a dict."""

    id: int
    rule_code: str
    severity: str
    status: str
    media_file_id: int | None
    library_id: int | None
    category: str | None
    absolute_path: str | None
    relative_path: str | None
    summary: str
    evidence: dict[str, object] | None
    recommendation: str | None
    first_detected_at: str
    last_detected_at: str
    resolved_at: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class FindingsStats:
    total_count: int
    active_count: int
    resolved_count: int
    ignored_count: int
    severity_counts: dict[str, int]
    rule_counts: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _row_to_finding_record(row: sqlite3.Row) -> FindingRecord:
    evidence = json.loads(row["evidence_json"]) if row["evidence_json"] is not None else None
    return FindingRecord(
        id=row["id"],
        rule_code=row["rule_code"],
        severity=row["severity"],
        status=row["status"],
        media_file_id=row["media_file_id"],
        library_id=row["library_id"],
        category=row["category"],
        absolute_path=row["absolute_path"],
        relative_path=row["relative_path"],
        summary=row["summary"],
        evidence=evidence,
        recommendation=row["recommendation"],
        first_detected_at=row["first_detected_at"],
        last_detected_at=row["last_detected_at"],
        resolved_at=row["resolved_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_findings(
    connection: sqlite3.Connection,
    *,
    status: str | None = None,
    severity: str | None = None,
    rule_code: str | None = None,
    category: str | None = None,
    media_file_id: int | None = None,
    limit: int | None = None,
) -> list[FindingRecord]:
    """List findings rows for browsing, with deterministic ordering.

    All filters are optional and combined with AND. Ordered by (category,
    relative_path, severity rank, rule rank, id): findings are grouped by
    the file they concern, then by severity (CRITICAL first), then by the
    rules' declared order in findings.ALL_RULES -- stable regardless of
    insertion order. Single query with two LEFT JOINs, never one query per
    row.
    """
    clauses: list[str] = []
    params: list[object] = []
    if status is not None:
        clauses.append("findings.status = ?")
        params.append(status)
    if severity is not None:
        clauses.append("findings.severity = ?")
        params.append(severity)
    if rule_code is not None:
        clauses.append("findings.rule_code = ?")
        params.append(rule_code)
    if category is not None:
        clauses.append("libraries.category = ?")
        params.append(category)
    if media_file_id is not None:
        clauses.append("findings.media_file_id = ?")
        params.append(media_file_id)

    sql = _FINDING_BASE_SELECT
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += _FINDING_ORDER_BY
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    rows = connection.execute(sql, params).fetchall()
    return [_row_to_finding_record(row) for row in rows]


def get_finding(connection: sqlite3.Connection, finding_id: int) -> FindingRecord | None:
    """Look up a single finding by id, or None if it doesn't exist."""
    sql = _FINDING_BASE_SELECT + " WHERE findings.id = ?"
    row = connection.execute(sql, (finding_id,)).fetchone()
    return _row_to_finding_record(row) if row is not None else None


def get_findings_stats(connection: sqlite3.Connection) -> FindingsStats:
    """Aggregate findings statistics in three fixed queries, none of them
    per-row: counts by status, and (ACTIVE findings only) counts by
    severity and by rule_code."""
    status_counts = {
        row["status"]: row["n"]
        for row in connection.execute("SELECT status, COUNT(*) AS n FROM findings GROUP BY status").fetchall()
    }
    severity_counts = {
        row["severity"]: row["n"]
        for row in connection.execute(
            "SELECT severity, COUNT(*) AS n FROM findings WHERE status = 'ACTIVE' GROUP BY severity"
        ).fetchall()
    }
    rule_counts = {
        row["rule_code"]: row["n"]
        for row in connection.execute(
            "SELECT rule_code, COUNT(*) AS n FROM findings WHERE status = 'ACTIVE' GROUP BY rule_code"
        ).fetchall()
    }

    return FindingsStats(
        total_count=sum(status_counts.values()),
        active_count=status_counts.get("ACTIVE", 0),
        resolved_count=status_counts.get("RESOLVED", 0),
        ignored_count=status_counts.get("IGNORED", 0),
        severity_counts=severity_counts,
        rule_counts=rule_counts,
    )
