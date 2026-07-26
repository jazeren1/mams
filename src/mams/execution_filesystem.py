"""Narrowly-scoped filesystem adapter for Milestone 8's approved-plan
executor: stat, mkdir, exclusive copy, fsync, atomic no-clobber commit,
exact-file unlink. No SQL, no database access -- `execution_service.py`
is the only caller.

Every mutating function here is a single, bounded, inspectable
operation. None of them is `shutil.copy`/`shutil.copy2`/`shutil.move`
used as a black box: this milestone explicitly requires the copy,
flush, fsync, checksum, and commit boundaries to be distinct and
individually verifiable, and `shutil.move`'s cross-filesystem fallback
in particular would hide exactly those boundaries. There is no
subprocess/shell invocation anywhere in this module.

A failure partway through a mutating function here is never silently
cleaned up by deleting whatever was written so far -- a partial temp
file, an empty directory, or a source file that failed to unlink is
left exactly as it is, because it is recovery evidence
(`mams ingest recovery`), not litter. The one exception is
`acquire`-style exclusive-create failures (the destination already
existed, the temp name already existed): nothing was written in that
case, so there is nothing to preserve.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path


def path_exists(path: str) -> bool:
    return Path(path).exists()


def is_regular_file(path: str) -> bool:
    return Path(path).is_file()


def is_directory(path: str) -> bool:
    return Path(path).is_dir()


def is_readable(path: str) -> bool:
    return os.access(path, os.R_OK)


def is_writable(path: str) -> bool:
    return os.access(path, os.W_OK)


def stat_size_bytes(path: str) -> int:
    return os.stat(path).st_size


def stat_mtime(path: str) -> float:
    return os.stat(path).st_mtime


def stat_device_id(path: str) -> int:
    """The already-obtained fact `execution.decide_transfer_strategy`
    compares -- never a path-prefix heuristic."""
    return os.stat(path).st_dev


def free_space_bytes(path: str) -> int:
    return shutil.disk_usage(path).free


def temp_destination_path(destination_directory: str, destination_filename: str, *, token: str) -> Path:
    """A hidden, collision-resistant temporary path *inside* the final
    destination directory (never a separate scratch directory), so the
    later commit step (`finalize_same_device_move`) is always a
    same-device operation. `token` is the execution's own lock/execution
    token, already unique per attempt."""
    return Path(destination_directory) / f".{destination_filename}.mams-partial-{token}"


def create_destination_directory(directory: str) -> bool:
    """Create the exact destination directory if it doesn't already
    exist. Returns whether this call actually created it (`False` if it
    already existed as a directory). Race-free: attempts the create
    first and only inspects the target on `FileExistsError`, rather than
    checking existence beforehand (a check-then-create would leave a
    TOCTOU window)."""
    path = Path(directory)
    try:
        path.mkdir(parents=True, exist_ok=False)
        return True
    except FileExistsError:
        if not path.is_dir():
            raise NotADirectoryError(
                f"Destination directory path is occupied by a non-directory: {directory}"
            ) from None
        return False


def stream_copy_with_checksum(
    source_path: str,
    temp_path: str,
    *,
    buffer_bytes: int,
    algorithm: str,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[str, int]:
    """Manual, bounded chunked read/write copy of `source_path` into a
    freshly-created `temp_path` (opened with `O_CREAT | O_EXCL`, so a
    colliding temp name fails loudly rather than silently overwriting
    -- collisions should be essentially impossible given `temp_path`'s
    unique execution token). Hashes the source bytes incrementally as
    they stream through, so a large source file is read exactly once.
    Flushes and `fsync`s the temp file's descriptor before returning.

    Returns `(source_checksum_hex, bytes_copied)`. Raises `OSError` if
    the number of bytes actually copied differs from the source file's
    size observed when the copy began -- the source changed size during
    the copy. Either way, whatever was written to `temp_path` before an
    error is left in place: a partial copy is recovery evidence, never
    resumed and never deleted automatically here."""
    hasher = hashlib.new(algorithm)
    expected_size = os.stat(source_path).st_size
    bytes_copied = 0

    fd = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    with open(source_path, "rb") as source, os.fdopen(fd, "wb") as destination:
        while True:
            chunk = source.read(buffer_bytes)
            if not chunk:
                break
            hasher.update(chunk)
            destination.write(chunk)
            bytes_copied += len(chunk)
            if on_progress is not None:
                on_progress(bytes_copied, expected_size)
        destination.flush()
        os.fsync(destination.fileno())

    if bytes_copied != expected_size:
        raise OSError(
            f"Copied {bytes_copied} bytes but source was {expected_size} bytes when the copy began "
            f"(partial copy retained at {temp_path} for recovery inspection): {source_path}"
        )
    return hasher.hexdigest(), bytes_copied


def checksum_file(path: str, *, algorithm: str, buffer_bytes: int) -> str:
    """An independent re-read of an already-written file from disk --
    this is the actual proof of what is physically persisted and
    re-readable, distinct from (and computed independently of) the
    write-time hash `stream_copy_with_checksum` produces from bytes as
    read from the source. Catches silent write/buffering corruption the
    write-time hash alone cannot."""
    hasher = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(buffer_bytes)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def finalize_same_device_move(source_or_temp_path: str, final_path: str) -> None:
    """The shared no-clobber, same-device commit primitive, used for
    both: the same-filesystem strategy's whole move
    (`source_or_temp_path` is the original source file) and the
    cross-filesystem strategy's temp-to-final commit
    (`source_or_temp_path` is the verified temp file, already inside the
    final directory so this step is always same-device by
    construction).

    Uses `os.link()`, not `os.rename()`/`Path.rename()`: a rename
    silently replaces an existing destination on POSIX, and a
    check-then-rename has a TOCTOU race between the check and the
    rename. `os.link()` raises `FileExistsError` natively and atomically
    if `final_path` already exists -- there is nothing to check
    beforehand. Confirms inode equality between the two paths before
    removing `source_or_temp_path`, as a cheap correctness check that
    the link is real hard-link semantics rather than something the
    filesystem silently faked; on a mismatch, raises without removing
    anything, preserving both copies.

    Some network filesystems (SMB/CIFS, some exFAT mounts) do not
    support hard links even when `os.stat` reports the same device for
    both paths. If `os.link()` raises here for any reason, that
    propagates unmodified as a hard step failure -- never a silent
    fallback to a copy strategy mid-execution."""
    os.link(source_or_temp_path, final_path)
    source_inode = os.stat(source_or_temp_path).st_ino
    final_inode = os.stat(final_path).st_ino
    if source_inode != final_inode:
        raise OSError(
            f"Hard link at {final_path} does not share an inode with {source_or_temp_path}; "
            "leaving both in place rather than removing the source"
        )
    os.unlink(source_or_temp_path)


def remove_source_file(source_path: str) -> None:
    """Removes exactly the one source file. Never recursive, never
    touches the parent directory (never removes the Incoming root or
    any intermediate directory)."""
    os.unlink(source_path)
