"""Faults at the authenticated artifact transport boundary, not production E2E."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest
from aiagent_domain.rpcwire import NonceCache, RpcAuthError, decode_length, encode_frame, sign_response, verify_request
from aiagent_gateways.source_transfer import send_source_for_capture
from aiagent_sandbox.binary_transfer import TransferError, encode_chunks
from aiagent_sandbox.source_ticket import artifact_binding, artifact_transfer_key

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Unix socket and private artifact FD")


@pytest.mark.parametrize(
    "fault", ["ok", "tampered", "truncated", "extra", "oversized", "wrong-binding", "forged-header", "revoked"]
)
async def test_artifact_rpc_validates_complete_bounded_transfer(tmp_path: Path, fault: str) -> None:
    rpc_key, source_key, binding = b"r" * 32, b"s" * 32, b"b" * 32
    data, proof = b"fixture-index-bytes", b"fixture-receipt"
    digest = hashlib.sha256(data).hexdigest()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    live = [True]

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            n = decode_length(await reader.readexactly(4))
            env = json.loads(await reader.readexactly(n))
            _, _, _, nonce = verify_request({"tool_gateway": rpc_key}, env, NonceCache())
            await reader.read()
            header = {
                "size": 4194305 if fault == "oversized" else len(data),
                "sha256": digest,
                "proof": base64.b64encode(proof).decode("ascii"),
            }
            response_key = b"x" * 32 if fault == "forged-header" else rpc_key
            writer.write(encode_frame(sign_response(response_key, nonce, {"type": "artifact", "data": header})))
            if fault == "revoked":
                await writer.drain()
                await asyncio.sleep(0.5)
            selected_binding = b"x" * 32 if fault == "wrong-binding" else binding
            wire = b"".join(
                encode_chunks(
                    artifact_transfer_key(source_key), artifact_binding(selected_binding, digest, len(data)), [data]
                )
            )
            if fault == "tampered":
                wire = wire[:12] + bytes([wire[12] ^ 1]) + wire[13:]
            elif fault == "truncated":
                wire = wire[:-1]
            elif fault == "extra":
                wire += b"extra"
            writer.write(wire)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    socket = tmp_path / "artifact.sock"
    server = await asyncio.start_unix_server(handle, path=socket)
    try:

        async def receive() -> tuple[bytes, bytes]:
            async with send_source_for_capture(
                f"unix:{socket}",
                rpc_key,
                "session",
                source_key,
                binding,
                [b"source"],
                private,
                deadline=time.monotonic() + 5,
                active=lambda: live[0],
            ) as (fd, receipt):
                assert os.fstat(fd).st_nlink == 0
                return os.read(fd, 1024), receipt

        if fault == "ok":
            assert await receive() == (data, proof)
        else:
            if fault == "revoked":
                asyncio.get_running_loop().call_later(0.05, lambda: live.__setitem__(0, False))
            with pytest.raises((ValueError, TransferError, RpcAuthError, ConnectionError, asyncio.IncompleteReadError)):
                await receive()
            if fault == "revoked":
                await asyncio.sleep(0.5)
        assert not list(private.iterdir())
    finally:
        server.close()
        await server.wait_closed()


async def test_revocation_cancels_stalled_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from aiagent_core.rpc import RpcClient

    entered, closed = asyncio.Event(), asyncio.Event()
    live = [True]

    async def stalled_open(_self):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(RpcClient, "_open", stalled_open)

    async def receive() -> None:
        async with send_source_for_capture(
            "unix:/unused",
            b"r" * 32,
            "session",
            b"s" * 32,
            b"b" * 32,
            [b"source"],
            tmp_path,
            deadline=time.monotonic() + 5,
            active=lambda: live[0],
        ):
            pytest.fail("revoked connection exposed capture")

    task = asyncio.create_task(receive())
    await asyncio.wait_for(entered.wait(), 1)
    live[0] = False
    with pytest.raises(TimeoutError, match="authority expired"):
        await asyncio.wait_for(task, 1)
    assert closed.is_set()
