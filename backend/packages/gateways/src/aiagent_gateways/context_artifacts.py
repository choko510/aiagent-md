"""Encrypted PostgreSQL Context artifacts, internal and not connected to model dispatch.

Ciphertext access is a SECURITY DEFINER function that re-validates the Run claim
(tenant/owner/task/run/lease/worker/restore) inside the database. The Model
Gateway role has no direct table privilege, so a client-set scope cannot widen
access. The key ring supplies the key for the artifact's stored key id; a missing
key fails closed instead of silently using another key.
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
from aiagent_core.db import Database
from aiagent_domain import uuid7
from cryptography.exceptions import InvalidTag

from .context_keys import LEGACY_KEY_ID, KeyRing


def artifact_aad(tenant_id: str, owner_id: str, task_id: str, run_id: str, ref: str, key_id: str) -> bytes:
    """AAD binds ciphertext to scope, the writing run and the key id.

    Rows written before key ids existed (legacy single-key deployment) keep the
    historic five-field AAD so they remain decryptable with the legacy key.
    """
    fields = [tenant_id, owner_id, task_id, run_id, ref]
    if key_id != LEGACY_KEY_ID:
        fields.append(key_id)
    return json.dumps(fields, separators=(",", ":")).encode()


class PostgresArtifactStore:
    """Internal ArtifactStorePort; scope derives from a validated claim, never from model text."""

    def __init__(self, db: Database, claim: control.Claim, key: bytes | KeyRing, *, expires_at: datetime) -> None:
        ring = key if isinstance(key, KeyRing) else KeyRing.single(key)
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("timezone-aware expiry is required")
        self._db = db
        self._claim = claim
        self._ring = ring
        self._expires_at = expires_at

    @property
    def key_ring(self) -> KeyRing:
        return self._ring

    def _scope(self, scope: Scope) -> None:
        claim = self._claim
        if (scope.tenant_id, scope.owner_id, scope.task_id, scope.run_id) != (
            claim.tenant_id,
            claim.owner_user_id,
            claim.task_id,
            claim.run_id,
        ):
            raise SafeStop("ARTIFACT_UNAVAILABLE")

    def _claim_args(self) -> tuple[str, str, str, str, int, str]:
        c = self._claim
        return (c.tenant_id, c.owner_user_id, c.task_id, c.run_id, c.lease_epoch, c.worker_ref)

    def _aad(self, ref: str, source_run_id: str, key_id: str) -> bytes:
        c = self._claim
        return artifact_aad(c.tenant_id, c.owner_user_id, c.task_id, source_run_id, ref, key_id)

    def _put(self, scope: Scope, kind: str, body: bytes, metadata: dict[str, str]) -> tuple[str, str]:
        self._scope(scope)
        if not body or len(body) > 2_000_000:
            raise SafeStop("INVALID_SOURCE")
        artifact_id = str(uuid7())
        digest = sha256(body).hexdigest()
        key_id = self._ring.active
        envelope = json.dumps(
            {"body": base64.b64encode(body).decode("ascii"), "metadata": metadata},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        nonce = os.urandom(12)
        encrypted = self._ring.cipher(key_id).encrypt(nonce, envelope, self._aad(artifact_id, scope.run_id, key_id))
        try:
            with self._db.tx() as c:
                row = c.execute(
                    "SELECT agent.context_artifact_put(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS id",
                    (
                        *self._claim_args(),
                        artifact_id,
                        kind,
                        digest,
                        len(body),
                        nonce,
                        encrypted,
                        self._expires_at,
                        key_id,
                    ),
                ).fetchone()
        except psycopg.errors.InsufficientPrivilege as exc:
            raise SafeStop("STALE_CONTEXT") from exc
        if row is None or row["id"] is None:
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
        try:
            with self._db.tx() as c:
                row = c.execute(
                    "SELECT * FROM agent.context_artifact_fetch(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (*self._claim_args(), ref, kind),
                ).fetchone()
        except psycopg.errors.InsufficientPrivilege as exc:
            raise SafeStop("STALE_CONTEXT") from exc
        if row is None:
            raise SafeStop("ARTIFACT_UNAVAILABLE")
        key_id = str(row["key_id"])
        try:
            cipher = self._ring.cipher(key_id)
        except KeyError as exc:
            raise SafeStop("KEY_UNAVAILABLE") from exc
        try:
            decoded = json.loads(
                cipher.decrypt(
                    bytes(row["nonce"]), bytes(row["ciphertext"]), self._aad(ref, str(row["run_id"]), key_id)
                )
            )
            body = base64.b64decode(decoded["body"], validate=True)
            metadata = decoded["metadata"]
            if not isinstance(metadata, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()
            ):
                raise ValueError("invalid metadata")
        except InvalidTag as exc:
            raise SafeStop("SOURCE_MISMATCH") from exc
        except (ValueError, TypeError, KeyError, binascii.Error) as exc:
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
        try:
            with self._db.tx() as c:
                row = c.execute(
                    "SELECT agent.context_artifact_revoke(%s,%s,%s,%s,%s,%s,%s) AS revoked", (*self._claim_args(), ref)
                ).fetchone()
        except psycopg.errors.InsufficientPrivilege as exc:
            raise SafeStop("STALE_CONTEXT") from exc
        if row is None or not row["revoked"]:
            raise SafeStop("ARTIFACT_UNAVAILABLE")

    def present(self, scope: Scope, ref: str, kind: str, digest: str) -> bool:
        """Metadata-only existence/digest check through the claim-bound function."""
        self._scope(scope)
        try:
            with self._db.tx() as c:
                row = c.execute(
                    "SELECT agent.context_artifact_present(%s,%s,%s,%s,%s,%s,%s,%s,%s) AS ok",
                    (*self._claim_args(), ref, kind, digest),
                ).fetchone()
        except psycopg.errors.InsufficientPrivilege as exc:
            raise SafeStop("STALE_CONTEXT") from exc
        return bool(row and row["ok"])
