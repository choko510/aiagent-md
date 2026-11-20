"""Bounded source projection wire encoding; this is not an authorization boundary.

Only a trusted transport may consume these chunks.  Incomplete streams must be
quarantined and discarded; no file is published by this module.
"""

from __future__ import annotations

import hashlib
import struct
import time
from collections.abc import Iterator
from dataclasses import dataclass

from .git_reader import GitSnapshot
from .models import ReadError
from .projection import ProjectionFile, ProjectionManifest, prepare_projection

_MAGIC = b"AIAGENT-SOURCE-1\x00"
_MAX_WIRE = 128 * 1024 * 1024
_MAX_CHUNK = 64 * 1024


@dataclass(frozen=True)
class ProjectionStreamReceipt:
    """Digest and size of the exact complete wire bytes, not an auth receipt."""

    sha256: str
    byte_length: int
    file_count: int


def _field(value: str, maximum: int) -> bytes:
    try:
        raw = value.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise ReadError("unsupported projection metadata") from exc
    if not raw or len(raw) > maximum:
        raise ReadError("invalid projection metadata length")
    return struct.pack(">I", len(raw)) + raw


def _header(item: ProjectionFile) -> bytes:
    return (
        _field(item.path, 4096) + _field(item.blob_id, 64) + bytes.fromhex(item.sha256) + struct.pack(">Q", item.size)
    )


def _wire_length(prefix: bytes, files: tuple[ProjectionFile, ...], limit: int, deadline: float) -> int:
    size = len(prefix)
    for item in files:
        if time.monotonic() > deadline:
            raise ReadError("projection stream deadline exceeded")
        size += len(_header(item)) + item.size
        if size > limit:
            raise ReadError("projection wire limit exceeded")
    if size > limit:
        raise ReadError("projection wire limit exceeded")
    return size


def _source_sha(data: bytes, deadline: float) -> str:
    digest = hashlib.sha256()
    for offset in range(0, len(data), _MAX_CHUNK):
        if time.monotonic() > deadline:
            raise ReadError("projection stream deadline exceeded")
        digest.update(data[offset : offset + _MAX_CHUNK])
    return digest.hexdigest()


class ProjectionEncoder:
    """Single-use wire encoder bound to a GitSnapshot and its verified manifest.

    The authenticated broker must enforce scope, deadline and publication. This
    encoder cannot make a slow consumer progress or revoke bytes already sent.
    """

    def __init__(self, reader: GitSnapshot, manifest: ProjectionManifest, *, max_wire_bytes: int = _MAX_WIRE) -> None:
        if not 1 <= max_wire_bytes <= _MAX_WIRE:
            raise ValueError("invalid projection wire limit")
        self._reader = reader
        self._manifest = manifest
        self._max_wire = max_wire_bytes
        self._started = False
        self.receipt: ProjectionStreamReceipt | None = None

    def chunks(self) -> Iterator[bytes]:
        """Yield at most 64 KiB per chunk; receipt appears only after exhaustion."""
        if self._started:
            raise ReadError("projection stream already used")
        self._started = True
        reader, manifest = self._reader, self._manifest
        if prepare_projection(reader, manifest.identity, manifest.tree) != manifest:
            raise ReadError("projection manifest mismatch")
        entries, issues = reader.entries()
        if issues:
            raise ReadError("incomplete snapshot tree")
        indexed = {entry.path: entry for entry in entries}
        identity = manifest.identity
        prefix = _MAGIC + b"".join(
            _field(field, 1024)
            for field in (identity.tenant_id, identity.owner_id, identity.binding_id, identity.revision)
        )
        prefix += _field(identity.commit, 64) + _field(manifest.tree, 64) + struct.pack(">I", len(manifest.files))
        wire_size = _wire_length(prefix, manifest.files, self._max_wire, reader.deadline)
        digest = hashlib.sha256()
        emitted = 0

        def bounded(data: bytes) -> Iterator[bytes]:
            nonlocal emitted
            for offset in range(0, len(data), _MAX_CHUNK):
                if time.monotonic() > reader.deadline:
                    raise ReadError("projection stream deadline exceeded")
                chunk = data[offset : offset + _MAX_CHUNK]
                digest.update(chunk)
                emitted += len(chunk)
                yield chunk

        yield from bounded(prefix)
        for item in manifest.files:
            entry = indexed.get(item.path)
            if entry is None or entry.oid != item.blob_id or entry.size != item.size:
                raise ReadError("projection entry mismatch")
            data = reader.read(entry)
            if len(data) != item.size or _source_sha(data, reader.deadline) != item.sha256:
                raise ReadError("projection source digest mismatch")
            yield from bounded(_header(item))
            yield from bounded(data)
        if time.monotonic() > reader.deadline:
            raise ReadError("projection stream deadline exceeded")
        if emitted != wire_size:
            raise ReadError("projection stream length mismatch")
        self.receipt = ProjectionStreamReceipt(digest.hexdigest(), emitted, len(manifest.files))
