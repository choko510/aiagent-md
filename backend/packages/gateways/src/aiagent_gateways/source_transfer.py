"""Client for an explicitly registered, one-shot source staging session.

This is not a production dispatcher: the caller must establish canonical
authority and arrange registration with the remote supervisor separately.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import os
import stat
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from aiagent_core.rpc import RpcClient
from aiagent_development.code_intelligence.git_proof import GitTreeProof
from aiagent_development.code_intelligence.projection import ProjectionManifest
from aiagent_development.code_intelligence.projection_stage import _open_trusted_parent
from aiagent_domain.rpcwire import decode_length, encode_frame, sign_request, verify_response
from aiagent_sandbox.binary_transfer import encode_chunks, receive_async
from aiagent_sandbox.source_ticket import (
    artifact_binding,
    artifact_transfer_key,
    source_binding,
    source_transfer_key,
    verify_source_ticket,
)

if TYPE_CHECKING:
    from aiagent_development.workspace_authority import PreparedExecution

    from .execution_artifacts import ExecutionArtifactRef, PostgresExecutionArtifactStore
    from .execution_lifecycle import ExecutionLifecycle


class SourceRegistrationUnknown(RuntimeError):
    """Registration may have reached the Supervisor; never retry automatically."""


class SourceCaptureUnknown(RuntimeError):
    """Execution or publication may have completed; never retry automatically."""


async def cancel_issued_source(
    lifecycle: ExecutionLifecycle,
    prepared: PreparedExecution,
    endpoint: str,
    rpc_key: bytes,
    ticket_key: bytes,
    ticket: dict[str, Any],
    *,
    deadline: float,
) -> bool:
    """Fence a session, including a cancellation racing its registration."""
    p = verify_source_ticket(ticket_key, ticket)
    if p["execution"] != prepared.id or p["binding_sha256"] != prepared.binding_sha256:
        raise ValueError("source ticket execution mismatch")
    confirmed = False
    try:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            result = await asyncio.wait_for(
                RpcClient(endpoint, "tool_gateway", rpc_key).call("source.cancel", {"ticket": ticket}),
                timeout=remaining,
            )
            confirmed = result.get("cancelled") is True
    finally:
        try:
            await asyncio.shield(
                asyncio.to_thread(
                    lifecycle.mark_unknown, prepared.id, prepared.binding_sha256, deadline=time.monotonic() + 5
                )
            )
        except Exception as exc:
            raise SourceRegistrationUnknown("source cancellation outcome requires reconciliation") from exc
    return confirmed


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
        await _send_frames(writer, env, transfer_key, binding, chunks, deadline)
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


async def _send_frames(
    writer: asyncio.StreamWriter,
    env: dict[str, Any],
    transfer_key: bytes,
    binding: bytes,
    chunks: Iterable[bytes],
    deadline: float,
    active: Callable[[], bool] = lambda: True,
) -> None:
    writer.write(encode_frame(env))
    for frame in encode_chunks(transfer_key, binding, chunks):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not active():
            raise TimeoutError("source deadline exceeded")
        writer.write(frame)
        draining = asyncio.create_task(writer.drain())
        try:
            while not draining.done():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not active():
                    raise TimeoutError("source authority expired")
                try:
                    await asyncio.wait_for(asyncio.shield(draining), timeout=min(remaining, 0.1))
                except TimeoutError:
                    continue
            await draining
        finally:
            if not draining.done():
                draining.cancel()
                await asyncio.gather(draining, return_exceptions=True)
    if not writer.can_write_eof():
        raise RuntimeError("source transport requires half-close")
    writer.write_eof()


async def _read_active(reader: asyncio.StreamReader, size: int, deadline: float, active: Callable[[], bool]) -> bytes:
    data = bytearray()
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not active():
            raise TimeoutError("artifact authority expired")
        try:
            block = await asyncio.wait_for(reader.read(size - len(data)), timeout=min(remaining, 0.1))
        except TimeoutError:
            continue
        if not block:
            raise asyncio.IncompleteReadError(bytes(data), size)
        data.extend(block)
    return bytes(data)


@contextlib.asynccontextmanager
async def send_source_for_capture(
    endpoint: str,
    rpc_key: bytes,
    session_id: str,
    transfer_key: bytes,
    binding: bytes,
    chunks: Iterable[bytes],
    private_parent: Path,
    *,
    deadline: float,
    active: Callable[[], bool],
) -> AsyncIterator[tuple[int, bytes]]:
    """Return a private read-only Artifact FD only after complete authenticated transfer.

    The receipt and canonical execution authority still require Gateway
    verification and a fenced FINISHED/publication transition.
    """
    env = sign_request(rpc_key, "tool_gateway", "source.receive", {"sessionId": session_id})
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not active():
        raise TimeoutError("source authority expired")
    reader, writer = await asyncio.wait_for(RpcClient(endpoint, "tool_gateway", rpc_key)._open(), timeout=remaining)
    parent_fd = -1
    try:
        parent_fd = _open_trusted_parent(private_parent)
        parent_info = os.fstat(parent_fd)
        if parent_info.st_uid != os.geteuid() or stat.S_IMODE(parent_info.st_mode) & 0o077:
            raise ValueError("artifact parent is not private")
        await _send_frames(writer, env, transfer_key, binding, chunks, deadline, active)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not active():
            raise TimeoutError("source authority expired")
        header = await _read_active(reader, 4, deadline, active)
        frame = json.loads(await _read_active(reader, decode_length(header), deadline, active))
        response = verify_response(rpc_key, env["nonce"], frame)
        data = response.get("data")
        if response.get("type") != "artifact" or not isinstance(data, dict) or set(data) != {"size", "sha256", "proof"}:
            raise ValueError("artifact response invalid")
        size, digest, encoded_proof = data["size"], data["sha256"], data["proof"]
        if type(size) is not int or not 1 <= size <= 4 * 1024 * 1024 or type(encoded_proof) is not str:
            raise ValueError("artifact response invalid")
        proof = base64.b64decode(encoded_proof, validate=True)
        if not 1 <= len(proof) <= 16384:
            raise ValueError("artifact response invalid")
        transfer_binding = artifact_binding(binding, digest, size)
        with tempfile.TemporaryFile(dir=f"/proc/self/fd/{parent_fd}") as quarantine:
            await receive_async(
                reader,
                quarantine,
                key=artifact_transfer_key(transfer_key),
                binding=transfer_binding,
                expected_size=size,
                expected_sha256=digest,
                deadline=deadline,
                active=active,
            )
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not active():
                    raise TimeoutError("artifact authority expired")
                try:
                    extra = await asyncio.wait_for(reader.read(1), timeout=min(remaining, 0.1))
                    break
                except TimeoutError:
                    continue
            if extra:
                raise ValueError("extra artifact data")
            quarantine.flush()
            fd = os.open(f"/proc/self/fd/{quarantine.fileno()}", os.O_RDONLY)
            try:
                yield fd, proof
            finally:
                os.close(fd)
    finally:
        if parent_fd != -1:
            os.close(parent_fd)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _complete_db_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Keep the capture FD open until an in-flight DB operation has stopped."""
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await task
        raise


async def capture_finish_publish(
    lifecycle: ExecutionLifecycle,
    store: PostgresExecutionArtifactStore,
    prepared: PreparedExecution,
    endpoint: str,
    rpc_key: bytes,
    ticket_key: bytes,
    ticket: dict[str, Any],
    session_id: str,
    transfer_key: bytes,
    binding: bytes,
    chunks: Iterable[bytes],
    private_parent: Path,
    *,
    deadline: float,
    active: Callable[[], bool],
) -> ExecutionArtifactRef:
    """Fence, validate and atomically publish the actual authenticated capture.

    Caller must supply the still-live canonical authority watcher and a session
    already authorized for the same prepared execution. This does not wire the
    Tool Gateway or establish a Supervisor watchdog.
    """
    p = verify_source_ticket(ticket_key, ticket)
    expected_binding = source_binding(
        p["session"],
        p["execution"],
        p["attempt"],
        p["binding_sha256"],
        p["snapshot"],
        p["source_bytes"],
        p["source_sha256"],
    )
    if (
        p["execution"] != prepared.id
        or p["attempt"] != prepared.attempt_id
        or p["snapshot"] != prepared.snapshot_id
        or p["recipe"] != prepared.recipe_id
        or p["binding_sha256"] != prepared.binding_sha256
        or p["session"] != session_id
        or not hmac.compare_digest(binding, expected_binding)
        or not hmac.compare_digest(transfer_key, source_transfer_key(ticket_key, session_id))
    ):
        raise ValueError("capture session mismatch")
    try:
        async with send_source_for_capture(
            endpoint,
            rpc_key,
            session_id,
            transfer_key,
            binding,
            chunks,
            private_parent,
            deadline=deadline,
            active=active,
        ) as (fd, proof):
            if not active():
                raise RuntimeError("capture authority revoked")
            await _complete_db_call(lifecycle.finish, prepared.id, fd, proof, deadline=deadline)
            if not active():
                raise RuntimeError("capture authority revoked")
            return cast(
                "ExecutionArtifactRef",
                await _complete_db_call(store.publish, prepared.id, fd, proof, deadline=deadline),
            )
    except (Exception, asyncio.CancelledError) as exc:
        try:
            await asyncio.shield(
                asyncio.to_thread(
                    lifecycle.mark_unknown, prepared.id, prepared.binding_sha256, deadline=time.monotonic() + 5
                )
            )
        except Exception as mark_exc:
            raise SourceCaptureUnknown("capture or publication outcome requires reconciliation") from mark_exc
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise SourceCaptureUnknown("capture or publication outcome unknown") from exc
