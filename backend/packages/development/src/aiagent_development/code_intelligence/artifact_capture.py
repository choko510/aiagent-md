"""Capture only index.scip into an anonymous private file; not an Artifact Broker.

The caller must authenticate the execution, ensure all untrusted writers have
stopped, and recheck authority before publication. This module does not make
those claims and exposes no network or model-facing file-read operation.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from .deadline import require_deadline
from .models import ReadError
from .projection_stage import _open_trusted_parent

_MAX_ARTIFACT = 4 * 1024 * 1024
_CHUNK = 64 * 1024


@dataclass(frozen=True)
class CapturedScip:
    """An unlinked temporary descriptor and digest of its exact bytes, not a receipt."""

    fd: int
    sha256: str
    byte_length: int


def _private_temp(parent_fd: int) -> tuple[int, int]:
    name = "capture-" + secrets.token_hex(16)
    writer = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
    reader = -1
    try:
        reader = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        wstat, rstat = os.fstat(writer), os.fstat(reader)
        if (wstat.st_dev, wstat.st_ino) != (rstat.st_dev, rstat.st_ino) or wstat.st_nlink != 1:
            raise ReadError("artifact staging identity mismatch")
        os.unlink(name, dir_fd=parent_fd)
        return writer, reader
    except BaseException:
        if reader >= 0:
            os.close(reader)
        os.close(writer)
        with suppress(FileNotFoundError):
            os.unlink(name, dir_fd=parent_fd)
        raise


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_uid,
        before.st_nlink,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (after.st_dev, after.st_ino, after.st_size, after.st_uid, after.st_nlink, after.st_mtime_ns, after.st_ctime_ns)


def _copy(source_fd: int, temp_fd: int, size: int, deadline: float) -> str:
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        if time.monotonic() > deadline:
            raise ReadError("artifact capture deadline exceeded")
        data = os.read(source_fd, min(remaining, _CHUNK))
        if time.monotonic() > deadline:
            raise ReadError("artifact capture deadline exceeded")
        if not data:
            raise ReadError("artifact capture incomplete")
        digest.update(data)
        view = memoryview(data)
        while view:
            if time.monotonic() > deadline:
                raise ReadError("artifact capture deadline exceeded")
            count = os.write(temp_fd, view)
            if count <= 0:
                raise ReadError("artifact staging write failed")
            view = view[count:]
        remaining -= len(data)
    return digest.hexdigest()


@contextmanager
def capture_scip(
    output_fd: int,
    private_parent: Path,
    *,
    expected_uid: int,
    deadline: float,
    max_bytes: int = _MAX_ARTIFACT,
) -> Iterator[CapturedScip]:
    """Copy one regular, single-link SCIP file, without following paths or publishing it."""
    require_deadline(deadline)
    if os.name != "posix" or not private_parent.is_absolute():
        raise ReadError("secure artifact capture unavailable")
    if type(expected_uid) is not int or expected_uid < 0 or not 1 <= max_bytes <= _MAX_ARTIFACT:
        raise ValueError("invalid artifact capture limits")
    if time.monotonic() > deadline:
        raise ReadError("artifact capture deadline exceeded")
    if not stat.S_ISDIR(os.fstat(output_fd).st_mode):
        raise ReadError("invalid execution output directory")
    parent_fd = _open_trusted_parent(private_parent)
    source_fd = -1
    temp_fd = -1
    reader_fd = -1
    try:
        parent = os.fstat(parent_fd)
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise ReadError("artifact parent is not private")
        source_fd = os.open("index.scip", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=output_fd)
        before = os.fstat(source_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != expected_uid
            or not 1 <= before.st_size <= max_bytes
        ):
            raise ReadError("invalid execution artifact")
        temp_fd, reader_fd = _private_temp(parent_fd)
        digest = _copy(source_fd, temp_fd, before.st_size, deadline)
        after = os.fstat(source_fd)
        named = os.stat("index.scip", dir_fd=output_fd, follow_symlinks=False)
        if time.monotonic() > deadline:
            raise ReadError("artifact capture deadline exceeded")
        if not _same_file(before, after) or not _same_file(before, named):
            raise ReadError("artifact changed during capture")
        os.fchmod(temp_fd, 0o400)
        os.close(temp_fd)
        temp_fd = -1
        yield CapturedScip(reader_fd, digest, before.st_size)
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if temp_fd >= 0:
            os.close(temp_fd)
        if reader_fd >= 0:
            os.close(reader_fd)
        os.close(parent_fd)
