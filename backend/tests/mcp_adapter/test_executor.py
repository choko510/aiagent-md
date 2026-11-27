from __future__ import annotations

import time
from contextlib import asynccontextmanager

import pytest
from aiagent_gateways.executors import Destination, ExecutorFailed, ExecutorUnknown
from aiagent_gateways.mcp_executor import McpCallContext, McpToolExecutor
from aiagent_mcp_adapter.canonical import digest
from aiagent_mcp_adapter.errors import ErrorCode, McpError
from aiagent_mcp_adapter.registry_snapshot import SnapshotKey, SnapshotStore
from aiagent_mcp_adapter.telemetry import InMemoryTelemetry

from .helpers import entry, limits, provider, snapshot


class FakeSession:
    protocol_revision = "2025-11-25"
    server_name = "fake"
    server_version = "1.0"

    def __init__(self, *, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict, *, timeout_s: float):
        self.calls.append((name, arguments))
        if self.error is not None:
            raise self.error
        return self.result


class FakeManager:
    def __init__(self, session: FakeSession) -> None:
        self.session_obj = session
        self.opens = 0
        self.permits: list[str | None] = []

    @asynccontextmanager
    async def call_slot(self, provider_id):
        yield

    @asynccontextmanager
    async def session(
        self,
        provider,
        *,
        on_list_changed,
        run_id=None,
        permit=None,
        effect_tool=None,
        effect_arguments_digest=None,
        before_effect_send=None,
        expected_credential_version=None,
    ):
        self.opens += 1
        self.permits.append(permit)
        self.effect_tool = effect_tool
        self.effect_arguments_digest = effect_arguments_digest
        self.before_effect_send = before_effect_send
        self.expected_credential_version = expected_credential_version
        self.on_list_changed = on_list_changed
        yield self.session_obj


def build(*, risk_class: str = "read", result=None, error=None):
    store = SnapshotStore(limits())
    snap = snapshot(entry(risk_class=risk_class, approval_required=risk_class != "read"))
    fake = FakeSession(result=result, error=error)
    manager = FakeManager(fake)
    executor = McpToolExecutor(
        store=store,
        sessions=manager,  # type: ignore[arg-type]
        providers={"demo": provider()},
        limits=limits(),
        telemetry=InMemoryTelemetry(),
    )
    return store, snap, executor, manager, fake


async def prepare(executor: McpToolExecutor, snap, args: dict, *, effect: str = "eff-1") -> McpCallContext:
    await executor.store.publish(snap, expected_generation=0)
    return await executor.prepare(
        effect_key=effect,
        snapshot=snap,
        entry=snap.entries[0],
        run_id="run-1",
        intent_id="intent-1",
        arguments_digest=digest(args),
    )


async def test_read_call_returns_normalized_payload_and_receipt() -> None:
    result = {"content": [{"type": "text", "text": "ok"}], "structuredContent": {"receipt": "rcpt-1"}}
    store, snap, executor, manager, fake = build(result=result)
    args = {"text": "hello"}
    await prepare(executor, snap, args)
    payload = await executor.execute("eff-1", args, "permit-1")
    assert manager.opens == 1
    assert manager.permits == ["permit-1"]
    assert fake.calls == [("echo", args)]
    assert payload["mcp"]["status"] == "OK"
    assert payload["mcp"]["text"] == "ok"
    assert "receipt" not in payload
    assert payload["result_digest"]


async def test_argument_digest_mismatch_fails_before_any_session() -> None:
    store, snap, executor, manager, _ = build(result={"content": []})
    await prepare(executor, snap, {"text": "hello"})
    with pytest.raises(ExecutorFailed, match="arguments_digest"):
        await executor.execute("eff-1", {"text": "tampered"}, "permit-1")
    assert manager.opens == 0


async def test_missing_permit_fails_before_any_session() -> None:
    store, snap, executor, manager, _ = build(result={"content": []})
    await prepare(executor, snap, {"text": "hello"})
    with pytest.raises(ExecutorFailed, match="permit"):
        await executor.execute("eff-1", {"text": "hello"}, None)
    assert manager.opens == 0


async def test_uncertain_write_is_unknown_but_uncertain_read_is_failed() -> None:
    error = McpError(ErrorCode.TRANSPORT_TIMEOUT, "timeout", effect_uncertain=True)
    store, snap, executor, _, _ = build(risk_class="write_reversible", error=error)
    await prepare(executor, snap, {"text": "hello"})
    with pytest.raises(ExecutorUnknown):
        await executor.execute("eff-1", {"text": "hello"}, "permit-1")

    store, snap, executor, _, _ = build(risk_class="read", error=error)
    await prepare(executor, snap, {"text": "hello"})
    with pytest.raises(ExecutorFailed):
        await executor.execute("eff-1", {"text": "hello"}, "permit-1")


async def test_remote_tool_error_on_write_is_unknown_not_confirmed() -> None:
    store, snap, executor, _, _ = build(
        risk_class="write_reversible",
        result={"content": [{"type": "text", "text": "boom"}], "isError": True},
    )
    await prepare(executor, snap, {"text": "hello"})
    with pytest.raises(ExecutorUnknown):
        await executor.execute("eff-1", {"text": "hello"}, "permit-1")


async def test_revoked_run_never_reaches_the_session() -> None:
    store, snap, executor, manager, _ = build(result={"content": []})
    await prepare(executor, snap, {"text": "hello"})
    await store.revoke_run("run-1")
    with pytest.raises(ExecutorFailed, match="POLICY_DENIED"):
        await executor.execute("eff-1", {"text": "hello"}, "permit-1")
    assert manager.opens == 0


async def test_binding_is_single_use() -> None:
    store, snap, executor, manager, _ = build(result={"content": [{"type": "text", "text": "ok"}]})
    await prepare(executor, snap, {"text": "hello"})
    await executor.execute("eff-1", {"text": "hello"}, "permit-1")
    with pytest.raises(ExecutorFailed, match="binding_missing"):
        await executor.execute("eff-1", {"text": "hello"}, "permit-1")


async def test_destination_cannot_be_resolved_without_admitted_identity() -> None:
    store, snap, executor, _, _ = build(result={"content": []})
    with pytest.raises(ExecutorFailed, match="admitted_identity"):
        executor.destination_for_tool("echo", {"text": "x"})
    assert executor.destination() is None
    dest = executor.destination_for_entry(snap.entries[0])
    assert dest == Destination("https", "mcp.example.com", 443, "POST")


async def test_pinned_input_schema_rejects_wrong_arguments_before_session() -> None:
    store, snap, executor, manager, _ = build(result={"content": [{"type": "text", "text": "ok"}]})
    await prepare(executor, snap, {"text": 5})
    with pytest.raises(ExecutorFailed, match="ARGUMENTS_INVALID"):
        await executor.execute("eff-1", {"text": 5}, "permit-1")
    assert manager.opens == 0


async def test_arguments_mutated_after_prepare_are_rejected_before_session() -> None:
    store, snap, executor, manager, _ = build(result={"content": [{"type": "text", "text": "ok"}]})
    args = {"text": "hello"}
    await prepare(executor, snap, args)
    args["text"] = "tampered-after-prepare"
    with pytest.raises(ExecutorFailed, match="arguments_digest"):
        await executor.execute("eff-1", args, "permit-1")
    assert manager.opens == 0


async def test_empty_write_result_is_unknown_not_confirmed() -> None:
    store, snap, executor, _, _ = build(risk_class="write_reversible", result={"content": []})
    await prepare(executor, snap, {"text": "hello"})
    with pytest.raises(ExecutorUnknown, match="EFFECT_UNCERTAIN"):
        await executor.execute("eff-1", {"text": "hello"}, "permit-1")


async def test_truncated_read_result_is_failed_not_confirmed() -> None:
    store, snap, executor, _, _ = build(result={"content": [{"type": "text", "text": "x" * 200_000}]})
    await prepare(executor, snap, {"text": "hello"})
    with pytest.raises(ExecutorFailed, match="RESULT_UNSUPPORTED"):
        await executor.execute("eff-1", {"text": "hello"}, "permit-1")


async def test_list_changed_invalidates_the_run_pin() -> None:
    store, snap, executor, manager, _ = build(result={"content": [{"type": "text", "text": "ok"}]})
    await prepare(executor, snap, {"text": "hello"})
    await store.invalidate_pins(SnapshotKey("tenant-a", "owner-a", "demo"))
    with pytest.raises(ExecutorFailed, match="POLICY_DENIED"):
        await executor.execute("eff-1", {"text": "hello"}, "permit-1")
    assert manager.opens == 0


async def test_actual_send_fence_refuses_a_revoked_run() -> None:
    store, snap, executor, manager, _ = build(result={"content": [{"type": "text", "text": "ok"}]})
    ctx = await prepare(executor, snap, {"text": "hello"})
    await store.revoke_run("run-1")
    with pytest.raises(McpError) as err:
        await executor._before_effect_send(ctx, time.time() + 30)
    assert err.value.code is ErrorCode.POLICY_DENIED


@pytest.mark.parametrize(
    "overrides",
    [
        {"endpoint": "https://other.example/mcp"},
        {"policy_revision": 3},
        {"credential_version": 2},
        {"enabled": False},
    ],
)
async def test_current_provider_revision_change_fails_closed(overrides: dict) -> None:
    store, snap, executor, manager, _ = build(result={"content": [{"type": "text", "text": "ok"}]})
    await prepare(executor, snap, {"text": "hello"})
    executor.providers["demo"] = provider(**overrides)
    with pytest.raises(ExecutorFailed):
        await executor.execute("eff-1", {"text": "hello"}, "permit-1")
    assert manager.opens == 0
