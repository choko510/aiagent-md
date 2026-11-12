"""Model Gateway admission for an already committed Context HEAD.

The admission transaction serializes with Memory/Artifact revocation on the
scope epoch. An admitted request can be sent after a later revocation; no DB
lock is held during provider I/O. A unique dispatch row forbids blind retries.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import psycopg
from aiagent_context.models import SafeStop, Scope
from aiagent_core import control
from aiagent_core.db import Database, Row
from aiagent_domain.rpcwire import RpcError

from .context_artifacts import PostgresArtifactStore
from .context_head import PostgresHeadPort
from .context_memory import PostgresMemoryRepository


class ContextAdmission:
    def __init__(self, db: Database, claim: control.Claim, key: bytes, head: int):
        if head < 1:
            raise RpcError("STALE_CONTEXT")
        self.db, self.claim, self.head = db, claim, head
        self.scope = Scope(claim.tenant_id, claim.owner_user_id, claim.task_id, claim.run_id)
        # The deadline is only for writes. This object performs read-only artifact access.
        self.store = PostgresArtifactStore(db, claim, key, expires_at=datetime.now(UTC) + timedelta(days=1))
        self.memory = PostgresMemoryRepository(db, claim, self.store)
        self.manifest: dict[str, Any] = {}

    def load(self) -> str:
        try:
            with self.db.tx() as c:
                self.memory._bind(c, self.scope)
                run = control.check_current(c, self.claim, for_update=True)
                row = c.execute(
                    "SELECT checkpoint_json FROM agent.run_checkpoints WHERE tenant_id=%s AND run_id=%s"
                    " AND checkpoint_revision=%s AND entry_kind='summary'",
                    (self.scope.tenant_id, self.scope.run_id, self.head),
                ).fetchone()
                if row is None or run["head_revision"] != self.head:
                    raise RpcError("STALE_CONTEXT")
                self.manifest = dict(row["checkpoint_json"])
            m = self.manifest
            if (
                m.get("kind") != "context_manifest"
                or m.get("model") != "fake-gateway"
                or m.get("recipient") != "fake"
                or m.get("restoreIncarnation") != str(self.claim.restore_incarnation)
            ):
                raise RpcError("STALE_CONTEXT")
            payload = self.store.get_summary(self.scope, m["payloadRef"])
            summary = self.store.get_summary(self.scope, m["summaryRef"])
            if sha256(payload).hexdigest() != m["payloadDigest"] or sha256(summary).hexdigest() != m["summaryDigest"]:
                raise RpcError("SOURCE_MISMATCH")
            for ref in m["rawRefs"]:
                self.store._get(self.scope, ref, "raw")
            data = json.loads(payload)
            memory_data = data["memoryDataNotInstructions"]
            if len(memory_data) != len(m["memory"]):
                raise RpcError("STALE_MEMORY")
            for item, frozen in zip(memory_data, m["memory"], strict=True):
                record = self.memory.get(self.scope, frozen["id"])
                row = self.memory._row(self.scope, frozen["id"])
                if (
                    record is None
                    or row is None
                    or row["recipient"] != m["recipient"]
                    or record.revision != frozen["revision"]
                    or item.get("ref") != record.ref
                    or item.get("text") != record.text
                    or item.get("source_refs") != list(record.source_refs)
                    or item.get("source_epoch") != m["sourceEpoch"]
                    or item.get("record_revision") != record.revision
                    or item.get("trust_tier") != "MEMORY_DATA"
                ):
                    raise RpcError("STALE_MEMORY")
            return payload.decode("utf-8", "strict")
        except control.ControlError as exc:
            raise RpcError(exc.code) from None
        except (SafeStop, KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise RpcError("CONTEXT_UNAVAILABLE", type(exc).__name__) from None

    def lock_and_validate(self, c: psycopg.Connection[Row], run: Row, payload: str, model: str) -> None:
        self.memory._bind(c, self.scope)
        row = c.execute(
            "SELECT checkpoint_json FROM agent.run_checkpoints WHERE tenant_id=%s AND run_id=%s"
            " AND checkpoint_revision=%s AND entry_kind='summary'",
            (self.scope.tenant_id, self.scope.run_id, self.head),
        ).fetchone()
        m = self.manifest
        task = c.execute(
            "SELECT profile_code FROM agent.tasks WHERE tenant_id=%s AND id=%s",
            (self.scope.tenant_id, self.scope.task_id),
        ).fetchone()
        if (
            row is None
            or row["checkpoint_json"] != m
            or run["head_revision"] != self.head
            or task is None
            or m["policyVersion"] != PostgresHeadPort._policy(run, task["profile_code"])
            or model != m["model"]
            or sha256(payload.encode()).hexdigest() != m["payloadDigest"]
        ):
            raise RpcError("STALE_CONTEXT")
        locked = c.execute(
            "SELECT agent.lock_memory_epoch(%s,%s,%s,%s) AS ok",
            (self.scope.tenant_id, self.scope.owner_id, self.scope.task_id, int(m["sourceEpoch"])),
        ).fetchone()
        if locked is None or not locked["ok"]:
            raise RpcError("STALE_MEMORY")
        for entry in m["memory"]:
            current = c.execute(
                "SELECT id FROM agent.memory_records WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s"
                " AND id::text=%s AND revision=%s AND recipient=%s AND revoked_at IS NULL AND expires_at > clock_timestamp()"
                " AND agent.memory_artifact_usable(tenant_id,owner_user_id,task_id,source_artifact_id)"
                " AND agent.memory_artifact_usable(tenant_id,owner_user_id,task_id,projection_artifact_id)",
                (
                    self.scope.tenant_id,
                    self.scope.owner_id,
                    self.scope.task_id,
                    entry["id"],
                    entry["revision"],
                    m["recipient"],
                ),
            ).fetchone()
            if current is None:
                raise RpcError("STALE_MEMORY")
        for ref in [*m["rawRefs"], m["summaryRef"], m["payloadRef"]]:
            artifact = c.execute(
                "SELECT id FROM agent.context_artifacts WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s"
                " AND id::text=%s AND revoked_at IS NULL AND expires_at > clock_timestamp()",
                (self.scope.tenant_id, self.scope.owner_id, self.scope.task_id, ref),
            ).fetchone()
            if artifact is None:
                raise RpcError("CONTEXT_UNAVAILABLE")

    def record(self, c: psycopg.Connection[Row], attempt_id: str) -> None:
        row = c.execute(
            "INSERT INTO agent.context_dispatches(tenant_id,owner_user_id,task_id,run_id,head_revision,model_attempt_id,status_code)"
            " VALUES (%s,%s,%s,%s,%s,%s,'ADMITTED') ON CONFLICT DO NOTHING RETURNING head_revision",
            (self.scope.tenant_id, self.scope.owner_id, self.scope.task_id, self.scope.run_id, self.head, attempt_id),
        ).fetchone()
        if row is None:
            raise RpcError("CONTEXT_ALREADY_ADMITTED")

    def finish(self, attempt_id: str, status: str) -> None:
        # No new authorization is conferred here; failures leave ADMITTED as UNKNOWN.
        with self.db.tx() as c:
            from aiagent_core.memory_writer import _bind

            _bind(c, self.scope.tenant_id, self.scope.owner_id, self.scope.task_id)
            self.finish_tx(c, attempt_id, status)

    def finish_tx(self, c: psycopg.Connection[Row], attempt_id: str, status: str) -> None:
        updated = c.execute(
            "UPDATE agent.context_dispatches SET status_code=%s WHERE tenant_id=%s AND run_id=%s"
            " AND head_revision=%s AND model_attempt_id=%s AND status_code='ADMITTED'",
            (status, self.scope.tenant_id, self.scope.run_id, self.head, attempt_id),
        )
        if updated.rowcount != 1:
            raise RpcError("CONTEXT_DISPATCH_STATE")
