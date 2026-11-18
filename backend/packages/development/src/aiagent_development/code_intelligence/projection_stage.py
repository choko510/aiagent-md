"""Private staging of a bounded projection stream, without publication or authorization.

The caller must be a trusted broker, provide an authority-verified manifest and
receipt, and enforce an I/O deadline externally. This module does not supply
identity, a read-only Cell mount, lease checks or atomic Artifact publication.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import shutil
import stat
import struct
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .deadline import require_deadline
from .git_proof import GitTreeCheck, GitTreeProof, verify_git_tree_proof
from .models import Identity, ReadError
from .paths import validate_git_path
from .projection import ProjectionManifest
from .projection_stream import _MAGIC, _MAX_CHUNK, _MAX_WIRE, ProjectionStreamReceipt, _field, _header, _wire_length

_HEX = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class _WireReader:
    def __init__(
        self, stream: BinaryIO, expected: ProjectionStreamReceipt, deadline: float, active: Callable[[], bool]
    ) -> None:
        self.stream = stream
        self.remaining = expected.byte_length
        self.deadline = deadline
        self.active = active
        self.digest = hashlib.sha256()

    def read(self, length: int) -> bytes:
        if not 0 <= length <= _MAX_CHUNK or length > self.remaining:
            raise ReadError("invalid projection frame length")
        result = bytearray()
        while len(result) < length:
            if time.monotonic() > self.deadline or not self.active():
                raise ReadError("projection receive deadline exceeded")
            block = self.stream.read(length - len(result))
            if time.monotonic() > self.deadline or not self.active():
                raise ReadError("projection receive deadline exceeded")
            if not isinstance(block, bytes) or not block or len(block) > length - len(result):
                raise ReadError("projection transfer incomplete")
            self.digest.update(block)
            self.remaining -= len(block)
            result.extend(block)
        return bytes(result)


def _prefix(manifest: ProjectionManifest) -> bytes:
    identity = manifest.identity
    result = _MAGIC + b"".join(
        _field(field, 1024) for field in (identity.tenant_id, identity.owner_id, identity.binding_id, identity.revision)
    )
    return result + _field(identity.commit, 64) + _field(manifest.tree, 64) + struct.pack(">I", len(manifest.files))


def _validate_manifest(manifest: ProjectionManifest, receipt: ProjectionStreamReceipt, deadline: float) -> bytes:
    if (
        len(manifest.files) > 10000
        or receipt.file_count != len(manifest.files)
        or not _SHA256.fullmatch(receipt.sha256)
        or not _HEX.fullmatch(manifest.identity.commit)
        or not _HEX.fullmatch(manifest.tree)
        or not 1 <= receipt.byte_length <= _MAX_WIRE
    ):
        raise ReadError("invalid projection receipt")
    previous = ""
    total = 0
    for item in manifest.files:
        if time.monotonic() > deadline:
            raise ReadError("projection receive deadline exceeded")
        try:
            validate_git_path(item.path)
        except (ValueError, UnicodeError) as exc:
            raise ReadError("invalid projection path") from exc
        if (
            item.path <= previous
            or not _HEX.fullmatch(item.blob_id)
            or not _SHA256.fullmatch(item.sha256)
            or type(item.size) is not int
            or not 0 <= item.size <= 64 * 1024 * 1024
        ):
            raise ReadError("invalid projection manifest")
        previous = item.path
        total += item.size
    if total != manifest.total_bytes:
        raise ReadError("projection total mismatch")
    prefix = _prefix(manifest)
    if _wire_length(prefix, manifest.files, _MAX_WIRE, deadline) != receipt.byte_length:
        raise ReadError("projection receipt length mismatch")
    return prefix


def _open_trusted_parent(path: Path) -> int:
    if not path.is_absolute():
        raise ReadError("invalid projection parent")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            if part in (".", ".."):
                raise ReadError("invalid projection parent")
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _parent_dir(root_fd: int, parts: tuple[str, ...]) -> int:
    fd = os.dup(root_fd)
    try:
        for name in parts:
            with suppress(FileExistsError):
                os.mkdir(name, 0o700, dir_fd=fd)
            next_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.fchmod(next_fd, 0o755)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_all(fd: int, data: bytes, deadline: float) -> None:
    view = memoryview(data)
    while view:
        if time.monotonic() > deadline:
            raise ReadError("projection receive deadline exceeded")
        count = os.write(fd, view)
        if count <= 0:
            raise ReadError("projection staging write failed")
        view = view[count:]


def _stage_files(root_fd: int, manifest: ProjectionManifest, wire: _WireReader) -> None:
    for item in manifest.files:
        if wire.read(len(_header(item))) != _header(item):
            raise ReadError("projection metadata mismatch")
        parts = tuple(item.path.split("/"))
        parent_fd = _parent_dir(root_fd, parts[:-1])
        try:
            fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        try:
            actual_sha = hashlib.sha256()
            algo = "sha1" if len(item.blob_id) == 40 else "sha256"
            git_sha = hashlib.new(algo, usedforsecurity=False)
            git_sha.update(f"blob {item.size}\0".encode("ascii"))
            left = item.size
            while left:
                chunk = wire.read(min(left, _MAX_CHUNK))
                _write_all(fd, chunk, wire.deadline)
                actual_sha.update(chunk)
                git_sha.update(chunk)
                left -= len(chunk)
            st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_nlink != 1
                or st.st_size != item.size
                or not hmac.compare_digest(actual_sha.hexdigest(), item.sha256)
                or not hmac.compare_digest(git_sha.hexdigest(), item.blob_id)
            ):
                raise ReadError("projection source verification failed")
            os.fchmod(fd, 0o644)
        finally:
            os.close(fd)


@contextmanager
def stage_projection(
    trusted_parent: Path,
    stream: BinaryIO,
    manifest: ProjectionManifest,
    receipt: ProjectionStreamReceipt,
    *,
    deadline: float,
    active: Callable[[], bool] = lambda: True,
) -> Iterator[int]:
    """Stage to a private POSIX directory and yield its open directory descriptor.

    This staging area is writable by its owner and is NOT a read-only execution
    boundary. Its parent must be inaccessible to the Cell UID. Files become
    readable by other UIDs only through an explicitly passed descriptor; a
    separate kernel restriction must deny writes. No identities here authorize.
    """
    require_deadline(deadline)
    if not active():
        raise ReadError("projection authority revoked")
    if os.name != "posix" or not trusted_parent.is_absolute():
        raise ReadError("secure POSIX staging unavailable")
    prefix = _validate_manifest(manifest, receipt, deadline)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = _open_trusted_parent(trusted_parent)
    name = "projection-" + secrets.token_hex(16)
    created = False
    try:
        parent_info = os.fstat(parent_fd)
        if parent_info.st_uid != os.geteuid() or stat.S_IMODE(parent_info.st_mode) & 0o077:
            raise ReadError("projection parent is not private")
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
        root_fd = os.open(name, flags, dir_fd=parent_fd)
        try:
            os.fchmod(root_fd, 0o755)
            wire = _WireReader(stream, receipt, deadline, active)
            if wire.read(len(prefix)) != prefix:
                raise ReadError("projection identity mismatch")
            _stage_files(root_fd, manifest, wire)
            if time.monotonic() > deadline or not active():
                raise ReadError("projection receive deadline exceeded")
            if wire.remaining or not hmac.compare_digest(wire.digest.hexdigest(), receipt.sha256):
                raise ReadError("projection stream digest mismatch")
            yield root_fd
        finally:
            os.close(root_fd)
    finally:
        try:
            if created:
                shutil.rmtree(name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)


@contextmanager
def cell_source_traversal(trusted_parent: Path, *, cell_gid: int) -> Iterator[None]:
    """Temporarily grant a dedicated Cell group traversal, never directory listing.

    Caller must enforce a separate Landlock domain per execution, use a dedicated
    Cell identity, and clean up after crashes. This is not an authorization check.
    """
    if os.name != "posix" or os.geteuid() != 0 or type(cell_gid) is not int or cell_gid <= 0:
        raise ReadError("Cell traversal isolation unavailable")
    fd = _open_trusted_parent(trusted_parent)
    changed = False
    try:
        before = os.fstat(fd)
        if before.st_uid != 0 or stat.S_IMODE(before.st_mode) != 0o700:
            raise ReadError("projection parent is not private")
        os.fchown(fd, -1, cell_gid)
        changed = True
        os.fchmod(fd, 0o710)
        yield
    finally:
        try:
            if changed:
                os.fchmod(fd, 0o700)
                os.fchown(fd, -1, before.st_gid)
        finally:
            os.close(fd)


@dataclass(frozen=True)
class GitStagedProjection:
    root_fd: int
    check: GitTreeCheck


@contextmanager
def stage_git_projection(
    trusted_parent: Path,
    stream: BinaryIO,
    manifest: ProjectionManifest,
    receipt: ProjectionStreamReceipt,
    proof: GitTreeProof,
    expected_identity: Identity,
    *,
    deadline: float,
    active: Callable[[], bool] = lambda: True,
    require_complete: bool = False,
) -> Iterator[GitStagedProjection]:
    """Verify object relationships and expose hash strength; caller owns authorization."""
    check = verify_git_tree_proof(proof, manifest, expected_identity, deadline=deadline, active=active)
    if require_complete and check.excluded_files:
        raise ReadError("complete source projection unavailable")
    with stage_projection(trusted_parent, stream, manifest, receipt, deadline=deadline, active=active) as root_fd:
        yield GitStagedProjection(root_fd, check)
