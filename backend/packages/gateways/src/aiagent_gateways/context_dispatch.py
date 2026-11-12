"""Model Gateway admission for an already committed Context HEAD.

The admission transaction serializes with Memory/Artifact revocation on the
scope epoch. An admitted request can be sent after a later revocation; no DB
lock is held during provider I/O. A unique dispatch row forbids blind retries.
All row access goes through claim-bound database functions.
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
from .context_keys import KeyRing
from .context_memory import PostgresMemoryRepository


class ContextAdmission:
    def __init__(self, db: Database, claim: control.Claim, key: bytes | KeyRing, head: int):
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
                or not m.get("model")
                or not m.get("recipient")
                or m.get("restoreIncarnation") != str(self.claim.restore_incarnation)
            ):
                raise RpcError("STALE_CONTEXT")
            # Check the external restore watermark before decrypting the frozen
            # payload, which may contain Memory text. No stale Memory plaintext is
            # loaded into the Gateway even transiently while reconciliation is pending.
            self.memory.epoch(self.scope)
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
        except SafeStop as exc:
            raise RpcError(exc.code) from None
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise RpcError("CONTEXT_UNAVAILABLE", type(exc).__name__) from None

    def lock_and_validate(self, c: psycopg.Connection[Row], run: Row, payload: str, model: str) -> None:
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
        # Preflight is advisory only. Re-read and row-lock the owner policy and
        # recipient consent in this same transaction as the unique dispatch row.
        # A user withdrawal that commits first denies admission; if admission locks
        # first, withdrawal waits until admission is durable and cannot retract it.
        policy = self.memory._call_tx(c, "context_policy_lock", self.scope)
        if (
            policy is None
            or not policy["enabled"]
            or policy["recipient"] != m["recipient"]
            or policy["model"] != m["model"]
        ):
            raise RpcError("CONTEXT_NOT_ELIGIBLE")
        consent = self.memory._call_tx(c, "memory_consent_lock", self.scope, str(m["recipient"]))
        if consent is None or not consent["memory_consent_lock"]:
            raise RpcError("MEMORY_CONSENT_REQUIRED")
        locked = self.memory._call_tx(c, "memory_epoch_lock", self.scope, int(m["sourceEpoch"]))
        if locked is None or not locked["memory_epoch_lock"]:
            raise RpcError("STALE_MEMORY")
        for entry in m["memory"]:
            current = self.memory._call_tx(
                c,
                "memory_row_current",
                self.scope,
                entry["id"],
                entry["revision"],
                m["recipient"],
                int(m["sourceEpoch"]),
            )
            if current is None or not current["memory_row_current"]:
                raise RpcError("STALE_MEMORY")
        for ref in [*m["rawRefs"], m["summaryRef"], m["payloadRef"]]:
            artifact = self.memory._call_tx(c, "context_artifact_live", self.scope, ref)
            if artifact is None or not artifact["context_artifact_live"]:
                raise RpcError("CONTEXT_UNAVAILABLE")

    def record(self, c: psycopg.Connection[Row], attempt_id: str) -> None:
        row = c.execute(
            "SELECT agent.context_dispatch_record(%s,%s,%s,%s,%s,%s,%s,%s) AS ok",
            (
                self.scope.tenant_id,
                self.scope.owner_id,
                self.scope.task_id,
                self.scope.run_id,
                self.claim.lease_epoch,
                self.claim.worker_ref,
                self.head,
                attempt_id,
            ),
        ).fetchone()
        if row is None or not row["ok"]:
            raise RpcError("CONTEXT_ALREADY_ADMITTED")

    def finish(self, attempt_id: str, status: str) -> None:
        # No new authorization is conferred here; failures leave ADMITTED as UNKNOWN.
        with self.db.tx() as c:
            self.finish_tx(c, attempt_id, status)

    def finish_tx(self, c: psycopg.Connection[Row], attempt_id: str, status: str) -> None:
        row = c.execute(
            "SELECT agent.context_dispatch_finish(%s,%s,%s,%s,%s,%s,%s,%s,%s) AS ok",
            (
                self.scope.tenant_id,
                self.scope.owner_id,
                self.scope.task_id,
                self.scope.run_id,
                self.claim.lease_epoch,
                self.claim.worker_ref,
                self.head,
                attempt_id,
                status,
            ),
        ).fetchone()
        if row is None or not row["ok"]:
            raise RpcError("CONTEXT_DISPATCH_STATE")
