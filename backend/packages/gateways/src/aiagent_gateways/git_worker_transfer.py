"""Authenticated, bounded Git patch transfer; this module never starts CAS."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

from aiagent_core.rpc import RpcClient
from aiagent_sandbox.binary_transfer import MAX_CHUNK, encode_chunks
from aiagent_sandbox.git_worker_rpc import git_transfer_binding, git_transfer_key, sign_git_worker_grant

from .git_patch_artifacts import VerifiedGitPatch


class GitTransferUnknown(RuntimeError):
    """A remote session may exist; never automatically resend this operation."""


async def register_git_transfer(
    endpoint: str,
    rpc_key: bytes,
    grant_key: bytes,
    payload: dict[str, Any],
    patch: VerifiedGitPatch,
    *,
    deadline: float,
) -> None:
    """Register a one-use worker session after the caller has checked authority."""
    try:
        if (
            not endpoint.startswith("unix:/")
            or len(rpc_key) < 32
            or len(grant_key) < 32
            or rpc_key == grant_key
            or not time.monotonic() < deadline <= time.monotonic() + 60
            or payload.get("patch_id") != patch.artifact_id
            or payload.get("parent") != patch.parent
            or payload.get("patch_sha256") != patch.digest
            or payload.get("patch_bytes") != len(patch.data)
            or payload.get("paths") != list(patch.paths)
            or not hmac.compare_digest(hashlib.sha256(patch.data).hexdigest(), patch.digest)
        ):
            raise ValueError("Git transfer scope invalid")
        grant = sign_git_worker_grant(grant_key, payload)
        result = await RpcClient(
            endpoint, "tool_gateway", rpc_key, timeout_s=max(0.1, deadline - time.monotonic())
        ).call("git.begin", {"grant": grant})
        if result.get("operation") != payload["operation"]:
            raise ValueError("Git session mismatch")
    except Exception as exc:
        raise GitTransferUnknown("Git session registration requires reconciliation") from exc


async def send_git_patch_frames(
    endpoint: str,
    rpc_key: bytes,
    transfer_key: bytes,
    payload: dict[str, Any],
    patch: VerifiedGitPatch,
    *,
    deadline: float,
) -> str:
    """Send authenticated frames and return the terminator for a separately fenced CAS."""
    try:
        if (
            not endpoint.startswith("unix:/")
            or len(rpc_key) < 32
            or len(transfer_key) < 32
            or rpc_key == transfer_key
            or not time.monotonic() < deadline <= time.monotonic() + 60
            or payload.get("patch_id") != patch.artifact_id
            or payload.get("patch_sha256") != patch.digest
            or payload.get("patch_bytes") != len(patch.data)
            or payload.get("paths") != list(patch.paths)
            or not hmac.compare_digest(hashlib.sha256(patch.data).hexdigest(), patch.digest)
        ):
            raise ValueError("Git transfer scope invalid")
        chunks = (patch.data[offset : offset + MAX_CHUNK] for offset in range(0, len(patch.data), MAX_CHUNK))
        frames = iter(
            encode_chunks(git_transfer_key(transfer_key, payload["operation"]), git_transfer_binding(payload), chunks)
        )
        client = RpcClient(endpoint, "tool_gateway", rpc_key, timeout_s=max(0.1, deadline - time.monotonic()))
        for frame in frames:
            if len(frame) == 44:  # authenticated binary terminator; do not execute here
                if time.monotonic() >= deadline:
                    raise TimeoutError
                return base64.b64encode(frame).decode("ascii")
            if time.monotonic() >= deadline:
                raise TimeoutError
            result = await client.call(
                "git.chunk", {"operation": payload["operation"], "frame": base64.b64encode(frame).decode("ascii")}
            )
            if result.get("accepted") is not True:
                raise ValueError("Git frame refused")
        raise ValueError("Git terminator missing")
    except Exception as exc:
        raise GitTransferUnknown("Git transfer requires reconciliation") from exc
