"""Fail-closed Run Authority polling for an internal execution bridge.

The event is local process state, not an authorization grant. Deployment must
connect it to a separately authenticated Cell cancellation channel; this module
alone does not revoke a remote Supervisor or publish Artifacts.
"""

from __future__ import annotations

import asyncio

from aiagent_core import control
from aiagent_core.db import Database


def _check(db: Database, claim: control.Claim) -> None:
    with db.tx(tenant=claim.tenant_id) as c:
        c.execute("SELECT set_config('statement_timeout', '1000ms', true)")
        c.execute("SELECT set_config('lock_timeout', '1000ms', true)")
        control.check_current(c, claim)


async def watch_run_authority(
    db: Database,
    claim: control.Claim,
    revoked: asyncio.Event,
    stop: asyncio.Event,
    ready: asyncio.Event,
    *,
    interval: float = 0.25,
) -> None:
    """Set revoked on stale authority, database failure or a stalled check.

    Callers must authenticate and bind the remote cancellation to the same
    Execution. No assertion about that transport is made here.
    """
    if not 0.05 <= interval <= 1.0:
        raise ValueError("invalid authority polling interval")
    while not stop.is_set() and not revoked.is_set():
        try:
            await asyncio.wait_for(asyncio.to_thread(_check, db, claim), timeout=1.5)
        except asyncio.CancelledError:
            revoked.set()
            raise
        except Exception:
            revoked.set()
            return
        if not stop.is_set() and not revoked.is_set():
            ready.set()
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
