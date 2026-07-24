"""Orchestrates one `mams identify evaluate` run: loads canonical ACTIVE
inventory records, parses each into an `IdentificationCandidate`
(identification.py -- pure, no SQL, no external service calls), and
reconciles the results into the identification_candidates table
atomically (identification_repository.py -- all identification SQL).

This module is the only place that wires parsing to persistence; neither
identification.py nor identification_repository.py depends on the other.
"""

from __future__ import annotations

import sqlite3

from . import identification, inventory_repository
from .identification_repository import ReconciliationResult, reconcile_candidates


def evaluate_candidates(connection: sqlite3.Connection) -> ReconciliationResult:
    """Parse and reconcile identification candidates for every currently
    ACTIVE media file, in one transaction.

    MISSING files are never visited: `list_media_files(state="ACTIVE")`
    excludes them, so any existing candidate for a file that has gone
    MISSING is left exactly as it was -- retained as the current
    historical interpretation rather than removed or specially marked.
    See docs/DATABASE.md ("identification_candidates lifecycle").

    Wrapping reconcile_candidates() in `with connection:` makes the whole
    reconciliation atomic -- an exception partway through rolls back every
    write, leaving identification_candidates exactly as it was before this
    call.
    """
    records = inventory_repository.list_media_files(connection, state="ACTIVE")
    candidates = [
        identification.evaluate_file(
            identification.IdentificationInput(
                media_file_id=record.id,
                category=record.category,
                layout=record.layout,
                filename=record.filename,
                relative_path=record.relative_path,
                parent_directory=record.parent_directory,
                extension=record.extension,
            )
        )
        for record in records
    ]
    with connection:
        return reconcile_candidates(connection, candidates)
