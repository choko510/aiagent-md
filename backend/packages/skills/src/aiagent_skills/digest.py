"""Canonical digests for manifests, file lists and scan results.

Rules (documented in docs/06-memory-skills/skill-lifecycle-implementation.md):

* A file digest is SHA-256 over the exact file bytes.
* A bundle digest is SHA-256 over the canonical JSON list of `(path, sha256, byteSize)` sorted by path.
* A manifest digest excludes `signature` (otherwise a signature over the manifest would be circular) and
  excludes derived fields (`contentDigest`).
* A content digest binds the manifest digest, the bundle digest and the entry path.

No digest uses timestamps, so re-reading the same immutable release is reproducible.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from aiagent_domain.canonical import canonical_json, sha256_hex

from .contracts import SkillFile, SkillManifestV1


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(data: bytes) -> str:
    return sha256_bytes(data)


def bundle_digest(files: Iterable[tuple[str, bytes]]) -> str:
    entries = [{"path": path, "sha256": file_digest(data), "byteSize": len(data)} for path, data in sorted(files)]
    return sha256_hex(entries)


def manifest_digest(manifest: SkillManifestV1) -> str:
    wire = manifest.to_wire()
    wire.pop("signature", None)
    return sha256_hex(wire)


def content_digest(manifest: SkillManifestV1, files: Iterable[tuple[str, bytes]]) -> str:
    return sha256_hex(
        {
            "manifest": manifest_digest(manifest),
            "bundle": bundle_digest(files),
            "entry": manifest.entry,
        }
    )


def capability_digest(capabilities: Iterable[str]) -> str:
    return sha256_hex(sorted(set(capabilities)))


def dependency_digest(pins: dict[str, str]) -> str:
    return sha256_hex({name: pins[name] for name in sorted(pins)})


def findings_digest(findings: Iterable[Any]) -> str:
    payload = [f.to_wire() if hasattr(f, "to_wire") else f for f in findings]
    ordered = sorted(payload, key=lambda x: (str(x.get("ruleId")), str(x.get("path")), x.get("line") or 0))
    return sha256_hex(ordered)


def canonical_bytes(obj: Any) -> bytes:
    return canonical_json(obj).encode("utf-8")


def verify_file_list(declared: Iterable[SkillFile], files: dict[str, bytes]) -> None:
    """Ensure the declared manifest file list and the actual bytes are identical (no extra/missing file)."""
    from .errors import BundleInvalid, DigestMismatch

    declared_map = {f.relative_path: f for f in declared}
    if set(declared_map) != set(files):
        missing = sorted(set(files) - set(declared_map))
        absent = sorted(set(declared_map) - set(files))
        raise BundleInvalid("manifest file list does not match bundle", missing=missing, absent=absent)
    for path, data in files.items():
        expected = declared_map[path]
        if expected.byte_size != len(data) or expected.sha256 != file_digest(data):
            raise DigestMismatch(f"file digest mismatch: {path}", path=path)
