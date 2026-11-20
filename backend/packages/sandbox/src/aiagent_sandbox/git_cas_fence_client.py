"""Authenticated pre-CAS handshake; it never retries an ambiguous admission."""

from __future__ import annotations

import asyncio
from typing import Any

from aiagent_core import config
from aiagent_core.rpc import RpcClient


class RpcGitCasAdmitter:
    def __init__(self, endpoint: str, key: bytes) -> None:
        if config.profile() != "test" or not endpoint.startswith("unix:/") or len(key) < 32:
            raise ValueError("Git CAS admission unavailable")
        self._endpoint, self._key = endpoint, key

    def __call__(self, payload: dict[str, Any], journal: dict[str, Any]) -> None:
        if config.profile() != "test":
            raise ValueError("Git CAS admission unavailable")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise ValueError("Git CAS admission must run in a worker thread")
        result = asyncio.run(
            RpcClient(self._endpoint, "git_worker", self._key, timeout_s=5).call(
                "git.enter_cas", {"operation": payload["operation"], "journal": journal}
            )
        )
        if result != {"operation": payload["operation"], "admitted": True}:
            raise ValueError("Git CAS admission unavailable")
