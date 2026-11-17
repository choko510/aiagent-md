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
