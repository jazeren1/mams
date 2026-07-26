"""Filesystem lock management for Milestone 8's approved-plan executor.

No SQL. This is the process/filesystem-level lock that complements the
database-level `APPROVED -> EXECUTING` transition
(`execution_repository.transition_plan_to_executing`) -- both must
succeed before any filesystem mutation begins (Phase C).

The lock file lives in a configured local state directory
(`AppConfig.execution_state_directory`), never inside the NAS media
destination and never on the NAS share itself: `O_EXCL`'s
create-exclusive guarantee is unreliable on some network filesystems,
so this directory must be local scratch disk, the same convention
already used for `project.database_path`.

A stale lock (its process no longer running, or its execution long
since finished) is never automatically removed here -- `read_lock()` is
strictly read-only, for `mams ingest recovery` to report on. Deciding
whether a lock is actually safe to clear is a human judgment call that
belongs to a future, deliberate recovery command, not to this module.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class LockInfo:
    token: str
    pid: int
    hostname: str
    plan_id: int
    acquired_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "token": self.token,
            "pid": self.pid,
            "hostname": self.hostname,
            "plan_id": self.plan_id,
            "acquired_at": self.acquired_at,
        }


class LockAlreadyHeldError(Exception):
    """Raised by `acquire_lock` when a lock file for this plan already
    exists. `existing` is a best-effort parse of its contents for
    diagnostics -- `None` if the file exists but couldn't be parsed
    (still a real, held lock; just one whose metadata is unreadable)."""

    def __init__(self, plan_id: int, path: Path, existing: LockInfo | None) -> None:
        self.plan_id = plan_id
        self.path = path
        self.existing = existing
        super().__init__(f"An execution lock already exists for plan {plan_id} at {path}")


class LockTokenMismatchError(Exception):
    """Raised by `release_lock` when the lock file on disk does not
    carry the token the caller expects -- releasing it anyway would risk
    dropping a lock some other process still legitimately holds."""

    def __init__(self, plan_id: int, path: Path) -> None:
        self.plan_id = plan_id
        self.path = path
        super().__init__(f"Lock file for plan {plan_id} at {path} does not match the expected token; not released")


def lock_path(state_directory: str | Path, plan_id: int) -> Path:
    return Path(state_directory) / f"ingest-plan-{plan_id}.lock"


def _parse_lock_file(path: Path) -> LockInfo | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return LockInfo(
            token=data["token"],
            pid=data["pid"],
            hostname=data["hostname"],
            plan_id=data["plan_id"],
            acquired_at=data["acquired_at"],
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None


def acquire_lock(state_directory: str | Path, plan_id: int, *, token: str) -> LockInfo:
    """Atomically create the lock file for one plan (`O_CREAT | O_EXCL`
    -- fails loudly and natively if the file already exists; no separate
    exists-check, so no TOCTOU race). Writes `{token, pid, hostname,
    plan_id, acquired_at}` and `fsync`s before returning."""
    path = lock_path(state_directory, plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    info = LockInfo(
        token=token,
        pid=os.getpid(),
        hostname=socket.gethostname(),
        plan_id=plan_id,
        acquired_at=datetime.now(UTC).isoformat(),
    )
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        raise LockAlreadyHeldError(plan_id, path, _parse_lock_file(path)) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(info.to_dict()))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # A lock file that was created but never finished writing its
        # contents must not be left behind to wrongly block every future
        # attempt -- roll it back before propagating the error.
        path.unlink(missing_ok=True)
        raise
    return info


def release_lock(state_directory: str | Path, plan_id: int, *, token: str) -> None:
    """Remove the lock file only if its stored token matches this
    caller's -- never releases a lock this call didn't itself acquire.
    Idempotent: does nothing if the file is already gone."""
    path = lock_path(state_directory, plan_id)
    existing = _parse_lock_file(path)
    if existing is None:
        return
    if existing.token != token:
        raise LockTokenMismatchError(plan_id, path)
    path.unlink(missing_ok=True)


def read_lock(state_directory: str | Path, plan_id: int) -> LockInfo | None:
    """Read-only, for `mams ingest recovery` -- never removes, never
    auto-expires a stale lock."""
    return _parse_lock_file(lock_path(state_directory, plan_id))
