"""Encrypted, scoped execution Artifact publication in the canonical PostgreSQL DB.

This internal service is not registered as a model-facing API. The caller must
configure real trusted Capture and SCIP validators; no permissive default exists.
An authenticated Cell transfer, execution completion attestation and production
wiring are not supplied by this module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import stat
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import psycopg
from aiagent_core import control
from aiagent_core.db import Database
from aiagent_domain import require_uuid7, uuid7
from cryptography.exceptions import InvalidTag

from .context_keys import KeyRing

_MAX_BYTES = 4 * 1024 * 1024
_CHUNK = 64 * 1024


class ExecutionArtifactError(RuntimeError):
    """Fixed diagnostic only; never include Artifact content or provider details."""


class CaptureVerifier(Protocol):
    def verify(self, claim: control.Claim, execution_id: str, digest: str, size: int, proof: bytes) -> None:
        """Authenticate capture provenance and exact execution binding; reject unavailable attestation."""


class ScipVerifier(Protocol):
    def verify(self, claim: control.Claim, execution_id: str, content: bytes) -> str:
        """Validate SCIP against the authorized snapshot, returning VALIDATED or PARTIAL."""


@dataclass(frozen=True)
class ExecutionArtifactRef:
    id: str
    execution_id: str
    sha256: str
    byte_length: int
    coverage: str
    origin_trust: str = "UNVERIFIED"


def _deadline(deadline: float) -> None:
    now = time.monotonic()
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
        or not now < deadline <= now + 3600
    ):
        raise ExecutionArtifactError("ARTIFACT_DEADLINE")


def _aad(
    claim: control.Claim,
    workspace_id: str,
    execution_id: str,
    artifact_id: str,
    digest: str,
    size: int,
    coverage: str,
    key_id: str,
) -> bytes:
    return json.dumps(
        [
            "execution-scip-v1",
            claim.tenant_id,
            claim.owner_user_id,
            workspace_id,
            claim.task_id,
            claim.run_id,
            execution_id,
            artifact_id,
            digest,
            size,
            coverage,
            key_id,
        ],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _read_private_fd(fd: int, deadline: float) -> bytes:
    if sys.platform != "linux":
        raise ExecutionArtifactError("ARTIFACT_FD_UNAVAILABLE")
    import fcntl

    try:
        before = os.fstat(fd)
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 0
            or before.st_uid != os.geteuid()
            or flags & os.O_ACCMODE != os.O_RDONLY
            or not 1 <= before.st_size <= _MAX_BYTES
        ):
            raise ExecutionArtifactError("INVALID_CAPTURE")
        result = bytearray()
        while len(result) < before.st_size:
            _deadline(deadline)
            data = os.pread(fd, min(_CHUNK, before.st_size - len(result)), len(result))
            _deadline(deadline)
            if not data:
                raise ExecutionArtifactError("INCOMPLETE_CAPTURE")
            result.extend(data)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ExecutionArtifactError("CAPTURE_CHANGED")
        return bytes(result)
    except OSError as exc:
        raise ExecutionArtifactError("CAPTURE_UNAVAILABLE") from exc


def _db_deadline(c: psycopg.Connection[object], deadline: float) -> None:
    _deadline(deadline)
    ms = max(1, int((deadline - time.monotonic()) * 1000))
    c.execute("SELECT set_config('statement_timeout', %s, true)", (f"{ms}ms",))
    c.execute("SELECT set_config('lock_timeout', %s, true)", (f"{ms}ms",))


class PostgresExecutionArtifactStore:
    """Gateway-owned encrypted storage with DB-serialized publication and scoped reads."""

    def __init__(
        self,
        db: Database,
        claim: control.Claim,
        keys: KeyRing,
        *,
        capture: CaptureVerifier,
        index: ScipVerifier,
        ttl: timedelta = timedelta(days=1),
    ) -> None:
        if not timedelta(0) < ttl <= timedelta(days=30):
            raise ValueError("invalid Artifact retention")
        self._db, self._claim, self._keys = db, claim, keys
        self._capture, self._index, self._ttl = capture, index, ttl

    def _workspace(self, deadline: float) -> str:
        with self._db.tx(tenant=self._claim.tenant_id) as c:
            _db_deadline(c, deadline)
            row = control.check_current(c, self._claim)
            return str(row["workspace_id"])

    def _scope_args(self, workspace: str, execution_id: str) -> tuple[object, ...]:
        claim = self._claim
        return (
            claim.tenant_id,
            claim.owner_user_id,
            workspace,
            claim.task_id,
            claim.run_id,
            claim.goal_revision,
            claim.lease_epoch,
            claim.worker_ref,
            claim.restore_incarnation,
            execution_id,
        )

    def publish(self, execution_id: str, capture_fd: int, proof: bytes, *, deadline: float) -> ExecutionArtifactRef:
        """Publish only a verified finished execution's captured bytes; no stdout is used."""
        _deadline(deadline)
        require_uuid7(execution_id, "execution id")
        if not isinstance(proof, bytes) or not 1 <= len(proof) <= 16384:
            raise ExecutionArtifactError("CAPTURE_PROOF_INVALID")
        workspace = self._workspace(deadline)
        data = _read_private_fd(capture_fd, deadline)
        digest = hashlib.sha256(data).hexdigest()
        self._capture.verify(self._claim, execution_id, digest, len(data), proof)
        _deadline(deadline)
        coverage = self._index.verify(self._claim, execution_id, data)
        if coverage not in ("VALIDATED", "PARTIAL"):
            raise ExecutionArtifactError("INVALID_SCIP")
        _deadline(deadline)
        artifact_id = uuid7()
        key_id = self._keys.active
        nonce = os.urandom(12)
        ciphertext = self._keys.cipher(key_id).encrypt(
            nonce, data, _aad(self._claim, workspace, execution_id, artifact_id, digest, len(data), coverage, key_id)
        )
        try:
            with self._db.tx(tenant=self._claim.tenant_id) as c:
                _db_deadline(c, deadline)
                row = c.execute(
                    "SELECT agent.execution_artifact_publish(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS id",
                    (
                        *self._scope_args(workspace, execution_id),
                        artifact_id,
                        digest,
                        len(data),
                        coverage,
                        key_id,
                        nonce,
                        ciphertext,
                        datetime.now(UTC) + self._ttl,
                        hashlib.sha256(proof).hexdigest(),
                    ),
                ).fetchone()
                if row is None or row["id"] is None:
                    raise ExecutionArtifactError("ARTIFACT_UNAVAILABLE")
                return ExecutionArtifactRef(str(row["id"]), execution_id, digest, len(data), coverage)
        except psycopg.Error as exc:
            raise ExecutionArtifactError("ARTIFACT_PUBLICATION_DENIED") from exc

    def fetch(self, execution_id: str, *, deadline: float) -> tuple[ExecutionArtifactRef, bytes]:
        """Fetch only while the same scoped Run and snapshot remain current."""
        _deadline(deadline)
        require_uuid7(execution_id, "execution id")
        workspace = self._workspace(deadline)
        try:
            with self._db.tx(tenant=self._claim.tenant_id) as c:
                _db_deadline(c, deadline)
                row = c.execute(
                    "SELECT * FROM agent.execution_artifact_fetch(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    self._scope_args(workspace, execution_id),
                ).fetchone()
        except psycopg.Error as exc:
            raise ExecutionArtifactError("ARTIFACT_UNAVAILABLE") from exc
        if row is None:
            raise ExecutionArtifactError("ARTIFACT_UNAVAILABLE")
        artifact_id, digest = str(row["id"]), str(row["plaintext_sha256"]).strip()
        size, coverage, key_id = int(row["byte_length"]), str(row["coverage"]), str(row["key_id"])
        try:
            data = self._keys.cipher(key_id).decrypt(
                bytes(row["nonce"]),
                bytes(row["ciphertext"]),
                _aad(self._claim, workspace, execution_id, artifact_id, digest, size, coverage, key_id),
            )
        except (InvalidTag, KeyError, ValueError) as exc:
            raise ExecutionArtifactError("ARTIFACT_INTEGRITY") from exc
        _deadline(deadline)
        if len(data) != size or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), digest):
            raise ExecutionArtifactError("ARTIFACT_INTEGRITY")
        return ExecutionArtifactRef(artifact_id, execution_id, digest, size, coverage), data
