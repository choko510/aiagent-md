"""Read-only Memory projection. Search/index matches are NEVER authorization evidence.

All guards are separate explicit ports and deny on absent/failed policy, consent,
retention or source verification. No auto-learning, mutation or cross-conversation
search exists here. Caller-supplied Scope must originate at authenticated server.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from aiagent_context.models import MemoryProjection, SafeStop, Scope, forbid_secrets, utc_instant


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    ref: str
    tenant_id: str
    owner_id: str
    scope_task_id: str | None
    text: str
    source_refs: tuple[str, ...]
    observed_at: str
    expires_at: str
    status: str = "active"
    consent_basis: str = "user_explicit"
    revision: int = 1
    sensitivity: str = "normal"

    def __post_init__(self) -> None:
        if not self.ref or not self.tenant_id or not self.owner_id or not self.source_refs or self.revision < 1:
            raise SafeStop("INVALID_MEMORY_RECORD")
        utc_instant(self.observed_at)
        utc_instant(self.expires_at)
        forbid_secrets(self.text)
        for source_ref in self.source_refs:
            forbid_secrets(source_ref)


class RepositoryPort(Protocol):
    def search(self, scope: Scope, query: str, limit: int) -> tuple[str, ...]: ...
    def get(self, scope: Scope, ref: str) -> MemoryRecord | None: ...
    def epoch(self, scope: Scope) -> str: ...


class ConsentPort(Protocol):
    def permits(self, scope: Scope, record: MemoryRecord, recipient: str) -> bool: ...


class RetentionPort(Protocol):
    def permits(self, scope: Scope, record: MemoryRecord, now: datetime) -> bool: ...


class SourcePort(Protocol):
    def permits(self, scope: Scope, source_refs: tuple[str, ...]) -> bool: ...


@dataclass(frozen=True, slots=True)
class Retrieval:
    projections: tuple[MemoryProjection, ...]
    source_epoch: str
    recipient: str


class MemoryRetriever:
    def __init__(
        self, repository: RepositoryPort, *, consent: ConsentPort, retention: RetentionPort, sources: SourcePort
    ):
        self.repository = repository
        self.consent = consent
        self.retention = retention
        self.sources = sources

    def read(
        self,
        scope: Scope,
        *,
        query: str,
        recipient: str,
        now: datetime | None = None,
        max_items: int = 8,
        max_bytes: int = 4000,
    ) -> Retrieval:
        forbid_secrets(query)
        if not query or len(query) > 2000 or not recipient or not 1 <= max_items <= 20 or not 0 < max_bytes <= 20000:
            raise SafeStop("INVALID_MEMORY_QUERY")
        if now is None:
            now = datetime.now(UTC)
        if now.tzinfo is None or now.utcoffset() is None:
            raise SafeStop("INVALID_TIMESTAMP")
        start_epoch = self.repository.epoch(scope)
        if not start_epoch:
            raise SafeStop("MEMORY_POLICY_UNAVAILABLE")
        refs = self.repository.search(scope, query, max_items * 4)
        selected: list[MemoryProjection] = []
        used = 0
        # Unknown/other-tenant ID returned by malicious index cannot escape.
        for ref in dict.fromkeys(refs):
            record = self.repository.get(scope, ref)
            if record is None or record.tenant_id != scope.tenant_id or record.owner_id != scope.owner_id:
                continue
            if record.scope_task_id is not None and record.scope_task_id != scope.task_id:
                continue
            if record.status != "active" or record.consent_basis not in {
                "user_explicit",
                "approved_setting",
                "reviewed_import",
            }:
                continue
            if utc_instant(record.expires_at) <= now:
                continue
            # Fail closed if one of the independent ports is unavailable/raises.
            if not (
                self.consent.permits(scope, record, recipient)
                and self.retention.permits(scope, record, now)
                and self.sources.permits(scope, record.source_refs)
            ):
                continue
            encoded_length = len(record.text.encode("utf-8"))
            if used + encoded_length > max_bytes:
                continue
            selected.append(
                MemoryProjection(
                    record.ref, record.text, record.source_refs, start_epoch, record_revision=record.revision
                )
            )
            used += encoded_length
            if len(selected) >= max_items:
                break
        if self.repository.epoch(scope) != start_epoch:
            raise SafeStop("STALE_MEMORY")
        return Retrieval(tuple(selected), start_epoch, recipient)


class MockMemoryRepository:
    """Fixed fixture store; ID hits can be deliberately poisoned to test reauthorization."""

    def __init__(self, records: tuple[MemoryRecord, ...] = ()):
        self.records = {r.ref: r for r in records}
        self.epoch_value = "epoch-1"
        self.index_override: tuple[str, ...] | None = None
        self.after_search = None

    def epoch(self, scope: Scope) -> str:
        return self.epoch_value

    def search(self, scope: Scope, query: str, limit: int) -> tuple[str, ...]:
        hits = (
            self.index_override
            if self.index_override is not None
            else tuple(r.ref for r in self.records.values() if query.lower() in r.text.lower())
        )
        if self.after_search:
            self.after_search(self)
        return hits[:limit]

    def get(self, scope: Scope, ref: str) -> MemoryRecord | None:
        return self.records.get(ref)  # intentionally untrusted: retriever must recheck


class MockConsent:
    def __init__(self, allowed: bool = False):
        self.allowed = allowed

    def permits(self, scope: Scope, record: MemoryRecord, recipient: str) -> bool:
        return self.allowed


class MockRetention:
    def __init__(self, allowed: bool = False):
        self.allowed = allowed

    def permits(self, scope: Scope, record: MemoryRecord, now: datetime) -> bool:
        return self.allowed


class MockSources:
    def __init__(self, allowed: set[str] | None = None):
        self.allowed = allowed or set()

    def permits(self, scope: Scope, source_refs: tuple[str, ...]) -> bool:
        return bool(source_refs) and all(ref in self.allowed for ref in source_refs)
