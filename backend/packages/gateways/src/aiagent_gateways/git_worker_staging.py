"""Stage a canonical Git patch in a worker without starting Git or CAS.

The returned session is not permission to execute. A separately verified
CAS fence is required before this path can be connected to git.execute.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_core.rpc import RpcClient

from .context_keys import KeyRing
from .git_authority_watch import pulse_git_authority
from .git_operation import PreparedGitWorkerRequest, prepare_git_worker_request
from .git_worker_transfer import GitTransferUnknown, register_git_transfer, send_git_patch_frames


@dataclass
class StagedGitWorker:
    request: PreparedGitWorkerRequest
    terminator: str = field(repr=False)
    revoked: asyncio.Event = field(repr=False)
    stop: asyncio.Event = field(repr=False)
    pulse: asyncio.Task[None] = field(repr=False)
    endpoint: str = field(repr=False)
    rpc_key: bytes = field(repr=False)

    async def cancel(self) -> bool:
        """Stop the watchdog and request cancellation; failed confirmation remains UNKNOWN."""
        self.stop.set()
        await asyncio.gather(self.pulse, return_exceptions=True)
        payload = self.request.payload
        try:
            response = await asyncio.wait_for(
                RpcClient(self.endpoint, "tool_gateway", self.rpc_key, timeout_s=1).call(
                    "git.cancel",
                    {
                        "operation": payload["operation"],
                        "attempt": payload["attempt"],
                        "request_sha256": payload["request_sha256"],
                    },
                ),
                timeout=1.5,
            )
            return response.get("cancelled") is True
        except Exception:
            return False


async def stage_git_worker(
    db: Database,
    claim: Claim,
    keys: KeyRing,
    operation: str,
    *,
    endpoint: str,
    rpc_key: bytes,
    grant_key: bytes,
    transfer_key: bytes,
    deadline: float,
) -> StagedGitWorker:
    """Prepare, register and transfer a patch while the canonical watchdog is live."""
    stop, revoked, ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    task: asyncio.Task[None] | None = None
    prepared: PreparedGitWorkerRequest | None = None
    try:
        prepared = await asyncio.to_thread(prepare_git_worker_request, db, claim, keys, operation, deadline=deadline)
        p: dict[str, Any] = prepared.payload
        await register_git_transfer(endpoint, rpc_key, grant_key, p, prepared.patch, deadline=deadline)
        task = asyncio.create_task(
            pulse_git_authority(
                db,
                claim,
                endpoint,
                rpc_key,
                revoked,
                stop,
                ready,
                workspace=p["workspace"],
                operation=operation,
                attempt=p["attempt"],
                request_sha256=p["request_sha256"],
            )
        )
        await asyncio.wait_for(ready.wait(), timeout=max(0.01, min(3, deadline - time.monotonic())))
        if revoked.is_set():
            raise GitTransferUnknown("Git authority revoked")
        terminator = await send_git_patch_frames(endpoint, rpc_key, transfer_key, p, prepared.patch, deadline=deadline)
        if revoked.is_set() or time.monotonic() >= deadline:
            raise GitTransferUnknown("Git authority revoked")
        return StagedGitWorker(prepared, terminator, revoked, stop, task, endpoint, rpc_key)
    except BaseException:
        stop.set()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if prepared is not None:
            p = prepared.payload
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    RpcClient(endpoint, "tool_gateway", rpc_key, timeout_s=1).call(
                        "git.cancel",
                        {"operation": p["operation"], "attempt": p["attempt"], "request_sha256": p["request_sha256"]},
                    ),
                    timeout=1.5,
                )
        raise
