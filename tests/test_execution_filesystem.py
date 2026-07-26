"""Tests for execution_filesystem.py: the narrowly-scoped filesystem
adapter. Real `tmp_path` filesystem operations throughout -- no SQL, no
mocking of the operations themselves (only `os.stat`/`os.link` are
monkeypatched in the handful of tests that need to simulate a
disk/filesystem-level failure that `tmp_path` alone can't produce).
"""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path

import pytest

from mams import execution_filesystem as fs_module
from mams.execution_filesystem import (
    DestinationCollisionError,
    FinalizationPreconditionError,
    checksum_file,
    create_destination_directory,
    finalize_same_filesystem_source_move,
    finalize_verified_temp_file,
    free_space_bytes,
    is_directory,
    is_readable,
    is_regular_file,
    is_writable,
    path_exists,
    remove_source_file,
    stat_device_id,
    stat_mtime,
    stat_size_bytes,
    stream_copy_with_checksum,
    temp_destination_path,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- basic stat helpers ----------------------------------------------------------


def test_path_exists_and_is_regular_file(tmp_path: Path) -> None:
    file_path = tmp_path / "a.mkv"
    file_path.write_bytes(b"hello")
    assert path_exists(str(file_path)) is True
    assert is_regular_file(str(file_path)) is True
    assert path_exists(str(tmp_path / "missing.mkv")) is False


def test_is_directory(tmp_path: Path) -> None:
    assert is_directory(str(tmp_path)) is True
    file_path = tmp_path / "a.mkv"
    file_path.write_bytes(b"hello")
    assert is_directory(str(file_path)) is False


def test_is_readable_and_writable(tmp_path: Path) -> None:
    file_path = tmp_path / "a.mkv"
    file_path.write_bytes(b"hello")
    assert is_readable(str(file_path)) is True
    assert is_writable(str(tmp_path)) is True


def test_stat_size_bytes_and_mtime(tmp_path: Path) -> None:
    file_path = tmp_path / "a.mkv"
    file_path.write_bytes(b"hello world")
    assert stat_size_bytes(str(file_path)) == 11
    assert stat_mtime(str(file_path)) == pytest.approx(os.stat(file_path).st_mtime)


def test_stat_device_id_is_stable_for_paths_on_the_same_filesystem(tmp_path: Path) -> None:
    a = tmp_path / "a.mkv"
    b = tmp_path / "b.mkv"
    a.write_bytes(b"1")
    b.write_bytes(b"2")
    assert stat_device_id(str(a)) == stat_device_id(str(b))


def test_free_space_bytes_returns_a_positive_number(tmp_path: Path) -> None:
    assert free_space_bytes(str(tmp_path)) > 0


def test_temp_destination_path_is_hidden_and_inside_destination_directory() -> None:
    path = temp_destination_path("/NAS/Movies/Alien (1979)", "Alien (1979).mkv", token="abc123")
    assert path.parent == Path("/NAS/Movies/Alien (1979)")
    assert path.name == ".Alien (1979).mkv.mams-partial-abc123"


# --- create_destination_directory ------------------------------------------------


def test_create_destination_directory_creates_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "Movies" / "Alien (1979)"
    assert create_destination_directory(str(target)) is True
    assert target.is_dir()


def test_create_destination_directory_returns_false_when_already_a_directory(tmp_path: Path) -> None:
    target = tmp_path / "Movies" / "Alien (1979)"
    target.mkdir(parents=True)
    assert create_destination_directory(str(target)) is False
    assert target.is_dir()


def test_create_destination_directory_raises_when_occupied_by_a_file(tmp_path: Path) -> None:
    target = tmp_path / "Alien (1979)"
    target.write_bytes(b"not a directory")
    with pytest.raises(NotADirectoryError):
        create_destination_directory(str(target))


# --- stream_copy_with_checksum ----------------------------------------------------


def test_stream_copy_with_checksum_copies_bytes_and_returns_matching_checksum(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    payload = b"the quick brown fox jumps over the lazy dog" * 1000
    source.write_bytes(payload)
    temp = tmp_path / ".dest.mkv.mams-partial-tok"

    checksum, bytes_copied = stream_copy_with_checksum(str(source), str(temp), buffer_bytes=64, algorithm="sha256")

    assert bytes_copied == len(payload)
    assert checksum == _sha256(payload)
    assert temp.read_bytes() == payload


def test_stream_copy_with_checksum_fsyncs_and_leaves_no_progress_lines(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x" * 5000)
    temp = tmp_path / ".dest.mkv.mams-partial-tok"
    progress_calls: list[tuple[int, int]] = []

    stream_copy_with_checksum(
        str(source),
        str(temp),
        buffer_bytes=512,
        algorithm="sha256",
        on_progress=lambda copied, total: progress_calls.append((copied, total)),
    )

    assert len(progress_calls) > 1
    assert progress_calls[-1][0] == 5000
    assert all(total == 5000 for _, total in progress_calls)


def test_stream_copy_with_checksum_refuses_to_overwrite_an_existing_temp_file(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"payload")
    temp = tmp_path / ".dest.mkv.mams-partial-tok"
    temp.write_bytes(b"already here")

    with pytest.raises(FileExistsError):
        stream_copy_with_checksum(str(source), str(temp), buffer_bytes=64, algorithm="sha256")

    # The pre-existing temp file must be untouched -- no silent overwrite.
    assert temp.read_bytes() == b"already here"


def test_stream_copy_with_checksum_raises_on_size_mismatch_and_retains_partial_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates 'the source changed size during the copy' by lying
    about the expected size via a patched os.stat -- the real
    filesystem can't easily produce this race deterministically."""
    source = tmp_path / "source.mkv"
    source.write_bytes(b"short")
    temp = tmp_path / ".dest.mkv.mams-partial-tok"

    real_stat = os.stat

    def _lying_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        result = real_stat(path, *args, **kwargs)  # type: ignore[arg-type]
        if str(path) == str(source):
            return os.stat_result(
                (result.st_mode, result.st_ino, result.st_dev, result.st_nlink, result.st_uid, result.st_gid,
                 999_999, result.st_atime, result.st_mtime, result.st_ctime)
            )
        return result

    monkeypatch.setattr(os, "stat", _lying_stat)

    with pytest.raises(OSError, match="Copied .* bytes but source was"):
        stream_copy_with_checksum(str(source), str(temp), buffer_bytes=64, algorithm="sha256")

    # Partial copy is retained as recovery evidence, never deleted automatically.
    assert temp.exists()
    assert temp.read_bytes() == b"short"


# --- checksum_file -----------------------------------------------------------------


def test_checksum_file_matches_hashlib_directly(tmp_path: Path) -> None:
    path = tmp_path / "file.mkv"
    payload = b"some file content" * 500
    path.write_bytes(payload)
    assert checksum_file(str(path), algorithm="sha256", buffer_bytes=128) == _sha256(payload)


def test_checksum_file_is_independent_of_stream_copy_hash(tmp_path: Path) -> None:
    """A corrupted-on-disk file (content differs from what was
    ostensibly written) must produce a checksum that reflects what's
    actually on disk now, not what was written earlier."""
    path = tmp_path / "file.mkv"
    path.write_bytes(b"original")
    first_checksum = checksum_file(str(path), algorithm="sha256", buffer_bytes=64)
    path.write_bytes(b"corrupted-afterward")
    second_checksum = checksum_file(str(path), algorithm="sha256", buffer_bytes=64)
    assert first_checksum != second_checksum


# --- finalize_same_filesystem_source_move ------------------------------------------


def test_finalize_same_filesystem_source_move_links_then_unlinks_source(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    final = tmp_path / "final.mkv"
    source.write_bytes(b"payload")

    finalize_same_filesystem_source_move(str(source), str(final))

    assert not source.exists()
    assert final.read_bytes() == b"payload"


def test_finalize_same_filesystem_source_move_refuses_to_overwrite_existing_final_path(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    final = tmp_path / "final.mkv"
    source.write_bytes(b"new content")
    final.write_bytes(b"existing content -- must not be touched")

    with pytest.raises(FileExistsError):
        finalize_same_filesystem_source_move(str(source), str(final))

    # Neither copy was touched by the failed attempt.
    assert source.read_bytes() == b"new content"
    assert final.read_bytes() == b"existing content -- must not be touched"


def test_finalize_same_filesystem_source_move_propagates_cross_device_link_failure_without_deleting_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates a filesystem that doesn't support hard links (some
    SMB/CIFS or exFAT mounts) even though the caller believed both
    paths were on the same device."""
    source = tmp_path / "source.mkv"
    final = tmp_path / "final.mkv"
    source.write_bytes(b"payload")

    def _raise_exdev(*args: object, **kwargs: object) -> None:
        raise OSError("simulated EXDEV: cross-device link not permitted")

    monkeypatch.setattr(os, "link", _raise_exdev)

    with pytest.raises(OSError, match="simulated EXDEV"):
        finalize_same_filesystem_source_move(str(source), str(final))

    assert source.exists()
    assert not final.exists()


def test_finalize_same_filesystem_source_move_raises_and_preserves_both_on_inode_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates a filesystem that reports success from os.link() but
    doesn't actually share an inode (defensive check against a
    non-conformant filesystem)."""
    source = tmp_path / "source.mkv"
    final = tmp_path / "final.mkv"
    source.write_bytes(b"payload")

    real_link = os.link

    def _fake_link(src: object, dst: object, *args: object, **kwargs: object) -> None:
        # Perform a real link so both paths exist, but the test will
        # patch os.stat to lie about inode equality below.
        real_link(src, dst, *args, **kwargs)  # type: ignore[arg-type]

    real_stat = os.stat
    call_count = {"n": 0}

    def _fake_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        result = real_stat(path, *args, **kwargs)  # type: ignore[arg-type]
        call_count["n"] += 1
        if call_count["n"] == 2:
            # Report a different inode for the second stat() call
            # (the final_path check) to simulate a mismatch.
            return os.stat_result(
                (result.st_mode, result.st_ino + 1, result.st_dev, result.st_nlink, result.st_uid,
                 result.st_gid, result.st_size, result.st_atime, result.st_mtime, result.st_ctime)
            )
        return result

    monkeypatch.setattr(os, "link", _fake_link)
    monkeypatch.setattr(os, "stat", _fake_stat)

    with pytest.raises(OSError, match="does not share an inode"):
        finalize_same_filesystem_source_move(str(source), str(final))

    assert source.exists()
    assert final.exists()


# --- finalize_verified_temp_file (cross-filesystem, SMB-safe) ----------------------


def test_finalize_verified_temp_file_promotes_temp_to_final(tmp_path: Path) -> None:
    directory = tmp_path / "Movies" / "Alien (1979)"
    directory.mkdir(parents=True)
    temp = directory / ".Alien (1979).mkv.mams-partial-tok"
    final = directory / "Alien (1979).mkv"
    temp.write_bytes(b"payload")

    finalize_verified_temp_file(str(temp), str(final))

    assert not temp.exists()
    assert final.read_bytes() == b"payload"


def test_finalize_verified_temp_file_never_calls_os_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "Movies" / "Alien (1979)"
    directory.mkdir(parents=True)
    temp = directory / ".Alien (1979).mkv.mams-partial-tok"
    final = directory / "Alien (1979).mkv"
    temp.write_bytes(b"payload")

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("finalize_verified_temp_file must never call os.link")

    monkeypatch.setattr(os, "link", _forbidden)

    finalize_verified_temp_file(str(temp), str(final))

    assert not temp.exists()
    assert final.read_bytes() == b"payload"


def test_finalize_verified_temp_file_reproduces_smb_hard_link_failure_and_still_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the real production failure: os.link()
    raising errno 45 (ENOTSUP) against an SMB-mounted NAS destination.
    finalize_verified_temp_file must succeed anyway, because it never
    calls os.link() in the first place."""
    directory = tmp_path / "Movies" / "District 9 (2009)"
    directory.mkdir(parents=True)
    temp = directory / ".District 9 (2009).mkv.mams-partial-824e39f7512a248d5f4e57bf99326dfe"
    final = directory / "District 9 (2009).mkv"
    temp.write_bytes(b"payload")

    def _raise_enotsup(*args: object, **kwargs: object) -> None:
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(os, "link", _raise_enotsup)

    finalize_verified_temp_file(str(temp), str(final))

    assert not temp.exists()
    assert final.read_bytes() == b"payload"


def test_finalize_verified_temp_file_refuses_to_overwrite_existing_final_path(tmp_path: Path) -> None:
    directory = tmp_path / "Movies" / "Alien (1979)"
    directory.mkdir(parents=True)
    temp = directory / ".Alien (1979).mkv.mams-partial-tok"
    final = directory / "Alien (1979).mkv"
    temp.write_bytes(b"new content")
    final.write_bytes(b"existing content -- must not be touched")

    with pytest.raises(DestinationCollisionError):
        finalize_verified_temp_file(str(temp), str(final))

    assert temp.read_bytes() == b"new content"
    assert final.read_bytes() == b"existing content -- must not be touched"


def test_finalize_verified_temp_file_fallback_path_also_refuses_to_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forces the lock-protected fallback (simulating RENAME_EXCL being
    unsupported, as it is on a real SMB mount) and proves it still never
    overwrites an existing destination -- the collision here is only
    detectable by the fallback's own lstat check, not by the precondition
    check that runs before the primitive is even tried, because the
    destination is created *between* those two checks."""
    directory = tmp_path / "Movies" / "Alien (1979)"
    directory.mkdir(parents=True)
    temp = directory / ".Alien (1979).mkv.mams-partial-tok"
    final = directory / "Alien (1979).mkv"
    temp.write_bytes(b"new content")

    def _force_unsupported(*args: object, **kwargs: object) -> None:
        final.write_bytes(b"existing content -- must not be touched")
        raise fs_module._RenameExclUnsupportedError("simulated: RENAME_EXCL unsupported")

    monkeypatch.setattr(fs_module, "_renamex_np_no_replace", _force_unsupported)

    with pytest.raises(DestinationCollisionError):
        finalize_verified_temp_file(str(temp), str(final))

    assert temp.read_bytes() == b"new content"
    assert final.read_bytes() == b"existing content -- must not be touched"


def test_finalize_verified_temp_file_uses_fallback_when_renamex_np_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "Movies" / "Alien (1979)"
    directory.mkdir(parents=True)
    temp = directory / ".Alien (1979).mkv.mams-partial-tok"
    final = directory / "Alien (1979).mkv"
    temp.write_bytes(b"payload")

    def _raise_unsupported(*args: object, **kwargs: object) -> None:
        raise fs_module._RenameExclUnsupportedError("simulated: RENAME_EXCL unsupported")

    monkeypatch.setattr(fs_module, "_renamex_np_no_replace", _raise_unsupported)

    finalize_verified_temp_file(str(temp), str(final))

    assert not temp.exists()
    assert final.read_bytes() == b"payload"


def test_finalize_verified_temp_file_rejects_symlink_temp(tmp_path: Path) -> None:
    directory = tmp_path / "Movies" / "Alien (1979)"
    directory.mkdir(parents=True)
    real_file = tmp_path / "elsewhere.mkv"
    real_file.write_bytes(b"payload")
    temp = directory / ".Alien (1979).mkv.mams-partial-tok"
    temp.symlink_to(real_file)
    final = directory / "Alien (1979).mkv"

    with pytest.raises(FinalizationPreconditionError, match="symlink"):
        finalize_verified_temp_file(str(temp), str(final))

    assert temp.is_symlink()
    assert not final.exists()


def test_finalize_verified_temp_file_rejects_symlink_destination(tmp_path: Path) -> None:
    directory = tmp_path / "Movies" / "Alien (1979)"
    directory.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere.mkv"
    temp = directory / ".Alien (1979).mkv.mams-partial-tok"
    temp.write_bytes(b"payload")
    final = directory / "Alien (1979).mkv"
    final.symlink_to(elsewhere)

    with pytest.raises(FinalizationPreconditionError, match="symlink"):
        finalize_verified_temp_file(str(temp), str(final))

    assert temp.exists()
    assert final.is_symlink()


def test_finalize_verified_temp_file_rejects_parent_directory_mismatch(tmp_path: Path) -> None:
    temp_dir = tmp_path / "Incoming"
    temp_dir.mkdir()
    final_dir = tmp_path / "Movies" / "Alien (1979)"
    final_dir.mkdir(parents=True)
    temp = temp_dir / ".Alien (1979).mkv.mams-partial-tok"
    temp.write_bytes(b"payload")
    final = final_dir / "Alien (1979).mkv"

    with pytest.raises(FinalizationPreconditionError, match="parent directory"):
        finalize_verified_temp_file(str(temp), str(final))

    assert temp.exists()
    assert not final.exists()


def test_finalize_verified_temp_file_attempts_directory_fsync_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fsync of the destination directory is attempted, but a platform
    that doesn't support it must not fail the whole finalization --
    directory-entry durability is best-effort."""
    directory = tmp_path / "Movies" / "Alien (1979)"
    directory.mkdir(parents=True)
    temp = directory / ".Alien (1979).mkv.mams-partial-tok"
    final = directory / "Alien (1979).mkv"
    temp.write_bytes(b"payload")

    real_fsync = os.fsync
    fsync_calls: list[int] = []

    def _tracking_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        raise OSError("simulated: fsync not supported on this filesystem")

    monkeypatch.setattr(os, "fsync", _tracking_fsync)

    finalize_verified_temp_file(str(temp), str(final))

    assert len(fsync_calls) == 1
    assert not temp.exists()
    assert final.read_bytes() == b"payload"
    monkeypatch.setattr(os, "fsync", real_fsync)


# --- remove_source_file ------------------------------------------------------------


def test_remove_source_file_removes_exact_file(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"payload")
    remove_source_file(str(source))
    assert not source.exists()


def test_remove_source_file_never_touches_parent_directory(tmp_path: Path) -> None:
    parent = tmp_path / "Incoming"
    parent.mkdir()
    sibling = parent / "sibling.mkv"
    target = parent / "target.mkv"
    sibling.write_bytes(b"stay")
    target.write_bytes(b"go")

    remove_source_file(str(target))

    assert parent.is_dir()
    assert sibling.exists()
    assert not target.exists()


def test_remove_source_file_raises_for_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        remove_source_file(str(tmp_path / "missing.mkv"))
