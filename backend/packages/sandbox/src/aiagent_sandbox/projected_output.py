"""Private per-Execution output staging; not an authorization or publication API."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from aiagent_domain import is_uuid7

from .grant import Grant, GrantError


@contextmanager
def projected_output(parent: Path, grant: Grant, *, cell_uid: int, cell_gid: int) -> Iterator[int]:
    """Create an exclusive directory for one verified projected Grant.

    The trusted Supervisor chooses parent, validates the Grant and must keep the
    parent private from all Cell identities. This context does not survive a
    crash and does not replace canonical Execution/Lease authorization.
    """
    if sys.platform != "linux" or os.geteuid() != 0 or not parent.is_absolute():
        raise GrantError("OUTPUT_ISOLATION_UNAVAILABLE")
    if cell_uid <= 0 or cell_gid <= 0 or grant.execution_binding_version != 1 or not is_uuid7(grant.execution_id):
        raise GrantError("OUTPUT_BINDING_INVALID")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = os.open("/", flags)
    try:
        for part in parent.parts[1:]:
            if part in (".", ".."):
                raise GrantError("OUTPUT_PARENT_INVALID")
            next_fd = os.open(part, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
    except BaseException:
        os.close(parent_fd)
        raise
    created = False
    assert grant.execution_id is not None
    name = grant.execution_id
    try:
        info = os.fstat(parent_fd)
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
            raise GrantError("OUTPUT_PARENT_NOT_PRIVATE")
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
        fd = os.open(name, flags, dir_fd=parent_fd)
        try:
            os.fchown(fd, cell_uid, cell_gid)
            os.fchmod(fd, 0o700)
            yield fd
        finally:
            os.close(fd)
    finally:
        try:
            if created:
                shutil.rmtree(name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)


def validate_projected_output(parent: Path, grant: Grant, fd: int, *, cell_uid: int, cell_gid: int) -> None:
    """Verify a preallocated output descriptor belongs to the exact signed execution."""
    if sys.platform != "linux" or os.geteuid() != 0 or not parent.is_absolute() or not is_uuid7(grant.execution_id):
        raise GrantError("OUTPUT_BINDING_INVALID")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open("/", flags)
    try:
        for part in parent.parts[1:]:
            if part in (".", ".."):
                raise GrantError("OUTPUT_PARENT_INVALID")
            next_fd = os.open(part, flags, dir_fd=root_fd)
            os.close(root_fd)
            root_fd = next_fd
        parent_info = os.fstat(root_fd)
        if parent_info.st_uid != 0 or stat.S_IMODE(parent_info.st_mode) != 0o700:
            raise GrantError("OUTPUT_PARENT_NOT_PRIVATE")
        assert grant.execution_id is not None
        named_fd = os.open(grant.execution_id, flags, dir_fd=root_fd)
        try:
            named, actual = os.fstat(named_fd), os.fstat(fd)
            with os.scandir(fd) as entries:
                has_entries = next(entries, None) is not None
            if (
                (named.st_dev, named.st_ino) != (actual.st_dev, actual.st_ino)
                or actual.st_uid != cell_uid
                or actual.st_gid != cell_gid
                or stat.S_IMODE(actual.st_mode) != 0o700
                or has_entries
            ):
                raise GrantError("OUTPUT_DESCRIPTOR_INVALID")
        finally:
            os.close(named_fd)
    except OSError as exc:
        raise GrantError("OUTPUT_DESCRIPTOR_INVALID") from exc
    finally:
        os.close(root_fd)
