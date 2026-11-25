from __future__ import annotations

import asyncio
import hashlib
import hmac

import httpx2
import pytest
from aiagent_mcp_adapter.egress import (
    EgressHttpTransport,
    EgressRequest,
    EgressResponse,
    ForwardProxyEgress,
    parse_target,
)
from aiagent_mcp_adapter.errors import ErrorCode, McpError


class ProxyServer:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.requests: list[bytes] = []
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            content_length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    content_length = int(line.split(b":", 1)[1])
            body = await reader.readexactly(content_length) if content_length else b""
            self.requests.append(head + body)
            writer.write(self.response)
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


class FakeIssuer:
    async def issue(self, **_: object) -> str:
        return "permit-1"


def request() -> EgressRequest:
    return EgressRequest(
        method="POST",
        target=parse_target("https://mcp.example.com/mcp"),
        headers=(("Content-Type", "application/json"),),
        body=b"{}",
    )


async def send(response: bytes, *, max_bytes: int = 1024):
    proxy = ProxyServer(response)
    await proxy.start()
    try:
        egress = ForwardProxyEgress(
            f"http://127.0.0.1:{proxy.port}",
            permit_issuer=FakeIssuer(),
            allowed_origins=frozenset({"https://mcp.example.com:443"}),
        )
        result = await egress.send(request(), permit="permit-1", timeout_s=2.0, max_bytes=max_bytes)
        return result, proxy
    finally:
        await proxy.stop()


async def test_absolute_form_request_and_permit_header() -> None:
    result, proxy = await send(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
    assert result.status == 200
    assert result.body == b"{}"
    head = proxy.requests[0]
    assert head.startswith(b"POST https://mcp.example.com/mcp HTTP/1.1\r\n")
    assert b"Proxy-Authorization: Aiagent tool_gateway:permit-1\r\n" in head
    assert b"Host: mcp.example.com\r\n" in head
    assert b"Content-Length: 2\r\n" in head
    assert head.endswith(b"\r\n\r\n{}")


async def test_redirects_are_never_followed() -> None:
    proxy = ProxyServer(b"HTTP/1.1 302 Found\r\nLocation: https://evil.example/\r\nContent-Length: 0\r\n\r\n")
    await proxy.start()
    try:
        egress = ForwardProxyEgress(
            f"http://127.0.0.1:{proxy.port}",
            permit_issuer=FakeIssuer(),
            allowed_origins=frozenset({"https://mcp.example.com:443"}),
        )
        with pytest.raises(McpError) as err:
            await egress.send(request(), permit="permit-1", timeout_s=2.0, max_bytes=1024)
        assert err.value.code is ErrorCode.EGRESS_DENIED
        assert len(proxy.requests) == 1
    finally:
        await proxy.stop()


async def test_authenticated_proxy_denial_is_pre_send_but_spoofed_is_not() -> None:
    code = "PRE_SEND_DENIED"
    mac = hmac.new(hashlib.sha256(b"permit-1").digest(), f"DENIED:{code}".encode(), hashlib.sha256).hexdigest()
    denial = (
        b"HTTP/1.1 403 Forbidden\r\n"
        b"x-aiagent-egress: denied\r\n"
        + f"x-aiagent-egress-proof: {code}:{mac}\r\n".encode()
        + b"Content-Length: 0\r\n\r\n"
    )
    with pytest.raises(McpError) as err:
        await send(denial)
    assert err.value.code is ErrorCode.EGRESS_DENIED
    assert err.value.effect_uncertain is False

    spoofed = (
        b"HTTP/1.1 403 Forbidden\r\n"
        b"x-aiagent-egress: denied\r\n"
        b"x-aiagent-egress-proof: PRE_SEND_DENIED:deadbeef\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    with pytest.raises(McpError) as err:
        await send(spoofed)
    assert err.value.code is ErrorCode.HTTP_FORBIDDEN


async def test_response_size_is_bounded() -> None:
    result, _ = await send(
        b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n1234567890",
        max_bytes=4,
    )
    assert result.body == b"1234"
    assert result.truncated is True


async def test_chunked_response() -> None:
    result, _ = await send(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nabcd\r\n0\r\n\r\n")
    assert result.body == b"abcd"
    assert result.truncated is False


def test_plain_http_outside_loopback_is_refused() -> None:
    with pytest.raises(McpError) as err:
        parse_target("http://mcp.example.com/mcp")
    assert err.value.code is ErrorCode.ENDPOINT_DENIED


class FakeEgress:
    def __init__(self) -> None:
        self.calls: list[EgressRequest] = []

    async def send(self, request: EgressRequest, *, permit: str, timeout_s: float, max_bytes: int) -> EgressResponse:
        self.calls.append(request)
        return EgressResponse(status=200, headers=(("content-type", "application/json"),), body=b"{}")


async def test_httpx_transport_forces_requests_through_egress() -> None:
    egress = FakeEgress()

    async def permit_for(request: EgressRequest) -> str:
        return "permit-9"

    transport = EgressHttpTransport(
        egress,
        permit_for=permit_for,
        allowed_origins=frozenset({"https://mcp.example.com:443"}),
        max_bytes=1024,
        timeout_s=2.0,
    )
    async with httpx2.AsyncClient(transport=transport, follow_redirects=False, trust_env=False) as client:
        response = await client.post("https://mcp.example.com/mcp", json={"x": 1})
    assert response.status_code == 200
    assert len(egress.calls) == 1
    assert egress.calls[0].target.url == "https://mcp.example.com/mcp"
    assert egress.calls[0].body
