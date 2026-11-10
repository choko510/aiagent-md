"""Authenticated local RPC between trusted processes (System Architecture §13.2 rule 2).

Transport: Unix domain socket (production; peer uid checked with SO_PEERCRED) or loopback TCP (development / non-Linux).
Authentication: per-caller HMAC keys (aiagent_domain.rpcwire) + replay protection + per-method caller ACL. Tenant
authority is NOT taken from the caller: every handler re-validates the RunClaim against the database.
A response may be a single result or a stream (`chunk` frames followed by one `result`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import struct
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiagent_domain.rpcwire import (
    NonceCache,
    RpcAuthError,
    RpcError,
    decode_length,
    encode_frame,
    sign_request,
    sign_response,
    verify_request,
    verify_response,
)

log = logging.getLogger("aiagent.rpc")
Handler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]] | AsyncIterator[dict[str, Any]]]


@dataclass
class Method:
    handler: Handler
    callers: frozenset[str]
    streaming: bool = False


@dataclass
class Endpoint:
    """`unix:/run/aiagent/tool.sock` or `tcp:127.0.0.1:7101`."""

    spec: str

    @property
    def kind(self) -> str:
        return self.spec.split(":", 1)[0]

    @property
    def path(self) -> str:
        return self.spec.split(":", 1)[1]

    @property
    def host_port(self) -> tuple[str, int]:
        host, port = self.spec.split(":", 1)[1].rsplit(":", 1)
        return host, int(port)


def parse_keys(spec: str) -> dict[str, bytes]:
    """`caller=hexkey,caller2=hexkey2` (from a secret file). Keys must be >= 32 bytes."""
    out: dict[str, bytes] = {}
    for raw in spec.replace("\n", ",").split(","):
        part = raw.strip()
        if not part:
            continue
        name, _, hexkey = part.partition("=")
        key = bytes.fromhex(hexkey.strip())
        if len(key) < 32:
            raise ValueError("rpc keys must be at least 32 bytes")
        out[name.strip()] = key
    return out


async def _read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    import json

    header = await reader.readexactly(4)
    n = decode_length(header)
    data = await reader.readexactly(n)
    obj = json.loads(data)
    if not isinstance(obj, dict):
        raise RpcError("BAD_FRAME")
    return obj


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    sock = writer.get_extra_info("socket")
    if sock is None or sys.platform != "linux" or sock.family != socket.AF_UNIX:
        return None
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return int(uid)


@dataclass
class RpcServer:
    name: str
    endpoint: Endpoint
    keys: dict[str, bytes]
    methods: dict[str, Method] = field(default_factory=dict)
    allowed_uids: dict[str, set[int]] = field(default_factory=dict)  # caller -> OS uids (unix sockets)
    _server: asyncio.base_events.Server | None = None
    _nonces: NonceCache = field(default_factory=NonceCache)
    _inflight: set[asyncio.Task[Any]] = field(default_factory=set)

    def register(self, method: str, handler: Handler, callers: set[str], *, streaming: bool = False) -> None:
        self.methods[method] = Method(handler, frozenset(callers), streaming)

    async def start(self) -> None:
        if self.endpoint.kind == "unix":
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.endpoint.path)
            self._server = await asyncio.start_unix_server(self._handle, path=self.endpoint.path)  # type: ignore[attr-defined]
            os.chmod(self.endpoint.path, 0o660)
        else:
            host, port = self.endpoint.host_port
            self._server = await asyncio.start_server(self._handle, host, port)

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    async def close(self, grace_s: float = 10.0) -> None:
        if self._server is not None:
            self._server.close()
        if self._inflight:
            await asyncio.wait(self._inflight, timeout=grace_s)
        if self._server is not None:
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._inflight.add(task)
        caller = "?"
        try:
            env = await asyncio.wait_for(_read_frame(reader), timeout=10)
            try:
                caller, method, body, nonce = verify_request(self.keys, env, self._nonces)
            except RpcAuthError as e:
                log.warning("rpc auth failed", extra={"fields": {"server": self.name, "code": str(e)}})
                writer.write(encode_frame({"type": "error", "code": "UNAUTHENTICATED"}))
                return
            uid = _peer_uid(writer)
            if caller in self.allowed_uids and (uid is None or uid not in self.allowed_uids[caller]):
                writer.write(
                    encode_frame(sign_response(self.keys[caller], nonce, {"type": "error", "code": "PEER_IDENTITY"}))
                )
                return
            m = self.methods.get(method)
            if m is None or caller not in m.callers:
                writer.write(
                    encode_frame(sign_response(self.keys[caller], nonce, {"type": "error", "code": "FORBIDDEN_METHOD"}))
                )
                return
            key = self.keys[caller]
            try:
                if m.streaming:
                    agen = m.handler(caller, body)
                    assert hasattr(agen, "__aiter__")
                    async for item in agen:  # type: ignore[union-attr]
                        frame = (
                            {"type": "chunk", "data": item}
                            if item.get("__final__") is None
                            else {"type": "result", "data": {k: v for k, v in item.items() if k != "__final__"}}
                        )
                        writer.write(encode_frame(sign_response(key, nonce, frame)))
                        await writer.drain()
                else:
                    res = await m.handler(caller, body)  # type: ignore[misc]
                    writer.write(encode_frame(sign_response(key, nonce, {"type": "result", "data": res})))
            except RpcError as e:
                writer.write(
                    encode_frame(
                        sign_response(key, nonce, {"type": "error", "code": e.code, "message": e.message[:500]})
                    )
                )
            except Exception as e:
                log.exception(
                    "rpc handler failed",
                    extra={"fields": {"server": self.name, "method": method, "exc": type(e).__name__}},
                )
                writer.write(encode_frame(sign_response(key, nonce, {"type": "error", "code": "INTERNAL"})))
            await writer.drain()
        except (asyncio.IncompleteReadError, TimeoutError, ConnectionError, RpcError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
            if task is not None:
                self._inflight.discard(task)


class RpcClient:
    def __init__(self, endpoint: str, caller: str, key: bytes, *, timeout_s: float = 120.0) -> None:
        self.endpoint, self.caller, self.key, self.timeout_s = Endpoint(endpoint), caller, key, timeout_s

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self.endpoint.kind == "vsock":  # Trusted Control VM -> Untrusted Execution VM (no IP route needed)
            cid, vport = self.endpoint.path.split(":")
            sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
            sock.setblocking(False)
            await asyncio.get_running_loop().sock_connect(sock, (int(cid), int(vport)))
            return await asyncio.open_connection(sock=sock)
        if self.endpoint.kind == "unix":
            return await asyncio.open_unix_connection(self.endpoint.path)  # type: ignore[attr-defined,no-any-return]
        host, port = self.endpoint.host_port
        return await asyncio.open_connection(host, port)

    async def call(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] | None = None
        async for kind, data in self.stream(method, body):
            if kind == "result":
                result = data
        if result is None:
            raise RpcError("NO_RESULT")
        return result

    async def stream(self, method: str, body: dict[str, Any]) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        env = sign_request(self.key, self.caller, method, body)
        reader, writer = await asyncio.wait_for(self._open(), timeout=10)
        try:
            writer.write(encode_frame(env))
            await writer.drain()
            while True:
                frame = await asyncio.wait_for(_read_frame(reader), timeout=self.timeout_s)
                if frame.get("type") == "error" and "mac" not in frame:
                    raise RpcAuthError(str(frame.get("code")))
                data = verify_response(self.key, env["nonce"], frame)
                if data["type"] == "error":
                    raise RpcError(str(data.get("code")), str(data.get("message", "")))
                if data["type"] == "chunk":
                    yield "chunk", data["data"]
                    continue
                yield "result", data["data"]
                return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
