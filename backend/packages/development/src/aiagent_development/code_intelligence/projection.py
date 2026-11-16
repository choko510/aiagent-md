"""Fail-closed snapshot inventory for a future isolated indexer.

This module does not launch commands, authenticate claims, transfer source, or
represent an execution receipt. The caller must obtain identity from authority.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .git_reader import GitSnapshot
from .models import Coverage, Identity, ReadError
from .paths import display_path


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
    coverage: Coverage
    issues: tuple[str, ...]


def projection_path_selected(path: str) -> bool:
    """Fixed B2 source policy; this is not proof that selected content is secret-free."""
    p = PurePosixPath(path)
    parts = tuple(part.lower() for part in p.parts)
    name = parts[-1]
    sensitive = (
        name.startswith(".env")
        or name in {"credentials", "id_rsa", "id_ed25519"}
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
        or any(word in name for word in ("credential", "secret", "private_key", "token"))
        or any(part in {".ssh", ".aws", ".config", "secrets", "credentials"} for part in parts)
    )
    return not sensitive and (p.suffix.lower() in {".py", ".pyi"} or name == "pyproject.toml")


def prepare_projection(reader: GitSnapshot, identity: Identity, tree: str) -> ProjectionManifest:
    """Verify every tree entry before a future broker may materialize a projection.

    This is deliberately all-or-nothing. The manifest is not an authorization
    grant and does not prove a Cell, source immutability, or toolchain identity.
    """
    if identity != reader.snapshot.identity or tree != reader.tree:
        raise ReadError("snapshot scope or tree mismatch")
    entries, tree_issues = reader.entries()
    if tree_issues:
        raise ReadError("incomplete snapshot tree")
    files: list[ProjectionFile] = []
    total = 0
    issues = ["source content has not been verified secret-free"]
    for entry in entries:
        if entry.kind != "BLOB" or entry.mode not in ("100644", "100755"):
            raise ReadError("unsupported snapshot entry")
        if not projection_path_selected(entry.path):
            issues.append(f"{display_path(entry.path)}: excluded by source policy")
            continue
        data = reader.read(entry)
        # Heuristics only: a hit is a rejection, absence of a hit is not proof of safety.
        lowered = data.lower()
        if b"-----begin " in lowered and b"private key-----" in lowered:
            raise ReadError("private key material in selected source")
        if re.search(
            rb"(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)\s*[:=]\s*['\"][^'\"\r\n]{4,}", lowered
        ):
            raise ReadError("possible credential in selected source")
        if data.startswith(b"version https://git-lfs.github.com/spec/v1\n") or data.startswith(
            b"version https://git-lfs.github.com/spec/v1\r\n"
        ):
            raise ReadError("LFS content unavailable")
        total += len(data)
        if total > reader.limits.total_bytes:
            raise ReadError("projection byte budget exceeded")
        files.append(ProjectionFile(entry.path, entry.oid, hashlib.sha256(data).hexdigest(), len(data)))
    return ProjectionManifest(identity, tree, tuple(files), total, Coverage.PARTIAL, tuple(issues))
