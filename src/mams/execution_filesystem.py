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

import ctypes
import errno
import hashlib
import os
import platform
import shutil
import stat
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
    later commit step (`finalize_verified_temp_file`) is always a
    same-directory operation. `token` is the execution's own lock/execution
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


def finalize_same_filesystem_source_move(source_path: str, final_path: str) -> None:
    """The same-filesystem strategy's whole move: `source_path` is the
    original source file, moved directly to `final_path` on the same
    device. Not used by the cross-filesystem strategy -- see
    `finalize_verified_temp_file()` for that, which never calls
    `os.link()` because some network filesystems (SMB/CIFS, some exFAT
    mounts) do not support hard links even when `os.stat` reports the
    same device for both paths (the exact failure this function's
    former shared version hit against a real NAS mount).

    Uses `os.link()`, not `os.rename()`/`Path.rename()`: a rename
    silently replaces an existing destination on POSIX, and a
    check-then-rename has a TOCTOU race between the check and the
    rename. `os.link()` raises `FileExistsError` natively and atomically
    if `final_path` already exists -- there is nothing to check
    beforehand. Confirms inode equality between the two paths before
    removing `source_path`, as a cheap correctness check that the link
    is real hard-link semantics rather than something the filesystem
    silently faked; on a mismatch, raises without removing anything,
    preserving both copies.

    If `os.link()` raises here for any reason, that propagates
    unmodified as a hard step failure -- never a silent fallback to a
    copy strategy mid-execution."""
    os.link(source_path, final_path)
    source_inode = os.stat(source_path).st_ino
    final_inode = os.stat(final_path).st_ino
    if source_inode != final_inode:
        raise OSError(
            f"Hard link at {final_path} does not share an inode with {source_path}; "
            "leaving both in place rather than removing the source"
        )
    os.unlink(source_path)


class DestinationCollisionError(OSError):
    """Raised by `finalize_verified_temp_file()` when the final
    destination path already exists at the moment of finalization --
    either the native `RENAME_EXCL` primitive's own `EEXIST`, or an
    explicit `lstat` check in the compatibility fallback path. Both the
    temp file and any pre-existing destination are left completely
    untouched; there is no code path from here to an overwrite."""


class FinalizationPreconditionError(ValueError):
    """Raised by `finalize_verified_temp_file()` when its inputs violate
    a hard precondition -- temp and final not in the same directory, a
    symlink at either path, or the temp path not being a regular file.
    Always a caller-construction error, never something that can happen
    from a live filesystem race (every precondition here is checked
    immediately before use)."""


class _RenameExclUnsupportedError(OSError):
    """Internal signal that `renamex_np(..., RENAME_EXCL)` is not usable
    on this platform or this destination filesystem -- caught inside
    `finalize_verified_temp_file()` to trigger the documented
    lock-protected fallback. Never escapes this module."""


_RENAME_EXCL = 0x00000004  # macOS <sys/fcntl.h>: renamex_np's no-clobber flag.


def _renamex_np_no_replace(old_path: str, new_path: str) -> None:
    """Tightly-scoped ctypes adapter for macOS's `renamex_np(2)` with
    `RENAME_EXCL` -- an OS-native atomic no-clobber rename, equivalent in
    intent to Linux's `renameat2(RENAME_NOREPLACE)`. Documented by Apple
    as supported on APFS/HFS+ but not guaranteed elsewhere; on an
    unsupported filesystem (including, per this milestone's real NAS
    acceptance failure, SMB) the kernel returns `ENOTSUP`/`EOPNOTSUPP`,
    which this function turns into `_RenameExclUnsupportedError` so the
    caller can fall back rather than this function silently doing
    something else. Never invoked on non-Darwin platforms."""
    if platform.system() != "Darwin":
        raise _RenameExclUnsupportedError("renamex_np is only available on macOS")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renamex_np = libc.renamex_np
    except (OSError, AttributeError) as exc:
        raise _RenameExclUnsupportedError("renamex_np symbol is not available on this system") from exc
    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int

    ctypes.set_errno(0)
    result = renamex_np(os.fsencode(old_path), os.fsencode(new_path), _RENAME_EXCL)
    if result == 0:
        return
    err = ctypes.get_errno()
    if err in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOSYS):
        raise _RenameExclUnsupportedError(os.strerror(err))
    if err == errno.EEXIST:
        raise DestinationCollisionError(err, f"Destination already exists (RENAME_EXCL): {new_path}")
    raise OSError(err, os.strerror(err), new_path)


def _finalize_via_locked_rename_fallback(temp_path: str, final_path: str) -> None:
    """The documented compatibility path for a filesystem that doesn't
    support a native no-replace rename (SMB, in the real acceptance
    failure this module exists to fix). Re-checks the destination is
    still absent, that the parent directory is real, and that the temp
    file is still a regular file, immediately before calling
    `os.rename()`.

    This path is NOT atomically race-free the way `RENAME_EXCL` is:
    `os.rename()` on POSIX silently overwrites an existing destination,
    so a file created at `final_path` by some *other* process in the
    narrow window between the `lstat` check below and the `os.rename()`
    call would be silently destroyed. This is a documented, accepted
    limitation of running against a filesystem that itself doesn't offer
    a stronger primitive -- not a bug -- and it is narrowed as far as
    this codebase can narrow it: `execute_plan()` already holds this
    plan's exclusive execution lock and has already confirmed, via fresh
    preflight, that nothing else in this codebase can be writing to this
    exact destination path concurrently. The residual risk is an
    unrelated third party writing to the same NAS path at the same
    moment, which no purely client-side rename call can fully close on a
    filesystem that does not itself support one."""
    if os.path.lexists(final_path):
        raise DestinationCollisionError(
            f"Destination already exists immediately before finalization "
            f"(no-replace rename unsupported on this filesystem): {final_path}"
        )
    parent = os.path.dirname(final_path)
    if not os.path.isdir(parent):
        raise FinalizationPreconditionError(f"Destination parent directory is missing or not a directory: {parent}")
    temp_stat = os.lstat(temp_path)
    if not stat.S_ISREG(temp_stat.st_mode):
        raise FinalizationPreconditionError(f"Temp path is not a regular file: {temp_path}")

    os.rename(temp_path, final_path)


def _fsync_directory_best_effort(directory: str) -> None:
    """`fsync` the destination directory after a rename, where the
    platform/filesystem supports it -- durability of the *directory
    entry*, not the file's own data (already flushed and `fsync`ed
    during the copy in `stream_copy_with_checksum`). Some filesystems,
    network mounts in particular, don't support `fsync` on a directory
    file descriptor at all; that failure is swallowed here because the
    rename itself already succeeded and directory-entry durability is
    best-effort, not a correctness requirement any caller depends on."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def finalize_verified_temp_file(temp_path: str, final_path: str) -> None:
    """The cross-filesystem strategy's temp-to-final commit: promotes an
    already fully-copied, flushed, `fsync`ed, and independently
    checksum-verified temporary file -- already inside the final
    destination directory, so this is always a same-directory operation
    -- to its final filename.

    Deliberately a *different* primitive from
    `finalize_same_filesystem_source_move()`: this function never calls
    `os.link()`. Some network filesystems (SMB/CIFS in particular -- the
    exact failure this function exists to fix, from this milestone's
    real NAS acceptance run) do not support hard links even inside a
    single directory on a device `os.stat()` reports as one filesystem.

    Tries, in order:
    1. `_renamex_np_no_replace()` -- macOS's native `renamex_np(...,
       RENAME_EXCL)`, an atomic, OS-enforced no-clobber rename.
    2. `_finalize_via_locked_rename_fallback()` if (1) is unsupported on
       this platform or destination filesystem -- see that function's
       docstring for the documented limitation of this path.

    Never calls `os.replace()` or `shutil.move()`. Never deletes or
    overwrites an existing destination. Never appends a "copy" suffix,
    a numeric suffix, or any alternate filename -- a real collision is
    always `DestinationCollisionError`, never silently worked around.
    Does not retry: a caught `_RenameExclUnsupportedError` triggers
    exactly one fallback attempt, never a loop."""
    temp = Path(temp_path)
    final = Path(final_path)

    if temp.parent != final.parent:
        raise FinalizationPreconditionError(
            f"temp and final paths must share a parent directory: {temp.parent} != {final.parent}"
        )
    if os.path.islink(temp_path):
        raise FinalizationPreconditionError(f"Refusing to finalize a symlink temp path: {temp_path}")
    if os.path.islink(final_path):
        raise FinalizationPreconditionError(f"Refusing to finalize onto a symlink destination path: {final_path}")

    try:
        temp_stat = os.lstat(temp_path)
    except FileNotFoundError as exc:
        raise FinalizationPreconditionError(f"Temp file does not exist: {temp_path}") from exc
    if not stat.S_ISREG(temp_stat.st_mode):
        raise FinalizationPreconditionError(f"Temp path is not a regular file: {temp_path}")

    if os.path.lexists(final_path):
        raise DestinationCollisionError(f"Destination already exists immediately before finalization: {final_path}")

    try:
        _renamex_np_no_replace(temp_path, final_path)
    except _RenameExclUnsupportedError:
        _finalize_via_locked_rename_fallback(temp_path, final_path)

    _fsync_directory_best_effort(str(final.parent))

    if os.path.lexists(temp_path):
        raise OSError(f"Temp file still present after finalization reported success: {temp_path}")
    if os.path.islink(final_path) or not os.path.isfile(final_path):
        raise OSError(f"Final path is not a regular file after finalization: {final_path}")


def remove_source_file(source_path: str) -> None:
    """Removes exactly the one source file. Never recursive, never
    touches the parent directory (never removes the Incoming root or
    any intermediate directory)."""
    os.unlink(source_path)
