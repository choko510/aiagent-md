"""Authenticated Worker-to-Gateway CAS admission for one canonical Run.

This endpoint records irrevocable admission but cannot prove the Git ref was
updated. An ambiguous response must never cause another CAS attempt.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from aiagent_core import config
from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_core.rpc import Endpoint, RpcServer
from aiagent_domain import is_uuid7
from aiagent_domain.rpcwire import RpcError

from .context_keys import KeyRing
from .git_operation import enter_git_cas


def build_git_cas_fence_server(
    endpoint: str, rpc_key: bytes, db: Database, claim: Claim, keys: KeyRing, journal_key: bytes, *, allowed_uid: int
) -> RpcServer:
    if (
        config.profile() != "test"
        or not endpoint.startswith("unix:/")
        or len(rpc_key) < 32
        or len(journal_key) < 32
        or rpc_key == journal_key
        or type(allowed_uid) is not int
        or allowed_uid <= 0
    ):
        raise ValueError("Git CAS fence server unavailable")
    server = RpcServer("git_cas_fence", Endpoint(endpoint), {"git_worker": rpc_key})
    server.allowed_uids["git_worker"] = {allowed_uid}

    async def enter(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        if (
            set(body) != {"operation", "journal"}
            or not is_uuid7(body.get("operation"))
            or not isinstance(body.get("journal"), dict)
        ):
            raise RpcError("BAD_REQUEST")
        try:
            await asyncio.to_thread(
                enter_git_cas,
                db,
                claim,
                keys,
                body["operation"],
                journal_key,
                body["journal"],
                deadline=time.monotonic() + 5,
            )
        except Exception as exc:
            raise RpcError("GIT_CAS_ADMISSION_UNKNOWN") from exc
        return {"operation": body["operation"], "admitted": True}

    server.register("git.enter_cas", enter, {"git_worker"})
    return server
