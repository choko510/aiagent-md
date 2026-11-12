"""User-originated artifact ingest for Memory (Model Gateway side).

The API never holds the artifact key. It forwards the authenticated user's plaintext
over an internal authenticated RPC; the Model Gateway encrypts it with the key ring
and stores it through the owner-scoped database function. The function validates the
authenticated owner/workspace and attaches the row to the task's active run, so a
caller cannot choose another owner's target.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import psycopg
from aiagent_context.models import SafeStop, forbid_secrets
from aiagent_core.db import Database
from aiagent_domain import uuid7
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .context_artifacts import artifact_aad
from .context_keys import KeyRing


class IngestError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class MemoryIngest:
    def __init__(self, db: Database, ring: KeyRing, *, max_bytes: int = 2_000_000):
        self.db, self.ring, self.max_bytes = db, ring, max_bytes

    def put_source(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        workspace_id: str,
        task_id: str,
        source_ref: str,
        locator_ref: str,
        observed_at: str,
        body: bytes,
        retention_days: int = 365,
    ) -> str:
        if not body or len(body) > self.max_bytes:
            raise IngestError("INVALID_SOURCE")
        forbid_secrets(body)
        if not 1 <= retention_days <= 3650:
            raise IngestError("INVALID_RETENTION")
        digest = sha256(body).hexdigest()
        artifact_id = str(uuid7())
        key_id = self.ring.active
        envelope = json.dumps(
            {
                "body": base64.b64encode(body).decode("ascii"),
                "metadata": {"source_ref": source_ref, "locator_ref": locator_ref, "observed_at": observed_at},
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        nonce = os.urandom(12)
        # AAD binds tenant, owner, task and the future active run, so a moved row cannot be decrypted.
        with self.db.tx() as c:
            run = c.execute(
                "SELECT t.active_run_id, t.id FROM agent.tasks t WHERE t.tenant_id=%s AND t.id=%s"
                " AND t.owner_user_id=%s AND t.workspace_id=%s"
                " AND agent.scope_active(t.tenant_id,t.owner_user_id,t.workspace_id)",
                (tenant_id, task_id, owner_id, workspace_id),
            ).fetchone()
            if run is None or run["active_run_id"] is None:
                raise IngestError("MEMORY_SCOPE_DENIED")
            run_id = str(run["active_run_id"])
        aad = artifact_aad(tenant_id, owner_id, task_id, run_id, artifact_id, key_id)
        encrypted = AESGCM(self.ring.keys[key_id]).encrypt(nonce, envelope, aad)
        expires_at = datetime.now(UTC) + timedelta(days=retention_days)
        try:
            with self.db.tx() as c:
                row = c.execute(
                    "SELECT agent.context_artifact_put_user(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS id",
                    (
                        tenant_id,
                        owner_id,
                        task_id,
                        run_id,
                        artifact_id,
                        "raw",
                        digest,
                        len(body),
                        nonce,
                        encrypted,
                        expires_at,
                        key_id,
                    ),
                ).fetchone()
        except psycopg.errors.InsufficientPrivilege as exc:
            raise IngestError("MEMORY_SCOPE_DENIED") from exc
        if row is None or row["id"] is None:
            raise IngestError("MEMORY_SCOPE_DENIED")
        return artifact_id


def ingest_handler(ingest: MemoryIngest) -> Any:
    """RPC handler body for `memory.artifact.put` (caller: api)."""
    from aiagent_domain.rpcwire import RpcError

    async def put(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            artifact_id = await asyncio.to_thread(
                ingest.put_source,
                tenant_id=str(body["tenantId"]),
                owner_id=str(body["ownerUserId"]),
                workspace_id=str(body["workspaceId"]),
                task_id=str(body["taskId"]),
                source_ref=str(body["sourceRef"]),
                locator_ref=str(body["locatorRef"]),
                observed_at=str(body["observedAt"]),
                body=str(body["content"]).encode("utf-8", "strict"),
                retention_days=int(body.get("retentionDays", 365)),
            )
        except IngestError as exc:
            raise RpcError(exc.code) from None
        except SafeStop as exc:
            raise RpcError(exc.code) from None
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise RpcError("BAD_REQUEST", type(exc).__name__) from None
        return {"artifactId": artifact_id}

    return put
