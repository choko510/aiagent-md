"""Fail-closed snapshot inventory for a future isolated indexer.

This module does not launch commands, authenticate claims, transfer source, or
represent an execution receipt. The caller must obtain identity from authority.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .git_reader import GitSnapshot
from .models import Identity, ReadError


@dataclass(frozen=True)
class ProjectionFile:
    path: str
    blob_id: str
    sha256: str
    size: int


@dataclass(frozen=True)
class ProjectionManifest:
    identity: Identity
    tree: str
    files: tuple[ProjectionFile, ...]
    total_bytes: int


def prepare_projection(reader: GitSnapshot, identity: Identity, tree: str) -> ProjectionManifest:
    """Verify every tree entry before a future broker may materialize a projection.

    This is deliberately all-or-nothing. The manifest is not an authorization
    grant and does not prove a Cell, source immutability, or toolchain identity.
    """
    if identity != reader.snapshot.identity or tree != reader.tree:
        raise ReadError("snapshot scope or tree mismatch")
    entries, issues = reader.entries()
    if issues:
        raise ReadError("incomplete snapshot tree")
    files: list[ProjectionFile] = []
    total = 0
    for entry in entries:
        if entry.kind != "BLOB" or entry.mode not in ("100644", "100755"):
            raise ReadError("unsupported snapshot entry")
        data = reader.read(entry)
        if data.startswith(b"version https://git-lfs.github.com/spec/v1\n") or data.startswith(
            b"version https://git-lfs.github.com/spec/v1\r\n"
        ):
            raise ReadError("LFS content unavailable")
        total += len(data)
        if total > reader.limits.total_bytes:
            raise ReadError("projection byte budget exceeded")
        files.append(ProjectionFile(entry.path, entry.oid, hashlib.sha256(data).hexdigest(), len(data)))
    return ProjectionManifest(identity, tree, tuple(files), total)
