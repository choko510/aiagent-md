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


@pytest.mark.parametrize("authority_fails", [False, True])
def test_pulse_occurs_only_after_authority_check(monkeypatch: pytest.MonkeyPatch, authority_fails: bool) -> None:
    import aiagent_core.rpc

    checks, calls = [], []

    def check(*_args: object) -> None:
        checks.append("checked")
        if authority_fails:
            raise OSError("unavailable")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def call(self, method, body):
            assert checks and method == "exec.heartbeat"
            calls.append(body)
            return {"renewed": True}

    monkeypatch.setattr(execution_watch, "_check", check)
    monkeypatch.setattr(aiagent_core.rpc, "RpcClient", FakeClient)

    async def exercise() -> None:
        revoked, stop, ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
        claim = SimpleNamespace(tenant_id="t")
        task = asyncio.create_task(
            execution_watch.pulse_supervisor_authority(
                SimpleNamespace(),
                claim,
                "unix:/unused",
                b"k" * 32,
                revoked,
                stop,
                ready,
                tenant_id="t",
                execution_id="e",
                attempt_id="a",
                binding_sha256="b" * 64,
                interval=0.05,
            )
        )
        if authority_fails:
            await asyncio.wait_for(revoked.wait(), 5)
            assert not ready.is_set() and not calls
        else:
            await asyncio.wait_for(ready.wait(), 5)
            assert calls and not revoked.is_set()
            stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(exercise())
