"""Recheck authenticated Git object bytes from an isolated Git worker."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from typing import Any

from aiagent_core.rpc import RpcClient
from aiagent_sandbox.git_worker_rpc import sign_git_worker_grant

from .git_patch_artifacts import VerifiedGitPatch

_EXTRA = frozenset({"commit", "tree", "cas", "worker_id"})


class RpcGitObjectVerifier:
    """Authenticate the worker and independently hash its bounded Git objects."""

    def __init__(self, endpoint: str, rpc_key: bytes, grant_key: bytes, worker_id: str) -> None:
        if (
            not endpoint.startswith("unix:/")
            or len(rpc_key) < 32
            or len(grant_key) < 32
            or rpc_key == grant_key
            or not worker_id
        ):
            raise ValueError("Git object verifier unavailable")
        self._endpoint, self._rpc_key, self._grant_key, self._worker_id = endpoint, rpc_key, grant_key, worker_id

    def verify(self, result: dict[str, Any], patch: VerifiedGitPatch, *, deadline: float) -> None:
        try:
            if not time.monotonic() < deadline <= time.monotonic() + 60:
                raise ValueError
            # Inspection gets a new, short-lived grant after the caller has
            # rechecked the canonical Run and binding; it never initiates CAS.
            payload = {k: v for k, v in result.items() if k not in _EXTRA}
            payload["expires_at"] = int(time.time()) + min(30, max(1, int(deadline - time.monotonic())))
            grant = sign_git_worker_grant(self._grant_key, payload)
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise ValueError("Git object verification requires a worker thread")
            response = asyncio.run(
                RpcClient(
                    self._endpoint, "tool_gateway", self._rpc_key, timeout_s=max(0.1, deadline - time.monotonic())
                ).call("git.inspect", {"grant": grant})
            )
            if (
                set(response)
                != {"operation", "worker_id", "commit", "tree", "parent", "diff_sha256", "commit_object", "tree_object"}
                or response["operation"] != result["operation"]
                or response["worker_id"] != self._worker_id
                or response["commit"] != result["commit"]
                or response["tree"] != result["tree"]
                or response["parent"] != result["parent"]
                or response["diff_sha256"] != patch.digest
            ):
                raise ValueError
            commit = base64.b64decode(response["commit_object"], validate=True)
            tree = base64.b64decode(response["tree_object"], validate=True)
            if not 1 <= len(commit) <= 1024 * 1024 or not 1 <= len(tree) <= 1024 * 1024:
                raise ValueError

            def git_sha(kind: bytes, data: bytes) -> str:
                return hashlib.sha1(
                    kind + b" " + str(len(data)).encode("ascii") + b"\0" + data, usedforsecurity=False
                ).hexdigest()

            if git_sha(b"commit", commit) != result["commit"] or git_sha(b"tree", tree) != result["tree"]:
                raise ValueError
            headers = commit.split(b"\n\n", 1)[0].splitlines()
            trees = [line[5:] for line in headers if line.startswith(b"tree ")]
            parents = [line[7:] for line in headers if line.startswith(b"parent ")]
            if trees != [result["tree"].encode("ascii")] or parents != [result["parent"].encode("ascii")]:
                raise ValueError
            if time.monotonic() >= deadline:
                raise ValueError
        except Exception as exc:
            raise ValueError("Git object verification unavailable") from exc
