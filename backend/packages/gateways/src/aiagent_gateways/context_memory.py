"""PostgreSQL-backed MemoryRetriever ports, still unavailable as an agent Tool.

The supplied claim is authenticated by the existing gateway boundary. Candidate
IDs are not authority: every row, consent and encrypted source is checked again
through claim-bound SECURITY DEFINER functions. Direct table access is not
granted to the Model Gateway role.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from aiagent_context.models import SafeStop, Scope
from aiagent_core import control
from aiagent_core.db import Database, Row
from aiagent_memory.retrieval import MemoryRecord, MemoryRetriever, Retrieval

from .context_artifacts import PostgresArtifactStore


class PostgresMemoryRepository:
    def __init__(self, db: Database, claim: control.Claim, store: PostgresArtifactStore):
        if claim != store._claim or db is not store._db:
            raise ValueError("memory and artifact authority must match")
        self.db, self.claim, self.store = db, claim, store

    def _args(self, scope: Scope) -> tuple[str, str, str, str, int, str]:
        claim = self.claim
        if (scope.tenant_id, scope.owner_id, scope.task_id, scope.run_id) != (
            claim.tenant_id,
            claim.owner_user_id,
            claim.task_id,
            claim.run_id,
        ):
            raise SafeStop("ARTIFACT_UNAVAILABLE")
        return (claim.tenant_id, claim.owner_user_id, claim.task_id, claim.run_id, claim.lease_epoch, claim.worker_ref)

    def _call_tx(self, c: psycopg.Connection[Row], name: str, scope: Scope, *args: Any) -> Row | None:
        try:
            return c.execute(
                f"SELECT * FROM agent.{name}(%s,%s,%s,%s,%s,%s{',%s' * len(args)})",
                (*self._args(scope), *args),
            ).fetchone()
        except psycopg.errors.InsufficientPrivilege as exc:
            text = str(exc)
            if "memory policy unavailable" in text:
                raise SafeStop("MEMORY_POLICY_UNAVAILABLE") from exc
            if "context claim denied" in text:
                raise SafeStop("MEMORY_RESTORE_RECONCILIATION_REQUIRED") from exc
            raise SafeStop("STALE_CONTEXT") from exc

    def _call(self, name: str, scope: Scope, *args: Any) -> Row | None:
        with self.db.tx() as c:
            return self._call_tx(c, name, scope, *args)

    def epoch(self, scope: Scope) -> str:
        row = self._call("memory_epoch", scope)
        if row is None:
            raise SafeStop("MEMORY_POLICY_UNAVAILABLE")
        return str(row["memory_epoch"])

    def _row(self, scope: Scope, ref: str) -> dict[str, Any] | None:
        row = self._call("memory_row", scope, ref)
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
        try:
            with self.db.tx() as c:
                rows = c.execute(
                    "SELECT id FROM agent.memory_live_rows(%s,%s,%s,%s,%s,%s) LIMIT %s",
                    (*self._args(scope), min(limit * 4, 320)),
                ).fetchall()
        except psycopg.errors.InsufficientPrivilege as exc:
            raise SafeStop("MEMORY_RESTORE_RECONCILIATION_REQUIRED") from exc
        found: list[str] = []
        for row in rows:
            record = self.get(scope, row["id"])
            if record is not None and query.casefold() in record.text.casefold():
                found.append(record.ref)
                if len(found) >= limit:
                    break
        return tuple(found)

    def retention_ok(self, scope: Scope, ref: str, revision: int, observed_at: str) -> bool:
        row = self._row(scope, ref)
        if row is None or row["revision"] != revision:
            return False
        try:
            return datetime.fromisoformat(row["expires_at"].isoformat()) > datetime.fromisoformat(observed_at)
        except ValueError:
            return False

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
        for ref in source_refs:
            row = self.repository._call("memory_source_usable", scope, ref)
            if row is None or not row["memory_source_usable"]:
                return False
        return True
