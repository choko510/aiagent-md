"""Client for an explicitly registered, one-shot source staging session.

This is not a production dispatcher: the caller must establish canonical
authority and arrange registration with the remote supervisor separately.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from collections.abc import Iterable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from aiagent_core.rpc import RpcClient
from aiagent_development.code_intelligence.git_proof import GitTreeProof
from aiagent_development.code_intelligence.projection import ProjectionManifest
from aiagent_domain.rpcwire import decode_length, encode_frame, sign_request, verify_response
from aiagent_sandbox.binary_transfer import encode_chunks
from aiagent_sandbox.source_ticket import source_binding, source_transfer_key, verify_source_ticket

if TYPE_CHECKING:
    from aiagent_development.workspace_authority import PreparedExecution

    from .execution_lifecycle import ExecutionLifecycle


class SourceRegistrationUnknown(RuntimeError):
    """Registration may have reached the Supervisor; never retry automatically."""


async def register_issued_source(
    lifecycle: ExecutionLifecycle,
    prepared: PreparedExecution,
    endpoint: str,
    rpc_key: bytes,
    ticket_key: bytes,
    ticket: dict[str, Any],
    manifest: ProjectionManifest,
    proof: GitTreeProof,
    *,
    deadline: float,
) -> tuple[str, bytes, bytes]:
    """Register a scoped ticket and fence ambiguous outcomes in the canonical DB."""
    payload = verify_source_ticket(ticket_key, ticket)
    if payload["execution"] != prepared.id or payload["binding_sha256"] != prepared.binding_sha256:
        raise ValueError("source ticket execution mismatch")
    try:
        return await register_source(endpoint, rpc_key, ticket_key, ticket, manifest, proof, deadline=deadline)
    except (Exception, asyncio.CancelledError) as exc:
        try:
            await asyncio.shield(
                asyncio.to_thread(
                    lifecycle.mark_unknown, prepared.id, prepared.binding_sha256, deadline=time.monotonic() + 5
                )
            )
        except Exception as mark_exc:
            raise SourceRegistrationUnknown("registration and UNKNOWN persistence unavailable") from mark_exc
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise SourceRegistrationUnknown("source registration outcome unknown") from exc


async def register_source(
    endpoint: str,
    rpc_key: bytes,
    ticket_key: bytes,
    ticket: dict[str, Any],
    manifest: ProjectionManifest,
    proof: GitTreeProof,
    *,
    deadline: float,
) -> tuple[str, bytes, bytes]:
    """Register a DB-issued ticket over authenticated RPC; callers enforce live authority."""
    p = verify_source_ticket(ticket_key, ticket)
    body = {
        "ticket": ticket,
        "files": [asdict(item) for item in manifest.files],
        "total_bytes": manifest.total_bytes,
        "commit_proof": base64.b64encode(proof.commit).decode("ascii"),
        "trees": [{"oid": item.oid, "data": base64.b64encode(item.data).decode("ascii")} for item in proof.trees],
    }
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("source deadline exceeded")
    result = await asyncio.wait_for(
        RpcClient(endpoint, "tool_gateway", rpc_key).call("source.register", body), timeout=remaining
    )
    if result != {"sessionId": p["session"]}:
        raise RuntimeError("source registration rejected")
    return (
        p["session"],
        source_transfer_key(ticket_key, p["session"]),
        source_binding(
            p["session"],
            p["execution"],
            p["attempt"],
            p["binding_sha256"],
            p["snapshot"],
            p["source_bytes"],
            p["source_sha256"],
        ),
    )


async def send_source(
    endpoint: str,
    rpc_key: bytes,
    session_id: str,
    transfer_key: bytes,
    binding: bytes,
    chunks: Iterable[bytes],
    *,
    deadline: float,
) -> None:
    """Send frames and require an authenticated staging result before returning."""
    env = sign_request(rpc_key, "tool_gateway", "source.receive", {"sessionId": session_id})
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("source deadline exceeded")
    reader, writer = await asyncio.wait_for(RpcClient(endpoint, "tool_gateway", rpc_key)._open(), timeout=remaining)
    try:
        writer.write(encode_frame(env))
        for frame in encode_chunks(transfer_key, binding, chunks):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("source deadline exceeded")
            writer.write(frame)
            await asyncio.wait_for(writer.drain(), timeout=remaining)
        if not writer.can_write_eof():
            raise RuntimeError("source transport requires half-close")
        writer.write_eof()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("source deadline exceeded")
        header = await asyncio.wait_for(reader.readexactly(4), timeout=remaining)
        remaining = deadline - time.monotonic()
        frame = json.loads(await asyncio.wait_for(reader.readexactly(decode_length(header)), timeout=remaining))
        response = verify_response(rpc_key, env["nonce"], frame)
        if response.get("type") != "result" or response.get("data") != {"staged": True}:
            raise RuntimeError("source staging rejected")
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
