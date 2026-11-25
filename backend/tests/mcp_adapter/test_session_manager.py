"""SessionManager: feature flag, revocation, credential gating and a bounded call slot."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest
from aiagent_mcp_adapter.config import AdapterLimits, ProviderConfig
from aiagent_mcp_adapter.errors import ErrorCode, McpError
from aiagent_mcp_adapter.session_manager import SessionManager
from aiagent_mcp_adapter.transport import Credential

from .helpers import limits, provider


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def open(
        self,
        *,
        provider: ProviderConfig,
        credential: Credential | None,
        on_list_changed: Any,
        limits: AdapterLimits,
        permit: str | None = None,
    ):
        self.calls.append({"provider": provider, "credential": credential, "limits": limits, "permit": permit})
        yield object()


class FakeBroker:
    def __init__(self, credential: Credential | None = None) -> None:
        self.credential = credential
        self.calls: list[tuple[ProviderConfig, str | None]] = []

    async def resolve(self, provider: ProviderConfig, *, run_id: str | None = None) -> Credential | None:
        self.calls.append((provider, run_id))
        return self.credential


def manager(
    *, limits: AdapterLimits | None = None, broker: FakeBroker | None = None
) -> tuple[SessionManager, FakeTransport, FakeBroker]:
    transport = FakeTransport()
    broker = broker or FakeBroker()
    return (
        SessionManager(transport, broker, limits or AdapterLimits()),  # type: ignore[arg-type]
        transport,
        broker,
    )


def bearer_provider(**overrides: Any) -> ProviderConfig:
    data: dict[str, Any] = {
        "auth_mode": "bearer",
        "credential_binding_ref": "binding-1",
        "credential_header": "Authorization",
    }
    data.update(overrides)
    return provider(**data)


async def test_feature_flag_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_ADAPTER_ENABLED", "0")
    mgr, transport, _ = manager()
    with pytest.raises(McpError) as err:
        async with mgr.session(provider(), on_list_changed=lambda: None):
            pass
    assert err.value.code is ErrorCode.ADAPTER_DISABLED
    assert transport.calls == []


async def test_disabled_provider_and_revocation_do_not_open_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_ADAPTER_ENABLED", "1")
    mgr, transport, _ = manager()
    with pytest.raises(McpError) as err:
        async with mgr.session(provider(enabled=False), on_list_changed=lambda: None):
            pass
    assert err.value.code is ErrorCode.ADAPTER_DISABLED

    mgr.revoke("demo")
    with pytest.raises(McpError) as err:
        async with mgr.session(provider(), on_list_changed=lambda: None):
            pass
    assert err.value.code is ErrorCode.POLICY_DENIED
    assert mgr.is_revoked("demo")
    assert transport.calls == []


async def test_missing_credential_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_ADAPTER_ENABLED", "1")
    mgr, transport, _ = manager()
    with pytest.raises(McpError) as err:
        async with mgr.session(bearer_provider(), on_list_changed=lambda: None):
            pass
    assert err.value.code is ErrorCode.CREDENTIAL_UNAVAILABLE
    assert transport.calls == []


async def test_credential_is_passed_only_to_the_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_ADAPTER_ENABLED", "1")
    credential = Credential("Authorization", "secret", version=1)
    mgr, transport, broker = manager(broker=FakeBroker(credential))
    async with mgr.session(bearer_provider(), on_list_changed=lambda: None, run_id="run-1") as session:
        assert session is not None
    assert broker.calls and broker.calls[0][1] == "run-1"
    assert transport.calls[0]["credential"] == credential
    assert mgr.active == 0


async def test_call_slot_bounds_concurrency() -> None:
    mgr, _, _ = manager(limits=limits(max_concurrent_calls=1))
    first_entered = asyncio.Event()
    release = asyncio.Event()
    second_entered = asyncio.Event()

    async def hold_first() -> None:
        async with mgr.call_slot("demo"):
            first_entered.set()
            await release.wait()

    async def take_second() -> None:
        async with mgr.call_slot("demo"):
            second_entered.set()

    first = asyncio.create_task(hold_first())
    await first_entered.wait()
    second = asyncio.create_task(take_second())
    await asyncio.sleep(0.02)
    assert not second_entered.is_set()
    release.set()
    await asyncio.gather(first, second)
    assert second_entered.is_set()
