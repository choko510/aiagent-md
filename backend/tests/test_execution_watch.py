from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from aiagent_gateways import execution_watch


def test_watch_fails_closed_on_authority_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object) -> None:
        raise OSError("unavailable")

    monkeypatch.setattr(execution_watch, "_check", fail)

    async def run() -> None:
        revoked, stop, ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
        await execution_watch.watch_run_authority(
            SimpleNamespace(), SimpleNamespace(), revoked, stop, ready, interval=0.05
        )
        assert revoked.is_set() and not ready.is_set()

    asyncio.run(run())


def test_watch_fails_closed_if_authority_stalls(monkeypatch: pytest.MonkeyPatch) -> None:
    def stalled(*_args: object) -> None:
        time.sleep(1.7)

    monkeypatch.setattr(execution_watch, "_check", stalled)

    async def run() -> None:
        revoked, stop, ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
        await execution_watch.watch_run_authority(
            SimpleNamespace(), SimpleNamespace(), revoked, stop, ready, interval=0.05
        )
        assert revoked.is_set() and not ready.is_set()

    asyncio.run(run())


@pytest.mark.parametrize("fails", [False, True])
def test_relay_sends_scoped_cancel_and_reports_unknown(monkeypatch: pytest.MonkeyPatch, fails: bool) -> None:
    import aiagent_core.rpc

    seen = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def call(self, method, body):
            seen.append((method, body))
            if fails:
                raise OSError("unavailable")
            return {"cancelled": True}

    monkeypatch.setattr(aiagent_core.rpc, "RpcClient", FakeClient)

    async def run() -> None:
        revoked, stop = asyncio.Event(), asyncio.Event()
        revoked.set()
        result = await execution_watch.relay_revocation(
            "unix:/unused",
            b"k" * 32,
            revoked,
            stop,
            tenant_id="tenant",
            execution_id="execution",
            attempt_id="attempt",
            binding_sha256="a" * 64,
        )
        assert result is not fails

    asyncio.run(run())
    assert seen == [
        (
            "exec.cancel",
            {"tenantId": "tenant", "executionId": "execution", "attemptId": "attempt", "bindingSha256": "a" * 64},
        )
    ]
