"""Verify bounded Git object proofs without running Git in the trusted verifier.

A proof establishes object integrity relative to an expected commit; it does not
establish repository authorization, Run authority, freshness or provenance.
The producer uses GitSnapshot inside the isolated execution boundary.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass

from .git_reader import GitSnapshot
from .models import Identity, ReadError
from .paths import validate_git_path
from .projection import ProjectionManifest, projection_path_selected

_HEX = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_MAX_TREES = 10000
_MAX_ENTRIES = 10000
_MAX_PROOF_BYTES = 32 * 1024 * 1024
_MAX_COMMIT_BYTES = 1024 * 1024
_MAX_DEPTH = 128


@dataclass(frozen=True)
class TreeObject:
    oid: str
    data: bytes


@dataclass(frozen=True)
class GitTreeProof:
    commit: bytes
    trees: tuple[TreeObject, ...]


@dataclass(frozen=True)
class GitTreeCheck:
    """A bounded integrity result, never an authenticated execution receipt."""

    commit: str
    tree: str
    tree_count: int
    entry_count: int
    projected_files: int
    object_hash: str
    collision_resistance: str


def _object_oid(kind: str, content: bytes, oid_length: int) -> str:
    digest = hashlib.new("sha1" if oid_length == 40 else "sha256", usedforsecurity=False)
    digest.update(f"{kind} {len(content)}\0".encode("ascii"))
    digest.update(content)
    return digest.hexdigest()


def _tree_entries(data: bytes, oid_bytes: int, deadline: float) -> list[tuple[bytes, str, str]]:
    result: list[tuple[bytes, str, str]] = []
    offset = 0
    while offset < len(data):
        if time.monotonic() > deadline or len(result) >= _MAX_ENTRIES:
            raise ReadError("Git tree proof budget exceeded")
        space = data.find(b" ", offset, min(offset + 8, len(data)))
        if space < 0:
            raise ReadError("invalid Git tree object")
        nul = data.find(b"\0", space + 1, min(space + 4098, len(data)))
        if nul < 0 or nul + 1 + oid_bytes > len(data):
            raise ReadError("invalid Git tree object")
        raw_name = data[space + 1 : nul]
        try:
            name = raw_name.decode("utf-8", "strict")
            if not name or "/" in name:
                raise ValueError("invalid component")
            validate_git_path(name)
        except (UnicodeError, ValueError) as exc:
            raise ReadError("unsafe Git tree path") from exc
        mode = data[offset:space]
        if mode not in (b"40000", b"100644", b"100755"):
            raise ReadError("unsupported Git tree mode")
        oid = data[nul + 1 : nul + 1 + oid_bytes].hex()
        result.append((mode, name, oid))
        offset = nul + 1 + oid_bytes
    return result


def build_git_tree_proof(reader: GitSnapshot) -> GitTreeProof:
    """Build a proof using bounded read-only Git operations inside the Cell."""
    commit = reader._git("cat-file", "commit", reader.snapshot.identity.commit)
    if len(commit) > _MAX_COMMIT_BYTES:
        raise ReadError("Git commit proof budget exceeded")
    reader._verify_object("commit", reader.snapshot.identity.commit, commit)
    pending = [reader.tree]
    queued = {reader.tree}
    found: dict[str, bytes] = {}
    total = len(commit)
    entry_count = 0
    oid_bytes = len(reader.tree) // 2
    while pending:
        if time.monotonic() > reader.deadline:
            raise ReadError("Git tree proof deadline exceeded")
        oid = pending.pop()
        if oid in found:
            continue
        if len(found) >= _MAX_TREES:
            raise ReadError("Git tree proof budget exceeded")
        data = reader._git("cat-file", "tree", oid)
        reader._verify_object("tree", oid, data)
        total += len(data)
        if total > _MAX_PROOF_BYTES:
            raise ReadError("Git tree proof budget exceeded")
        found[oid] = data
        entries = _tree_entries(data, oid_bytes, reader.deadline)
        entry_count += len(entries)
        if entry_count > _MAX_ENTRIES:
            raise ReadError("Git tree proof entry limit exceeded")
        for mode, _, child in entries:
            if mode == b"40000" and child not in queued:
                queued.add(child)
                pending.append(child)
    return GitTreeProof(commit, tuple(TreeObject(oid, found[oid]) for oid in sorted(found)))


def _validated_objects(
    proof: GitTreeProof, manifest: ProjectionManifest, expected_identity: Identity, deadline: float
) -> dict[str, bytes]:
    commit_oid = expected_identity.commit
    if manifest.identity != expected_identity or not isinstance(commit_oid, str) or not _HEX.fullmatch(commit_oid):
        raise ReadError("Git proof scope mismatch")
    if not isinstance(proof.commit, bytes) or len(proof.commit) > _MAX_COMMIT_BYTES or len(proof.trees) > _MAX_TREES:
        raise ReadError("Git tree proof budget exceeded")
    if _object_oid("commit", proof.commit, len(commit_oid)) != commit_oid:
        raise ReadError("Git commit proof mismatch")
    first = proof.commit.partition(b"\n")[0]
    if (
        not isinstance(manifest.tree, str)
        or not _HEX.fullmatch(manifest.tree)
        or first != b"tree " + manifest.tree.encode("ascii")
        or len(manifest.tree) != len(commit_oid)
    ):
        raise ReadError("Git commit tree mismatch")
    objects: dict[str, bytes] = {}
    total = len(proof.commit)
    for tree in proof.trees:
        if time.monotonic() > deadline:
            raise ReadError("Git tree proof deadline exceeded")
        if (
            not isinstance(tree.oid, str)
            or not isinstance(tree.data, bytes)
            or not _HEX.fullmatch(tree.oid)
            or len(tree.oid) != len(commit_oid)
            or tree.oid in objects
        ):
            raise ReadError("invalid Git tree proof object")
        total += len(tree.data)
        if total > _MAX_PROOF_BYTES or _object_oid("tree", tree.data, len(commit_oid)) != tree.oid:
            raise ReadError("Git tree proof mismatch or budget exceeded")
        objects[tree.oid] = tree.data
    return objects


def _walk_proof(
    objects: dict[str, bytes], root: str, oid_length: int, deadline: float
) -> tuple[dict[str, str], int, set[str]]:
    pending = [(root, "", 0)]
    visited: set[str] = set()
    all_paths: set[str] = set()
    selected: dict[str, str] = {}
    entry_count = 0
    while pending:
        if time.monotonic() > deadline:
            raise ReadError("Git tree proof deadline exceeded")
        oid, prefix, depth = pending.pop()
        if depth > _MAX_DEPTH or oid not in objects:
            raise ReadError("incomplete Git tree proof")
        visited.add(oid)
        for mode, name, child in _tree_entries(objects[oid], oid_length // 2, deadline):
            entry_count += 1
            if entry_count > _MAX_ENTRIES:
                raise ReadError("Git tree proof entry limit exceeded")
            path = f"{prefix}/{name}" if prefix else name
            try:
                validate_git_path(path)
            except (UnicodeError, ValueError) as exc:
                raise ReadError("unsafe Git tree path") from exc
            if path in all_paths:
                raise ReadError("duplicate Git tree path")
            all_paths.add(path)
            if mode == b"40000":
                pending.append((child, path, depth + 1))
            elif projection_path_selected(path):
                selected[path] = child
    return selected, entry_count, visited


def verify_git_tree_proof(
    proof: GitTreeProof, manifest: ProjectionManifest, expected_identity: Identity, *, deadline: float
) -> GitTreeCheck:
    """Check a supplied proof against a caller-selected identity; caller owns authorization."""
    objects = _validated_objects(proof, manifest, expected_identity, deadline)
    selected, count, visited = _walk_proof(objects, manifest.tree, len(expected_identity.commit), deadline)
    if visited != objects.keys():
        raise ReadError("unreferenced Git tree proof object")
    expected = {item.path: item.blob_id for item in manifest.files}
    if len(expected) != len(manifest.files) or selected != expected:
        raise ReadError("projection differs from Git tree")
    algorithm = "sha1" if len(expected_identity.commit) == 40 else "sha256"
    collision_resistance = "LEGACY_SHA1" if algorithm == "sha1" else "SHA256"
    return GitTreeCheck(
        expected_identity.commit, manifest.tree, len(visited), count, len(selected), algorithm, collision_resistance
    )
