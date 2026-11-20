"""Renew a Git worker watchdog only after a canonical operation check.

This is a liveness mechanism, not an atomic fence between database revocation
and Git CAS. Production dispatch remains disabled until that fence is proven.
"""

from __future__ import annotations

import asyncio
import time

from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_core.rpc import RpcClient
from aiagent_domain import is_uuid7


def _check_git_operation(db: Database, claim: Claim, workspace: str, operation: str) -> float:
    with db.tx(tenant=claim.tenant_id) as c:
        c.execute("SELECT set_config('statement_timeout', '1000ms', true)")
        c.execute("SELECT set_config('lock_timeout', '1000ms', true)")
        row = c.execute(
            "SELECT agent.git_operation_heartbeat(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS expires_at",
            (
                claim.tenant_id,
                claim.owner_user_id,
                workspace,
                claim.task_id,
                claim.run_id,
                claim.goal_revision,
                claim.lease_epoch,
                claim.worker_ref,
                claim.restore_incarnation,
                operation,
            ),
        ).fetchone()
        if row is None or float(row["expires_at"]) <= time.time():
            raise ValueError("Git authority expired")
        return float(row["expires_at"])


async def pulse_git_authority(
    db: Database,
    claim: Claim,
    endpoint: str,
    rpc_key: bytes,
    revoked: asyncio.Event,
    stop: asyncio.Event,
    ready: asyncio.Event,
    *,
    workspace: str,
    operation: str,
    attempt: str,
    request_sha256: str,
    interval: float = 0.25,
) -> None:
    """Poll authority and renew the matching remote watchdog; errors revoke locally."""
    if (
        not endpoint.startswith("unix:/")
        or len(rpc_key) < 32
        or not is_uuid7(workspace)
        or not is_uuid7(operation)
        or not is_uuid7(attempt)
        or len(request_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in request_sha256)
        or not 0.05 <= interval <= 1
    ):
        raise ValueError("invalid Git heartbeat scope")
    body = {"operation": operation, "attempt": attempt, "request_sha256": request_sha256}
    try:
        while not stop.is_set() and not revoked.is_set():
            await asyncio.wait_for(
                asyncio.to_thread(_check_git_operation, db, claim, workspace, operation), timeout=1.5
            )
            result = await asyncio.wait_for(
                RpcClient(endpoint, "tool_gateway", rpc_key, timeout_s=1).call("git.heartbeat", body), timeout=1.5
            )
            if result.get("renewed") is not True:
                raise ValueError("Git heartbeat refused")
            ready.set()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass
    except asyncio.CancelledError:
        revoked.set()
        raise
    except Exception:
        revoked.set()
        try:
            await asyncio.wait_for(
                RpcClient(endpoint, "tool_gateway", rpc_key, timeout_s=1).call("git.cancel", body), timeout=1.5
            )
        except Exception:
            pass  # Remote watchdog expiry fences an unconfirmed cancellation.
