"""Client for an explicitly registered, one-shot source staging session.

This is not a production dispatcher: the caller must establish canonical
authority and arrange registration with the remote supervisor separately.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Iterable

from aiagent_core.rpc import RpcClient
from aiagent_domain.rpcwire import decode_length, encode_frame, sign_request, verify_response
from aiagent_sandbox.binary_transfer import encode_chunks


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
