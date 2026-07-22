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
