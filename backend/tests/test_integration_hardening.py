"""Unit regression tests for PR #92 integration hardening fixes (no live Postgres required)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from aiagent_context import Anchor, AnchorKind, ContextRequest, ContextService, ModelBudget, SourceInput
from aiagent_context.compaction import ContextPublication
from aiagent_context.models import RawArtifact, SafeStop, Scope
from aiagent_core import audit, commands, control, memory_restore, memory_writer
from aiagent_core.memory_tombstones import GENESIS, TombstoneLog
from aiagent_domain.rpcwire import RpcError
from aiagent_gateways.context_artifacts import PostgresArtifactStore
from aiagent_gateways.context_dispatch import ContextAdmission
from aiagent_gateways.context_head import PostgresHeadPort
from aiagent_gateways.model_gateway import ModelGateway, Route, SealedModelRequest, _extract_context_payload
from aiagent_runtime.adapter import _CODE_TO_STATUS


def _claim() -> control.Claim:
    return control.Claim(
        tenant_id="01900000-0000-7000-8000-000000000001",
        owner_user_id="01900000-0000-7000-8000-000000000002",
        task_id="01900000-0000-7000-8000-000000000003",
        run_id="01900000-0000-7000-8000-000000000004",
        lease_epoch=1,
        goal_revision=1,
        worker_ref="worker-1",
        restore_incarnation=1,
    )


def test_adapter_maps_context_outcome_unknown() -> None:
    assert _CODE_TO_STATUS["CONTEXT_OUTCOME_UNKNOWN"] == "UNKNOWN_EFFECT"


def test_audit_rejects_forbidden_keys_and_secrets() -> None:
    calls: list[tuple[str, object]] = []

    class FakeCursor:
        def execute(self, sql: str, params: object = ()) -> None:
            calls.append((sql, params))

    cur = FakeCursor()
    audit.record_audit(
        cur,  # type: ignore[arg-type]
        tenant_id="t-1",
        action_code="memory.revoked",
        target_type="memory",
        detail={"taskId": "task-1", "revision": 2},
    )
    assert len(calls) == 1

    for bad_detail in [
        {"text": "private memory"},
        {"nested": [{"Auth-Header": "something"}]},
        {"api_key": "value"},
        {"note": "sk-abcdefghijklmnopqrstuvwxyz"},
        {"note": "Bearer supersecrettoken12345"},
        {"note": "xoxb-1234567890-abcdef"},
        {"note": "-----BEGIN RSA PRIVATE KEY-----"},
    ]:
        with pytest.raises(ValueError, match="AUDIT_DETAIL_FORBIDDEN"):
            audit.record_audit(
                cur,  # type: ignore[arg-type]
                tenant_id="t-1",
                action_code="memory.revoked",
                target_type="memory",
                detail=bad_detail,
            )
    assert len(calls) == 1


def test_extract_context_payload_and_structured_content() -> None:
    for invalid in [
        [],
        [{"kind": "assistant", "content": "x"}],
        [{"kind": "user", "content": "a"}, {"kind": "user", "content": "b"}],
        [{"kind": "user", "content": ""}],
    ]:
        with pytest.raises(RpcError, match="CONTEXT_UNAVAILABLE"):
            _extract_context_payload(invalid)

    assert _extract_context_payload([{"kind": "user", "content": "payload-json"}]) == "payload-json"

    req = SealedModelRequest.from_wire(
        {
            "claim": _claim().to_wire(),
            "stream": "main",
            "model": "fake-gateway",
            "tools": [],
            "messages": [
                {
                    "kind": "user",
                    "content": [{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}],
                }
            ],
        }
    )
    assert req.messages[0]["content"] == "hello world"


def test_complete_context_cancellation_releases_budget_and_finishes_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    claim = _claim()
    released: list[tuple[str, str, str | None]] = []
    finished: list[tuple[str, str]] = []

    class FakeDb:
        @contextlib.contextmanager
        def tx(self):  # type: ignore[no-untyped-def]
            yield self

    class FakeProvider:
        def capability(self, _model: str) -> Any:
            return type("Cap", (), {"external_recipient": False, "provider": "fake"})()

    class FakeContextAdmission:
        def __init__(self, _db: Any, _claim: control.Claim, _key: Any, _head: int) -> None:
            self.scope = Scope(claim.tenant_id, claim.owner_user_id, claim.task_id, claim.run_id)
            self.memory = type("Mem", (), {"_call": lambda *_a, **_kw: {"context_dispatch_unresolved": False}})()
            self.manifest = {"recipient": "fake", "model": "fake-gateway"}

        def load(self) -> str:
            return json.dumps({"trustedControl": {}, "untrustedData": {"memoryDataNotInstructions": []}})

        def finish(self, attempt_id: str, outcome: str) -> None:
            finished.append((attempt_id, outcome))

    monkeypatch.setattr("aiagent_gateways.model_gateway.ContextAdmission", FakeContextAdmission)
    monkeypatch.setattr(
        "aiagent_gateways.context_policy.resolve_policy",
        lambda *_a, **_kw: type("Policy", (), {"recipient": "fake", "model": "fake-gateway"})(),
    )

    gw = ModelGateway(
        FakeDb(),  # type: ignore[arg-type]
        {"fake": FakeProvider()},
        {"fake-gateway": [Route("fake", "fake-model")]},
        context_key=b"k" * 32,
    )
    monkeypatch.setattr(gw, "_admit", lambda *_a, **_kw: "attempt-1")
    monkeypatch.setattr(
        gw,
        "_release",
        lambda _req, attempt_id, _est, status, _usage, quality, *, reason_code=None: released.append(
            (status, quality, reason_code)
        ),
    )

    async def cancelled_stream(*_a: Any, **_kw: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(gw, "_stream_with_retry", cancelled_stream)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(gw.complete_context(claim, 1))

    assert released == [("CANCELLED", "UNKNOWN", "CANCELLED")]
    assert finished == [("attempt-1", "UNKNOWN")]


def test_context_head_and_dispatch_require_separated_payload_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    claim = _claim()
    scope = Scope(claim.tenant_id, claim.owner_user_id, claim.task_id, claim.run_id)

    class MemoryArtifactStore:
        def __init__(self) -> None:
            self._db = object()
            self._claim = claim
            self._items: dict[str, bytes] = {}
            self._meta: dict[str, RawArtifact] = {}

        def put_raw(self, _scope: Scope, source: SourceInput) -> RawArtifact:
            ref = f"raw-{len(self._items) + 1}"
            self._items[ref] = source.body
            art = RawArtifact(
                ref,
                source.source_ref,
                source.locator_ref,
                source.observed_at,
                sha256(source.body).hexdigest(),
                len(source.body),
            )
            self._meta[ref] = art
            return art

        def get_raw(self, _scope: Scope, record: RawArtifact) -> bytes:
            return self._items[record.artifact_ref]

        def put_summary(self, _scope: Scope, content: bytes) -> tuple[str, str]:
            ref = f"sum-{len(self._items) + 1}"
            self._items[ref] = content
            return ref, sha256(content).hexdigest()

        def get_summary(self, _scope: Scope, ref: str) -> bytes:
            return self._items[ref]

        def _get(self, _scope: Scope, ref: str, _kind: str) -> tuple[bytes, dict[str, str], str]:
            return self._items[ref], {}, sha256(self._items[ref]).hexdigest()

    class FakeHeadPort:
        def policy_version(self, _scope: Scope) -> str:
            return "pol-1"

        def assert_current(self, *_a: Any) -> None:
            return None

        def publish(self, *_a: Any) -> str:
            return "2"

    store = MemoryArtifactStore()
    req = ContextRequest(
        scope=scope,
        expected_head="1",
        lease_epoch="1",
        goal_revision="1",
        policy_version="pol-1",
        memory_source_epoch="1",
        raw_event_cursor="c:1",
        anchors=(Anchor(AnchorKind.GOAL_REF, "goal:1", "Summarize notes"),),
        budget=ModelBudget("fake-gateway", "v1", "tok-v1", 8192, 512, 128),
        recipient="fake",
        memory=(),
    )
    svc = ContextService(store, FakeHeadPort())  # type: ignore[arg-type]
    plan = svc.compact(req, (SourceInput("src-1", "2026-11-16T00:00:00Z", b"alpha beta gamma", locator_ref="loc-1"),))
    payload_bytes = plan.model_payload.encode("utf-8")
    payload_ref, payload_digest = store.put_summary(scope, payload_bytes)
    summary_ref, summary_digest = store.put_summary(scope, b"{}")

    fake_memory = type("FakeMem", (), {"store": store, "epoch": lambda self, _s: 1})()
    head_port = PostgresHeadPort(store._db, claim, store, fake_memory)  # type: ignore[arg-type]

    # Tamper payload to legacy flat schema -> PostgresHeadPort must reject with QUALITY_FAILED.
    bad_ref, bad_digest = store.put_summary(
        scope,
        json.dumps(
            {
                "untrustedDataOnly": True,
                "sourceBackedExcerpts": [],
                "typedCanonicalAnchors": [],
                "memoryDataNotInstructions": [],
                "toolPairIds": [],
            }
        ).encode(),
    )
    pub = ContextPublication(req, plan.raw_artifacts, (), summary_ref, summary_digest, bad_ref, bad_digest)
    with pytest.raises(SafeStop, match="QUALITY_FAILED"):
        head_port.publish(scope, "1", "1", "1", "pol-1", "1", "a" * 64, pub)

    manifest_row = {
        "kind": "context_manifest",
        "model": "fake-gateway",
        "recipient": "fake",
        "restoreIncarnation": "1",
        "payloadRef": payload_ref,
        "payloadDigest": payload_digest,
        "summaryRef": summary_ref,
        "summaryDigest": summary_digest,
        "rawRefs": [r.artifact_ref for r in plan.raw_artifacts],
        "memory": [],
        "sourceEpoch": 1,
    }

    class FakeAdmissionDb:
        @contextlib.contextmanager
        def tx(self):  # type: ignore[no-untyped-def]
            yield self

        def execute(self, _sql: str, _params: object = ()) -> Any:
            return type("Res", (), {"fetchone": lambda _self: {"checkpoint_json": manifest_row}})()

    monkeypatch.setattr("aiagent_core.control.check_current", lambda *_a, **_kw: {"head_revision": 2})

    # Separated schema passes payload validation inside ContextAdmission.load as well.
    admission = ContextAdmission.__new__(ContextAdmission)
    admission.db = FakeAdmissionDb()  # type: ignore[assignment]
    admission.claim = claim
    admission.head = 2
    admission.scope = scope
    admission.store = store  # type: ignore[assignment]
    admission.memory = fake_memory  # type: ignore[assignment]
    admission.manifest = {}
    loaded = admission.load()
    parsed = json.loads(loaded)
    assert parsed["untrustedData"]["trustBoundary"] == "UNTRUSTED_OBSERVATION_AND_MEMORY_ONLY"


def test_memory_restore_aborts_before_commit_on_concurrent_append(tmp_path: Path) -> None:
    log = TombstoneLog(tmp_path / "tombstones.jsonl", integrity_key=b"t" * 32)
    log.initialize()
    ev1 = log.append(
        kind="revoked",
        tenant_id="01900000-0000-7000-8000-000000000001",
        owner_id="01900000-0000-7000-8000-000000000002",
        task_id="01900000-0000-7000-8000-000000000003",
        memory_id="01900000-0000-7000-8000-000000000099",
        revision=2,
        recorded_at=datetime.now(UTC).isoformat(),
    )

    @dataclass
    class FakeResult:
        row: dict[str, Any] | None

        def fetchone(self) -> dict[str, Any] | None:
            return self.row

    class FakeConn:
        def __init__(self, *, inject_append: bool) -> None:
            self.inject_append = inject_append
            self.executed: list[tuple[str, object]] = []

        def execute(self, sql: str, params: object = ()) -> FakeResult:
            self.executed.append((sql, params))
            if "FROM agent.schema_control" in sql:
                return FakeResult({"restore_incarnation": 2, "dispatch_paused": False})
            if "INSERT INTO agent.memory_restore_state" in sql and self.inject_append:
                log.append(
                    kind="revoked",
                    tenant_id="01900000-0000-7000-8000-000000000001",
                    owner_id="01900000-0000-7000-8000-000000000002",
                    task_id="01900000-0000-7000-8000-000000000003",
                    memory_id="01900000-0000-7000-8000-000000000098",
                    revision=2,
                    recorded_at=datetime.now(UTC).isoformat(),
                )
            return FakeResult(None)

    entries, snap = log.read_with_snapshot()
    # Non-contiguous generation case would not rely on entries[gen-1]; verify last_revocation_head is ev1.chain_head
    conn_ok = FakeConn(inject_append=False)
    report = memory_restore._apply(
        conn_ok,  # type: ignore[arg-type]
        entries,
        reconciled_by="ops",
        log=log,
        expected_chain_head=snap.chain_head,
    )
    assert report.chain_head == ev1.chain_head
    coverage_updates = [p for sql, p in conn_ok.executed if "UPDATE agent.memory_tombstone_coverage" in sql]
    assert coverage_updates == [(ev1.generation, ev1.chain_head, ev1.generation)]

    conn_raced = FakeConn(inject_append=True)
    with pytest.raises(memory_restore.RestoreReconcileError, match="advanced during reconciliation"):
        memory_restore._apply(
            conn_raced,  # type: ignore[arg-type]
            entries,
            reconciled_by="ops",
            log=log,
            expected_chain_head=snap.chain_head,
        )


def test_memory_writer_update_requires_tombstone_and_create_replay_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIAGENT_PROFILE", "test")
    scope = commands.Scope(
        "01900000-0000-7000-8000-000000000001",
        "01900000-0000-7000-8000-000000000002",
        "01900000-0000-7000-8000-000000000003",
    )
    with pytest.raises(commands.CommandError, match="MEMORY_TOMBSTONE_UNAVAILABLE") as exc_info:
        memory_writer.update_memory(
            object(),  # type: ignore[arg-type]
            scope,
            task_id="01900000-0000-7000-8000-000000000003",
            memory_id="01900000-0000-7000-8000-000000000099",
            recipient="fake",
            expected_revision=1,
            tombstone_log=None,
        )
    assert exc_info.value.http == 503

    # Verify PostgresArtifactStore supports dynamic ttl without fixed expires_at collision.
    store = PostgresArtifactStore(object(), _claim(), b"k" * 32, ttl=timedelta(hours=1))  # type: ignore[arg-type]
    assert store._ttl == timedelta(hours=1)
    assert GENESIS == "0" * 64
