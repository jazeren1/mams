"""Tests for execution_lock.py: the filesystem lock file that
complements the database-level APPROVED -> EXECUTING transition. Real
`tmp_path` filesystem operations, no SQL.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mams.execution_lock import (
    LockAlreadyHeldError,
    LockTokenMismatchError,
    acquire_lock,
    lock_path,
    read_lock,
    release_lock,
)


def test_acquire_lock_creates_lock_file_with_expected_contents(tmp_path: Path) -> None:
    info = acquire_lock(tmp_path, plan_id=42, token="tok-a")
    path = lock_path(tmp_path, 42)
    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["token"] == "tok-a"
    assert on_disk["pid"] == os.getpid()
    assert on_disk["plan_id"] == 42
    assert info.token == "tok-a"
    assert info.plan_id == 42


def test_acquire_lock_creates_state_directory_if_missing(tmp_path: Path) -> None:
    state_dir = tmp_path / "nested" / "locks"
    assert not state_dir.exists()
    acquire_lock(state_dir, plan_id=1, token="tok")
    assert state_dir.is_dir()
    assert lock_path(state_dir, 1).exists()


def test_second_acquire_for_same_plan_raises_lock_already_held(tmp_path: Path) -> None:
    acquire_lock(tmp_path, plan_id=42, token="tok-a")
    with pytest.raises(LockAlreadyHeldError) as excinfo:
        acquire_lock(tmp_path, plan_id=42, token="tok-b")
    assert excinfo.value.plan_id == 42
    assert excinfo.value.existing is not None
    assert excinfo.value.existing.token == "tok-a"


def test_acquire_lock_for_different_plans_does_not_conflict(tmp_path: Path) -> None:
    first = acquire_lock(tmp_path, plan_id=1, token="tok-1")
    second = acquire_lock(tmp_path, plan_id=2, token="tok-2")
    assert first.plan_id == 1
    assert second.plan_id == 2
    assert lock_path(tmp_path, 1).exists()
    assert lock_path(tmp_path, 2).exists()


def test_acquire_lock_over_malformed_existing_file_raises_with_existing_none(tmp_path: Path) -> None:
    path = lock_path(tmp_path, 42)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json", encoding="utf-8")
    with pytest.raises(LockAlreadyHeldError) as excinfo:
        acquire_lock(tmp_path, plan_id=42, token="tok-b")
    assert excinfo.value.existing is None


def test_release_lock_removes_file_when_token_matches(tmp_path: Path) -> None:
    acquire_lock(tmp_path, plan_id=42, token="tok-a")
    release_lock(tmp_path, plan_id=42, token="tok-a")
    assert not lock_path(tmp_path, 42).exists()


def test_release_lock_with_wrong_token_raises_and_does_not_remove(tmp_path: Path) -> None:
    acquire_lock(tmp_path, plan_id=42, token="tok-a")
    with pytest.raises(LockTokenMismatchError):
        release_lock(tmp_path, plan_id=42, token="wrong-token")
    assert lock_path(tmp_path, 42).exists()


def test_release_lock_is_idempotent_when_already_gone(tmp_path: Path) -> None:
    # No lock was ever acquired; releasing must not raise.
    release_lock(tmp_path, plan_id=999, token="whatever")


def test_release_lock_allows_reacquiring_afterward(tmp_path: Path) -> None:
    acquire_lock(tmp_path, plan_id=42, token="tok-a")
    release_lock(tmp_path, plan_id=42, token="tok-a")
    second = acquire_lock(tmp_path, plan_id=42, token="tok-b")
    assert second.token == "tok-b"


def test_read_lock_returns_none_when_no_lock_exists(tmp_path: Path) -> None:
    assert read_lock(tmp_path, plan_id=42) is None


def test_read_lock_returns_info_without_removing_it(tmp_path: Path) -> None:
    acquire_lock(tmp_path, plan_id=42, token="tok-a")
    info = read_lock(tmp_path, plan_id=42)
    assert info is not None
    assert info.token == "tok-a"
    assert lock_path(tmp_path, 42).exists()


def test_read_lock_returns_none_for_malformed_lock_file(tmp_path: Path) -> None:
    path = lock_path(tmp_path, 42)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json", encoding="utf-8")
    assert read_lock(tmp_path, plan_id=42) is None
