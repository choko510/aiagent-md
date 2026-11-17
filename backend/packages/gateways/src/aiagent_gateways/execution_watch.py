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


async def relay_revocation(
    endpoint: str,
    rpc_key: bytes,
    revoked: asyncio.Event,
    stop: asyncio.Event,
    *,
    tenant_id: str,
    execution_id: str,
    attempt_id: str,
    binding_sha256: str,
) -> bool:
    """Send a scoped cancellation over authenticated RPC; False means no confirmed stop.

    This does not poll authority itself or handle disconnected Cell fail-closed
    watchdogs. The caller must couple it to a verified Execution and track UNKNOWN.
    """
    from aiagent_core.rpc import RpcClient

    revoked_wait = asyncio.create_task(revoked.wait())
    stop_wait = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({revoked_wait, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
        if not revoked.is_set():
            return False
        try:
            result = await asyncio.wait_for(
                RpcClient(endpoint, "tool_gateway", rpc_key, timeout_s=2).call(
                    "exec.cancel",
                    {
                        "tenantId": tenant_id,
                        "executionId": execution_id,
                        "attemptId": attempt_id,
                        "bindingSha256": binding_sha256,
                    },
                ),
                timeout=3,
            )
            return result.get("cancelled") is True
        except Exception:
            return False
    finally:
        revoked_wait.cancel()
        stop_wait.cancel()
        await asyncio.gather(revoked_wait, stop_wait, return_exceptions=True)
