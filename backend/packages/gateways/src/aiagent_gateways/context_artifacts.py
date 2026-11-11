"""Encrypted PostgreSQL Context artifacts, internal and not connected to model dispatch.

Blob and metadata use one database transaction. A caller must provide a claim issued by
STEP 3, a key supplied outside the database, and a finite retention deadline. This is
not an Evidence authority or an authorization to send a model request.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from datetime import datetime
from hashlib import sha256

import psycopg
from aiagent_context.models import RawArtifact, SafeStop, Scope, SourceInput, forbid_secrets
from aiagent_core import control
from aiagent_core.db import Database, Row
from aiagent_domain import uuid7
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class PostgresArtifactStore:
    """Internal ArtifactStorePort; scope derives from a trusted claim, never from model text."""

    def __init__(self, db: Database, claim: control.Claim, key: bytes, *, expires_at: datetime) -> None:
        if len(key) != 32 or expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("a 256-bit key and timezone-aware expiry are required")
        self._db = db
        self._claim = claim
        self._cipher = AESGCM(key)
        self._expires_at = expires_at

    def _scope(self, scope: Scope) -> None:
        claim = self._claim
        if (scope.tenant_id, scope.owner_id, scope.task_id, scope.run_id) != (
            claim.tenant_id,
            claim.owner_user_id,
            claim.task_id,
            claim.run_id,
        ):
            raise SafeStop("ARTIFACT_UNAVAILABLE")

    def _current(self, c: psycopg.Connection[Row]) -> None:
        try:
            control.check_current(c, self._claim, for_update=True)
        except control.ControlError as exc:
            raise SafeStop("STALE_CONTEXT") from exc
        claim = self._claim
        c.execute(
            "SELECT set_config('aiagent.artifact_tenant', %s, true),"
            " set_config('aiagent.artifact_owner', %s, true),"
            " set_config('aiagent.artifact_task', %s, true),"
            " set_config('aiagent.artifact_run', %s, true)",
            (claim.tenant_id, claim.owner_user_id, claim.task_id, claim.run_id),
        )

    def _aad(self, ref: str, source_run_id: str) -> bytes:
        c = self._claim
        return json.dumps([c.tenant_id, c.owner_user_id, c.task_id, source_run_id, ref], separators=(",", ":")).encode()

    def _put(self, scope: Scope, kind: str, body: bytes, metadata: dict[str, str]) -> tuple[str, str]:
        self._scope(scope)
        if not body or len(body) > 2_000_000:
            raise SafeStop("INVALID_SOURCE")
        artifact_id = str(uuid7())
        digest = sha256(body).hexdigest()
        envelope = json.dumps(
            {"body": base64.b64encode(body).decode("ascii"), "metadata": metadata},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        nonce = os.urandom(12)
        encrypted = self._cipher.encrypt(nonce, envelope, self._aad(artifact_id, scope.run_id))
        with self._db.tx() as c:
            self._current(c)
            # Metadata and bytes are committed together; a rolled-back write leaves neither visible.
            row = c.execute(
                "INSERT INTO agent.context_artifacts(tenant_id,id,task_id,run_id,owner_user_id,kind,"
                "content_sha256,byte_length,nonce,ciphertext,expires_at)"
                " SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s WHERE %s > clock_timestamp() RETURNING id",
                (
                    scope.tenant_id,
                    artifact_id,
                    scope.task_id,
                    scope.run_id,
                    scope.owner_id,
                    kind,
                    digest,
                    len(body),
                    nonce,
                    encrypted,
                    self._expires_at,
                    self._expires_at,
                ),
            ).fetchone()
            if row is None:
                raise SafeStop("RETENTION_EXPIRED")
        return artifact_id, digest

    def put_raw(self, scope: Scope, source: SourceInput) -> RawArtifact:
        ref, digest = self._put(
            scope,
            "raw",
            source.body,
            {
                "source_ref": source.source_ref,
                "locator_ref": source.locator_ref,
                "observed_at": source.observed_at,
            },
        )
        return RawArtifact(ref, source.source_ref, source.locator_ref, source.observed_at, digest, len(source.body))

    def put_summary(self, scope: Scope, content: bytes) -> tuple[str, str]:
        forbid_secrets(content)
        return self._put(scope, "summary", content, {})

    def _get(self, scope: Scope, ref: str, kind: str) -> tuple[bytes, dict[str, str], str]:
        self._scope(scope)
        with self._db.tx() as c:
            self._current(c)
            row = c.execute(
                "SELECT run_id, content_sha256, byte_length, nonce, ciphertext FROM agent.context_artifacts"
                " WHERE tenant_id=%s AND id::text=%s AND owner_user_id=%s AND task_id=%s"
                " AND kind=%s AND revoked_at IS NULL AND expires_at > clock_timestamp()",
                (scope.tenant_id, ref, scope.owner_id, scope.task_id, kind),
            ).fetchone()
            if row is None:
                raise SafeStop("ARTIFACT_UNAVAILABLE")
            try:
                decoded = json.loads(
                    self._cipher.decrypt(bytes(row["nonce"]), bytes(row["ciphertext"]), self._aad(ref, row["run_id"]))
                )
                body = base64.b64decode(decoded["body"], validate=True)
                metadata = decoded["metadata"]
                if not isinstance(metadata, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()
                ):
                    raise ValueError("invalid metadata")
            except (InvalidTag, ValueError, TypeError, KeyError, binascii.Error) as exc:
                raise SafeStop("SOURCE_MISMATCH") from exc
            digest = sha256(body).hexdigest()
            if digest != row["content_sha256"].strip() or len(body) != row["byte_length"]:
                raise SafeStop("SOURCE_MISMATCH")
            return body, metadata, digest

    def get_raw(self, scope: Scope, record: RawArtifact) -> bytes:
        body, meta, digest = self._get(scope, record.artifact_ref, "raw")
        if (
            digest != record.sha256
            or len(body) != record.byte_length
            or meta
            != {
                "source_ref": record.source_ref,
                "locator_ref": record.locator_ref,
                "observed_at": record.observed_at,
            }
        ):
            raise SafeStop("SOURCE_MISMATCH")
        return body

    def get_summary(self, scope: Scope, ref: str) -> bytes:
        body, _, _ = self._get(scope, ref, "summary")
        return body

    def read_range(self, scope: Scope, record: RawArtifact, start: int, end: int) -> bytes:
        body = self.get_raw(scope, record)
        if not 0 <= start < end <= len(body):
            raise SafeStop("INVALID_LOCATOR")
        return body[start:end]

    def revoke(self, scope: Scope, ref: str) -> None:
        self._scope(scope)
        with self._db.tx() as c:
            self._current(c)
            row = c.execute(
                "SELECT agent.revoke_context_artifact(%s,%s,%s,%s,%s) AS revoked",
                (scope.tenant_id, scope.owner_id, scope.task_id, scope.run_id, ref),
            ).fetchone()
            if row is None or not row["revoked"]:
                raise SafeStop("ARTIFACT_UNAVAILABLE")
