"""PostgreSQL-backed MemoryRetriever ports, still unavailable as an agent Tool.

The supplied claim is authenticated by the existing gateway boundary. Candidate
IDs are not authority: every row, consent and encrypted source is checked again.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from aiagent_context.models import SafeStop, Scope
from aiagent_core import control
from aiagent_core.db import Database, Row
from aiagent_core.memory_writer import _bind
from aiagent_memory.retrieval import MemoryRecord, MemoryRetriever, Retrieval

from .context_artifacts import PostgresArtifactStore


class PostgresMemoryRepository:
    def __init__(self, db: Database, claim: control.Claim, store: PostgresArtifactStore):
        if claim != store._claim or db is not store._db:
            raise ValueError("memory and artifact authority must match")
        self.db, self.claim, self.store = db, claim, store

    def _bind(self, c: psycopg.Connection[Row], scope: Scope) -> None:
        self.store._scope(scope)
        self.store._current(c)
        _bind(c, scope.tenant_id, scope.owner_id, scope.task_id)

    def epoch(self, scope: Scope) -> str:
        with self.db.tx() as c:
            self._bind(c, scope)
            row = c.execute(
                "SELECT source_epoch FROM agent.memory_scope_epochs WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s",
                (scope.tenant_id, scope.owner_id, scope.task_id),
            ).fetchone()
            if row is None:
                raise SafeStop("MEMORY_POLICY_UNAVAILABLE")
            return str(row["source_epoch"])

    def _row(self, scope: Scope, ref: str) -> dict[str, Any] | None:
        with self.db.tx() as c:
            self._bind(c, scope)
            row = c.execute(
                "SELECT id,source_artifact_id,projection_artifact_id,recipient,consent_basis,observed_at,expires_at,revision"
                " FROM agent.memory_records WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s AND id::text=%s"
                " AND revoked_at IS NULL AND expires_at > clock_timestamp()"
                " AND agent.memory_artifact_usable(tenant_id,owner_user_id,task_id,source_artifact_id)"
                " AND agent.memory_artifact_usable(tenant_id,owner_user_id,task_id,projection_artifact_id)",
                (scope.tenant_id, scope.owner_id, scope.task_id, ref),
            ).fetchone()
            return dict(row) if row is not None else None

    def get(self, scope: Scope, ref: str) -> MemoryRecord | None:
        row = self._row(scope, ref)
        if row is None:
            return None
        try:
            data, _, _ = self.store._get(scope, row["projection_artifact_id"], "raw")
            text = data.decode("utf-8", "strict")
        except UnicodeError as exc:
            raise SafeStop("MEMORY_INVALID_ENCODING") from exc
        return MemoryRecord(
            ref=row["id"],
            tenant_id=scope.tenant_id,
            owner_id=scope.owner_id,
            scope_task_id=scope.task_id,
            text=text,
            source_refs=(row["source_artifact_id"],),
            observed_at=row["observed_at"].isoformat(),
            expires_at=row["expires_at"].isoformat(),
            consent_basis=row["consent_basis"],
            revision=row["revision"],
        )

    def search(self, scope: Scope, query: str, limit: int) -> tuple[str, ...]:
        # Bounded candidate discovery. Each candidate is fully checked and decrypted again.
        with self.db.tx() as c:
            self._bind(c, scope)
            rows = c.execute(
                "SELECT id FROM agent.memory_records WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s"
                " AND revoked_at IS NULL AND expires_at > clock_timestamp() ORDER BY created_at DESC,id LIMIT %s",
                (scope.tenant_id, scope.owner_id, scope.task_id, min(limit * 4, 320)),
            ).fetchall()
        found: list[str] = []
        for row in rows:
            record = self.get(scope, row["id"])
            if record is not None and query.casefold() in record.text.casefold():
                found.append(record.ref)
                if len(found) >= limit:
                    break
        return tuple(found)

    def retrieval(self, scope: Scope, *, query: str, recipient: str) -> Retrieval:
        return MemoryRetriever(self, consent=_Consent(self), retention=_Retention(self), sources=_Sources(self)).read(
            scope, query=query, recipient=recipient
        )


class _Consent:
    def __init__(self, repository: PostgresMemoryRepository):
        self.repository = repository

    def permits(self, scope: Scope, record: MemoryRecord, recipient: str) -> bool:
        row = self.repository._row(scope, record.ref)
        return bool(row and row["recipient"] == recipient and row["revision"] == record.revision)


class _Retention:
    def __init__(self, repository: PostgresMemoryRepository):
        self.repository = repository

    def permits(self, scope: Scope, record: MemoryRecord, now: datetime) -> bool:
        row = self.repository._row(scope, record.ref)
        return bool(row and row["revision"] == record.revision and row["expires_at"] > now)


class _Sources:
    def __init__(self, repository: PostgresMemoryRepository):
        self.repository = repository

    def permits(self, scope: Scope, source_refs: tuple[str, ...]) -> bool:
        if not source_refs:
            return False
        with self.repository.db.tx() as c:
            self.repository._bind(c, scope)
            for ref in source_refs:
                row = c.execute(
                    "SELECT agent.memory_artifact_usable(%s,%s,%s,%s) AS ok",
                    (scope.tenant_id, scope.owner_id, scope.task_id, ref),
                ).fetchone()
                if row is None or not row["ok"]:
                    return False
            return True
