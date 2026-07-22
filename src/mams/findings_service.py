"""Orchestrates one `mams findings evaluate` run: loads canonical inventory
records, evaluates every findings rule against them (findings.py -- pure,
no SQL), and reconciles the resulting candidates into the findings table
atomically (findings_repository.py -- all findings SQL).

This module is the only place that wires rule evaluation to persistence;
neither findings.py nor findings_repository.py depends on the other.
"""

from __future__ import annotations

import sqlite3

from . import findings, inventory_repository
from .findings_repository import ReconciliationResult, reconcile_findings


def evaluate_findings(connection: sqlite3.Connection) -> ReconciliationResult:
    """Evaluate all findings rules against the current canonical inventory
    and reconcile the results into the findings table in one transaction.

    `list_media_files()` with no filters returns every media_files row
    regardless of state, which is what rule scoping requires: ACTIVE rows
    for every rule but missing_file, MISSING rows for missing_file alone
    (see findings.py's module docstring). Wrapping reconcile_findings() in
    `with connection:` makes the whole reconciliation atomic -- an
    exception partway through rolls back every write, leaving the
    findings table exactly as it was before this call.
    """
    records = inventory_repository.list_media_files(connection)
    candidates = findings.evaluate_all(records)
    with connection:
        return reconcile_findings(connection, candidates)
