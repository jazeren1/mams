"""Lifecycle tests for findings reconciliation (findings_service.py /
findings_repository.py): creation, no-duplicate re-evaluation, resolution,
reactivation, IGNORED preservation, atomic rollback, and deterministic
evidence_json.

Builds media_files rows directly via SQL (same approach as
test_schema_findings.py) rather than through persist_scan(), since these
tests are about reconciliation behavior, not scan mechanics -- each test
manipulates exactly the condition its rule cares about.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from mams import findings_repository
from mams.db import connect, migrate
from mams.findings_service import evaluate_findings

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "migrations"


@pytest.fixture()
def connection(tmp_path: Path):
    db_path = tmp_path / "mams.db"
    migrate(db_path, REPO_MIGRATIONS_DIR)
    conn = connect(db_path)
    yield conn
    conn.close()


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _insert_library(connection: sqlite3.Connection, category: str = "movies") -> int:
    cursor = connection.execute(
        "INSERT INTO libraries (category, root_path) VALUES (?, ?)", (category, f"/Volumes/{category}")
    )
    return _lastrowid(cursor)


def _insert_scan_run(connection: sqlite3.Connection) -> int:
    cursor = connection.execute("INSERT INTO scan_runs DEFAULT VALUES")
    return _lastrowid(cursor)


def _insert_media_file(
    connection: sqlite3.Connection,
    *,
    library_id: int,
    scan_id: int,
    name: str = "Movie (2001).mkv",
    size_bytes: int = 5_000_000_000,
    state: str = "ACTIVE",
) -> int:
    # media_info_probed_at is always set here so that only the rule each
    # test is targeting fires -- an un-probed file would also (correctly)
    # trigger metadata_not_probed, which isn't what these tests are about.
    cursor = connection.execute(
        """
        INSERT INTO media_files (
            library_id, absolute_path, relative_path, filename, extension,
            parent_directory, layout, size_bytes, state,
            media_info_probed_at, first_seen_scan_id, last_seen_scan_id
        ) VALUES (?, ?, ?, ?, ?, ?, 'movie_flat', ?, ?, '2026-07-20T00:00:00', ?, ?)
        """,
        (
            library_id,
            f"/Volumes/movies/{name}",
            name,
            name,
            ".mkv",
            "/Volumes/movies",
            size_bytes,
            state,
            scan_id,
            scan_id,
        ),
    )
    media_file_id = _lastrowid(cursor)
    # One video + one audio track so no_video_track/no_audio_track don't
    # also fire -- these tests are about zero_byte_file's lifecycle, not
    # track-count rules.
    connection.execute(
        "INSERT INTO video_tracks (media_file_id, track_index, codec) VALUES (?, 0, 'h264')", (media_file_id,)
    )
    connection.execute(
        "INSERT INTO audio_tracks (media_file_id, track_index, codec) VALUES (?, 0, 'aac')", (media_file_id,)
    )
    return media_file_id


def _get_finding(connection: sqlite3.Connection, *, rule_code: str, media_file_id: int) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM findings WHERE rule_code = ? AND media_file_id = ?", (rule_code, media_file_id)
    ).fetchone()


def _fixture(connection: sqlite3.Connection) -> tuple[int, int]:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, size_bytes=0)
    return library_id, media_file_id


# --- creation / no duplicates / stability ---------------------------------------


def test_new_finding_is_created_active(connection: sqlite3.Connection) -> None:
    _, media_file_id = _fixture(connection)
    result = evaluate_findings(connection)
    assert result.created == 1
    assert result.resolved == 0

    row = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert row is not None
    assert row["status"] == "ACTIVE"
    assert row["resolved_at"] is None


def test_repeated_evaluation_creates_no_duplicate_and_stable_id(connection: sqlite3.Connection) -> None:
    _, media_file_id = _fixture(connection)
    evaluate_findings(connection)
    first = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert first is not None

    result = evaluate_findings(connection)
    assert result.created == 0
    assert result.unchanged == 1

    rows = connection.execute(
        "SELECT * FROM findings WHERE rule_code = ? AND media_file_id = ?", ("zero_byte_file", media_file_id)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == first["id"]


def test_first_detected_at_preserved_across_repeated_evaluation(connection: sqlite3.Connection) -> None:
    _, media_file_id = _fixture(connection)
    evaluate_findings(connection)
    first = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert first is not None

    evaluate_findings(connection)
    second = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert second is not None
    assert second["first_detected_at"] == first["first_detected_at"]


def test_last_detected_at_updates_on_reconfirmation(connection: sqlite3.Connection) -> None:
    _, media_file_id = _fixture(connection)
    evaluate_findings(connection)
    first = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert first is not None

    # Force a distinguishable timestamp so we can tell CURRENT_TIMESTAMP moved.
    connection.execute(
        "UPDATE findings SET last_detected_at = '2000-01-01 00:00:00' WHERE id = ?", (first["id"],)
    )
    evaluate_findings(connection)
    second = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert second is not None
    assert second["last_detected_at"] != "2000-01-01 00:00:00"


def test_updated_at_does_not_churn_when_content_is_unchanged(connection: sqlite3.Connection) -> None:
    _, media_file_id = _fixture(connection)
    evaluate_findings(connection)
    first = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert first is not None

    connection.execute("UPDATE findings SET updated_at = '2000-01-01 00:00:00' WHERE id = ?", (first["id"],))
    evaluate_findings(connection)
    second = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert second is not None
    assert second["updated_at"] == "2000-01-01 00:00:00"


def test_updated_at_changes_when_content_changes(connection: sqlite3.Connection) -> None:
    library_id = _insert_library(connection)
    scan_id = _insert_scan_run(connection)
    media_file_id = _insert_media_file(connection, library_id=library_id, scan_id=scan_id, size_bytes=0)
    evaluate_findings(connection)
    first = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert first is not None
    connection.execute("UPDATE findings SET updated_at = '2000-01-01 00:00:00' WHERE id = ?", (first["id"],))

    # Move the file to a different library -- content (library_id) changes,
    # but the file stays zero-byte, so the same finding key still matches.
    other_library_id = _insert_library(connection, category="kids_movies")
    connection.execute("UPDATE media_files SET library_id = ? WHERE id = ?", (other_library_id, media_file_id))

    evaluate_findings(connection)
    second = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert second is not None
    assert second["updated_at"] != "2000-01-01 00:00:00"
    assert second["library_id"] == other_library_id


# --- resolve / reactivate --------------------------------------------------------


def test_finding_resolved_when_condition_disappears(connection: sqlite3.Connection) -> None:
    _, media_file_id = _fixture(connection)
    evaluate_findings(connection)

    connection.execute("UPDATE media_files SET size_bytes = 5000000000 WHERE id = ?", (media_file_id,))
    result = evaluate_findings(connection)
    assert result.resolved == 1

    row = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert row is not None
    assert row["status"] == "RESOLVED"
    assert row["resolved_at"] is not None


def test_finding_reactivated_when_condition_returns_preserving_first_detected_at(
    connection: sqlite3.Connection,
) -> None:
    _, media_file_id = _fixture(connection)
    evaluate_findings(connection)
    original = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert original is not None

    connection.execute("UPDATE media_files SET size_bytes = 5000000000 WHERE id = ?", (media_file_id,))
    evaluate_findings(connection)
    resolved = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert resolved is not None and resolved["status"] == "RESOLVED"

    connection.execute("UPDATE media_files SET size_bytes = 0 WHERE id = ?", (media_file_id,))
    result = evaluate_findings(connection)
    assert result.reactivated == 1

    reactivated = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert reactivated is not None
    assert reactivated["id"] == original["id"]
    assert reactivated["status"] == "ACTIVE"
    assert reactivated["resolved_at"] is None
    assert reactivated["first_detected_at"] == original["first_detected_at"]


def test_resolved_finding_not_in_candidates_is_left_alone(connection: sqlite3.Connection) -> None:
    _, media_file_id = _fixture(connection)
    evaluate_findings(connection)
    connection.execute("UPDATE media_files SET size_bytes = 5000000000 WHERE id = ?", (media_file_id,))
    evaluate_findings(connection)
    resolved = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert resolved is not None and resolved["status"] == "RESOLVED"

    result = evaluate_findings(connection)
    assert result.resolved == 0
    assert result.created == 0
    still = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert still is not None
    assert still["status"] == "RESOLVED"
    assert still["resolved_at"] == resolved["resolved_at"]


# --- IGNORED preservation --------------------------------------------------------


def test_ignored_finding_is_not_reactivated_or_touched_in_status_while_condition_persists(
    connection: sqlite3.Connection,
) -> None:
    _, media_file_id = _fixture(connection)
    evaluate_findings(connection)
    finding = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert finding is not None
    connection.execute(
        "UPDATE findings SET status = 'IGNORED', last_detected_at = '2000-01-01 00:00:00' WHERE id = ?",
        (finding["id"],),
    )

    result = evaluate_findings(connection)
    assert result.ignored_touched == 1
    assert result.created == 0
    assert result.updated == 0

    row = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert row is not None
    assert row["status"] == "IGNORED"
    assert row["last_detected_at"] != "2000-01-01 00:00:00"


def test_ignored_finding_is_not_resolved_when_condition_disappears(connection: sqlite3.Connection) -> None:
    _, media_file_id = _fixture(connection)
    evaluate_findings(connection)
    finding = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert finding is not None
    connection.execute("UPDATE findings SET status = 'IGNORED' WHERE id = ?", (finding["id"],))

    connection.execute("UPDATE media_files SET size_bytes = 5000000000 WHERE id = ?", (media_file_id,))
    result = evaluate_findings(connection)
    assert result.resolved == 0

    row = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert row is not None
    assert row["status"] == "IGNORED"
    assert row["resolved_at"] is None


# --- atomicity --------------------------------------------------------------------


def test_evaluation_failure_leaves_no_partial_findings(connection: sqlite3.Connection) -> None:
    _, media_file_id = _fixture(connection)
    evaluate_findings(connection)

    # Now force a failure during a run that would both resolve this finding
    # (file no longer zero-byte) and create a new one (a second file goes
    # zero-byte) -- if the failure isn't atomic, one half would land and
    # the other wouldn't.
    connection.execute("UPDATE media_files SET size_bytes = 5000000000 WHERE id = ?", (media_file_id,))
    library_id = connection.execute("SELECT library_id FROM media_files WHERE id = ?", (media_file_id,)).fetchone()[0]
    scan_id = _insert_scan_run(connection)
    second_media_file_id = _insert_media_file(
        connection, library_id=library_id, scan_id=scan_id, name="Second.mkv", size_bytes=0
    )

    with mock.patch.object(findings_repository, "_resolve", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            evaluate_findings(connection)

    # The pre-existing finding must still be exactly as it was before the
    # failed run -- not resolved.
    original = _get_finding(connection, rule_code="zero_byte_file", media_file_id=media_file_id)
    assert original is not None
    assert original["status"] == "ACTIVE"

    # The new file's finding must not have been created either.
    new_finding = _get_finding(connection, rule_code="zero_byte_file", media_file_id=second_media_file_id)
    assert new_finding is None


# --- deterministic evidence_json --------------------------------------------------


def test_evidence_json_is_deterministic_across_independent_runs(tmp_path: Path) -> None:
    counter = iter(range(2))

    def run() -> str:
        db_path = tmp_path / f"mams_{next(counter)}.db"
        migrate(db_path, REPO_MIGRATIONS_DIR)
        conn = connect(db_path)
        try:
            library_id = _insert_library(conn)
            scan_id = _insert_scan_run(conn)
            media_file_id = _insert_media_file(conn, library_id=library_id, scan_id=scan_id, size_bytes=0)
            evaluate_findings(conn)
            row = _get_finding(conn, rule_code="zero_byte_file", media_file_id=media_file_id)
            assert row is not None
            return row["evidence_json"]
        finally:
            conn.close()

    first = run()
    second = run()
    assert first == second
    assert json.loads(first) == {"size_bytes": 0}
