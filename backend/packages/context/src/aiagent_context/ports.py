"""Mock adapters. Production ownership, persistence, HEAD CAS and admission: AI-07."""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from typing import Protocol
from uuid import uuid4

from .models import ContextPlan, RawArtifact, SafeStop, Scope, SourceInput


class ArtifactStorePort(Protocol):
    def put_raw(self, scope: Scope, source: SourceInput) -> RawArtifact: ...
    def get_raw(self, scope: Scope, record: RawArtifact) -> bytes: ...
    def put_summary(self, scope: Scope, content: bytes) -> tuple[str, str]: ...


class HeadPort(Protocol):
    def assert_current(self, scope: Scope, expected_head: str, goal_revision: str,
                       lease_epoch: str, policy_version: str, memory_epoch: str) -> None: ...
    def publish(self, scope: Scope, expected_head: str, goal_revision: str,
                lease_epoch: str, policy_version: str, memory_epoch: str,
                plan_digest: str) -> str: ...
    def check_dispatch(self, plan: ContextPlan, lease_epoch: str) -> None: ...


class MockArtifactStore:
    """Complete immutable raw bytes in memory, tenant/owner ACL, digest-on-every-read."""

    def __init__(self) -> None:
        self._raw: dict[str, tuple[Scope, RawArtifact, bytes]] = {}
        self._summary: dict[str, tuple[Scope, bytes]] = {}
        self.fail_next_write = False

    def _write_allowed(self) -> None:
        if self.fail_next_write:
            self.fail_next_write = False
            raise SafeStop("OFFLOAD_FAILED")

    def put_raw(self, scope: Scope, source: SourceInput) -> RawArtifact:
        self._write_allowed()
        ref = "mock-artifact:" + str(uuid4())
        record = RawArtifact(ref, source.source_ref, source.locator_ref, source.observed_at,
                             sha256(source.body).hexdigest(), len(source.body))
        self._raw[ref] = (scope, record, bytes(source.body))
        return record

    def get_raw(self, scope: Scope, record: RawArtifact) -> bytes:
        saved = self._raw.get(record.artifact_ref)
        if saved is None:
            raise SafeStop("ARTIFACT_UNAVAILABLE")
        actual_scope, metadata, content = saved
        if actual_scope != scope:
            raise SafeStop("ARTIFACT_UNAVAILABLE")
        if metadata != record or len(content) != record.byte_length or sha256(content).hexdigest() != record.sha256:
            raise SafeStop("SOURCE_MISMATCH")
        return content

    def put_summary(self, scope: Scope, content: bytes) -> tuple[str, str]:
        self._write_allowed()
        ref = "mock-summary:" + str(uuid4())
        self._summary[ref] = (scope, bytes(content))
        return ref, sha256(content).hexdigest()

    def get_summary(self, scope: Scope, ref: str) -> bytes:
        item = self._summary.get(ref)
        if item is None or item[0] != scope:
            raise SafeStop("ARTIFACT_UNAVAILABLE")
        return bytes(item[1])

    def drop_raw(self, ref: str) -> None:
        self._raw.pop(ref, None)

    def tamper_raw(self, ref: str, content: bytes) -> None:
        scope, meta, _ = self._raw[ref]
        self._raw[ref] = (scope, meta, content)


class MockHeadPort:
    """Simulates protected HEAD CAS and latest goal/policy/memory/lease; not a DB."""

    def __init__(self, scope: Scope, *, head: str, goal_revision: str,
                 lease_epoch: str, policy_version: str, memory_epoch: str):
        self.scope = scope
        self.head = head
        self.goal_revision = goal_revision
        self.lease_epoch = lease_epoch
        self.policy_version = policy_version
        self.memory_epoch = memory_epoch
        self.fail_before_cas = False
        self.published: list[tuple[str, str]] = []

    def assert_current(self, scope: Scope, expected_head: str, goal_revision: str,
                       lease_epoch: str, policy_version: str, memory_epoch: str) -> None:
        if scope != self.scope or self.head != expected_head or self.goal_revision != goal_revision \
           or self.lease_epoch != lease_epoch or self.policy_version != policy_version \
           or self.memory_epoch != memory_epoch:
            raise SafeStop("STALE_CONTEXT")

    def publish(self, scope: Scope, expected_head: str, goal_revision: str,
                lease_epoch: str, policy_version: str, memory_epoch: str,
                plan_digest: str) -> str:
        self.assert_current(scope, expected_head, goal_revision, lease_epoch, policy_version, memory_epoch)
        if self.fail_before_cas:
            raise SafeStop("SIMULATED_KILL_BEFORE_CAS")
        new_head = "mock-head:" + plan_digest
        self.head = new_head
        self.published.append((expected_head, new_head))
        return new_head

    def check_dispatch(self, plan: ContextPlan, lease_epoch: str) -> None:
        self.assert_current(plan.scope, plan.committed_head, plan.goal_revision, lease_epoch,
                            plan.policy_version, plan.memory_source_epoch)
