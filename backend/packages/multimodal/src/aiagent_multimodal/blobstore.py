"""Owner-scoped quarantine blob store.

The server never uses a client filename as a path. Storage keys are opaque
<tenant-uuid>/<artifact-uuid>.bin; every read re-validates the key, the tenant
prefix and the digest, and every write is atomic (temp + fsync + replace).
Chunks are appended to a staging directory, then concatenated only after the
total length and complete SHA-256 match. This is filesystem-level quarantine,
not malware sanitisation: callers must still run archive/XML/active-content gates.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .errors import MmiError

_KEY: Final = re.compile(r"^[0-9a-f-]{36}/[0-9a-f-]{36}\.bin$")
_COPY: Final = 1 << 20


@dataclass(frozen=True, slots=True)
class StoredBlob:
    storage_key: str
    sha256: str
    byte_length: int


class QuarantineBlobStore:
    """Filesystem quarantine. root must be owner-only writable on the host."""

    def __init__(self, root: Path | str, *, max_file_bytes: int) -> None:
        self.root = Path(root).resolve()
        self.max_file_bytes = max_file_bytes
        self._stage = self.root / "staging"
        self._blobs = self.root / "quarantine"
        self._stage.mkdir(parents=True, exist_ok=True)
        self._blobs.mkdir(parents=True, exist_ok=True)
        self._chmod(self._stage, 0o700)
        self._chmod(self._blobs, 0o700)

    @staticmethod
    def _chmod(path: Path, mode: int) -> None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass

    def _tenant_dir(self, base: Path, tenant_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f-]{36}", tenant_id):
            raise MmiError("POLICY_DENIED", "invalid tenant identifier")
        path = base / tenant_id
        path.mkdir(parents=True, exist_ok=True)
        self._chmod(path, 0o700)
        return path

    def put_chunk(self, tenant_id: str, upload_id: str, index: int, data: bytes) -> str:
        if index < 0 or index > 65535:
            raise MmiError("MAX_INPUT", "chunk index out of range")
        if not re.fullmatch(r"[0-9a-f-]{36}", upload_id):
            raise MmiError("POLICY_DENIED", "invalid upload identifier")
        if not data or len(data) > _COPY:
            raise MmiError("MAX_INPUT", "chunk length out of range")
        chunk_dir = self._tenant_dir(self._stage, tenant_id) / upload_id
        chunk_dir.mkdir(parents=True, exist_ok=True)
        target = chunk_dir / f"{index:05d}.part"
        if target.exists():
            existing = target.read_bytes()
            if existing != data:
                raise MmiError("POLICY_DENIED", "chunk replay differs")
            return hashlib.sha256(data).hexdigest()
        temp = target.with_suffix(".tmp")
        with temp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, target)
        return hashlib.sha256(data).hexdigest()

    def finalize(
        self,
        tenant_id: str,
        upload_id: str,
        artifact_id: str,
        *,
        expected_sha256: str,
        expected_bytes: int,
        chunk_count: int,
    ) -> StoredBlob:
        if expected_bytes < 1 or expected_bytes > self.max_file_bytes:
            raise MmiError("MAX_INPUT", "artifact size out of range")
        chunk_dir = self._tenant_dir(self._stage, tenant_id) / upload_id
        if not chunk_dir.is_dir():
            raise MmiError("MAX_INPUT", "no uploaded chunks")
        final_tmp = self.root / f".final-{artifact_id}.tmp"
        h = hashlib.sha256()
        total = 0
        try:
            with final_tmp.open("wb") as out:
                for index in range(chunk_count):
                    part = chunk_dir / f"{index:05d}.part"
                    if not part.is_file():
                        raise MmiError("MAX_INPUT", "missing upload chunk")
                    with part.open("rb") as src:
                        while True:
                            block = src.read(_COPY)
                            if not block:
                                break
                            total += len(block)
                            if total > expected_bytes or total > self.max_file_bytes:
                                raise MmiError("MAX_INPUT", "upload exceeds declared size")
                            h.update(block)
                            out.write(block)
                out.flush()
                os.fsync(out.fileno())
            if total != expected_bytes:
                raise MmiError("MAX_INPUT", "upload length mismatch")
            digest = h.hexdigest()
            if digest != expected_sha256:
                raise MmiError("SOURCE_MISMATCH", "upload digest mismatch")
            dest_dir = self._tenant_dir(self._blobs, tenant_id)
            dest = dest_dir / f"{artifact_id}.bin"
            os.replace(final_tmp, dest)
            self._chmod(dest, 0o600)
            key = f"{tenant_id}/{artifact_id}.bin"
            if not _KEY.fullmatch(key):
                raise MmiError("POLICY_DENIED", "invalid storage key")
            return StoredBlob(key, digest, total)
        finally:
            if final_tmp.exists():
                final_tmp.unlink(missing_ok=True)
            shutil.rmtree(chunk_dir, ignore_errors=True)

    def _resolve(self, storage_key: str) -> Path:
        if not _KEY.fullmatch(storage_key):
            raise MmiError("POLICY_DENIED", "invalid storage key")
        tenant, name = storage_key.split("/", 1)
        path = (self._blobs / tenant / name).resolve()
        root = (self._blobs / tenant).resolve()
        if path.parent != root or not path.is_file():
            raise MmiError("QUARANTINED", "artifact is not available")
        return path

    def read(self, storage_key: str, expected_sha256: str, expected_bytes: int) -> bytes:
        path = self._resolve(storage_key)
        size = path.stat().st_size
        if size != expected_bytes or size > self.max_file_bytes:
            raise MmiError("SOURCE_MISMATCH", "artifact length changed")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise MmiError("SOURCE_MISMATCH", "artifact digest changed")
        return data

    def delete(self, storage_key: str) -> None:
        try:
            path = self._resolve(storage_key)
        except MmiError:
            return
        path.unlink(missing_ok=True)

    def abort_upload(self, tenant_id: str, upload_id: str) -> None:
        if not re.fullmatch(r"[0-9a-f-]{36}", upload_id):
            return
        shutil.rmtree(self._stage / tenant_id / upload_id, ignore_errors=True)
