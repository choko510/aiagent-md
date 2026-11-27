"""Real MCP SDK protocol test over a real HTTP server.

The client is the production adapter transport. Its only network path is a test forward proxy, which
proves the adapter sends absolute-form requests and never connects to the origin directly. The proxy is
test scaffolding, not production code.
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import pytest
import uvicorn
from aiagent_mcp_adapter.canonical import digest
from aiagent_mcp_adapter.config import AdapterLimits, ProviderConfig
from aiagent_mcp_adapter.egress import ForwardProxyEgress
from aiagent_mcp_adapter.errors import ErrorCode, McpError
from aiagent_mcp_adapter.sdk_transport import SdkStreamableHttpTransport
from aiagent_mcp_adapter.transport import Credential
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import ServerRequestContext
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount


@dataclass
class ForwardingProxy:
    """Minimal HTTP forward proxy: absolute-form in, origin-form out, records every request head."""

    server: asyncio.AbstractServer | None = None
    port: int = 0
    requests: list[bytes] = None  # type: ignore[assignment]
    reject_tool_calls: bool = False

    def __post_init__(self) -> None:
        self.requests = []

    async def start(self) -> None:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head[:-4].split(b"\r\n")
            method, target, version = lines[0].decode("latin-1").split(" ")
            content_length = 0
            for line in lines[1:]:
                if line.lower().startswith(b"content-length:"):
                    content_length = int(line.split(b":", 1)[1])
            body = await reader.readexactly(content_length) if content_length else b""
            self.requests.append(head + body)
            if self.reject_tool_calls and b"tools/call" in body:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            url = urlsplit(target)
            host = url.hostname or ""
            port = url.port or (443 if url.scheme == "https" else 80)
            path = url.path or "/"
            if url.query:
                path += "?" + url.query
            upstream = [f"{method} {path} {version}"]
            for line in lines[1:]:
                lowered = line.lower()
                if lowered.startswith((b"proxy-authorization:", b"proxy-connection:", b"connection:")):
                    continue
                upstream.append(line.decode("latin-1"))
            upstream.append("Connection: close")
            origin_reader, origin_writer = await asyncio.open_connection(host, port)
            try:
                origin_writer.write(("\r\n".join(upstream) + "\r\n\r\n").encode("latin-1") + body)
                await origin_writer.drain()
                response = await origin_reader.read()
            finally:
                origin_writer.close()
                await origin_writer.wait_closed()
            writer.write(response)
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


class FakeMcpServer:
    def __init__(self, *, paginate: bool = True) -> None:
        self.paginate = paginate
        self.list_calls = 0
        self.call_calls: list[tuple[str, dict]] = []

        async def list_tools(ctx: ServerRequestContext[Any], params: Any) -> types.ListToolsResult:
            self.list_calls += 1
            cursor = getattr(params, "cursor", None) if params is not None else None
            if self.paginate and cursor is None:
                return types.ListToolsResult(
                    tools=[
                        types.Tool(
                            name="echo",
                            description="Echo text back.",
                            inputSchema={
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                                "additionalProperties": False,
                            },
                        )
                    ],
                    nextCursor="page-2",
                )
            return types.ListToolsResult(
                tools=[
                    types.Tool(
                        name="other",
                        description="Another tool.",
                        inputSchema={"type": "object", "properties": {}},
                    )
                ]
            )

        async def call_tool(
            ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
        ) -> types.CallToolResult:
            self.call_calls.append((params.name, dict(params.arguments or {})))
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"echo:{params.arguments['text']}")],
                structuredContent={"receipt": "rcpt-e2e"},
            )

        self.server = Server(
            "adapter-e2e",
            version="1.0.0",
            instructions="test server",
            on_list_tools=list_tools,
            on_call_tool=call_tool,
        )


@asynccontextmanager
async def running_server(*, paginate: bool = True):
    test_server = FakeMcpServer(paginate=paginate)
    manager = StreamableHTTPSessionManager(app=test_server.server, json_response=True, stateless=False)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with manager.run():
            yield

    app = Starlette(routes=[Mount("/mcp", app=manager.handle_request)], lifespan=lifespan)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    uv = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    task = asyncio.create_task(uv.serve(sockets=[sock]))
    try:
        for _ in range(500):
            if uv.started:
                break
            await asyncio.sleep(0.01)
        if not uv.started:
            raise RuntimeError("uvicorn did not start")
        yield test_server, port
    finally:
        uv.should_exit = True
        await asyncio.wait_for(task, timeout=10)


class FakeIssuer:
    def __init__(self) -> None:
        self.purposes: list[str] = []

    async def issue(self, *, purpose: str, **_: Any) -> str:
        self.purposes.append(purpose)
        return f"control-{len(self.purposes)}"


class FakeBroker:
    def __init__(self, credential: Credential | None = None) -> None:
        self.credential = credential

    async def resolve(self, provider: ProviderConfig, *, run_id: str | None = None) -> Credential | None:
        return self.credential


def make_provider(port: int, *, auth: bool = False) -> ProviderConfig:
    data: dict[str, Any] = {
        "provider_id": "demo",
        "tenant_id": "tenant-a",
        "owner_user_id": "owner-a",
        "display_name": "Loopback",
        "endpoint": f"http://127.0.0.1:{port}/mcp/",
        "enabled": True,
        "allow_loopback_for_tests": True,
    }
    if auth:
        data.update(auth_mode="bearer", credential_binding_ref="binding-1", credential_version=2)
    return ProviderConfig(**data)


def make_transport(proxy_port: int, origin: str, *, max_bytes: int = 1024 * 1024):
    issuer = FakeIssuer()
    egress = ForwardProxyEgress(
        f"http://127.0.0.1:{proxy_port}",
        permit_issuer=issuer,
        allowed_origins=frozenset({origin}),
        allow_loopback=True,
    )
    return SdkStreamableHttpTransport(egress, permit_issuer=issuer), egress


@pytest.fixture(autouse=True)
def test_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIAGENT_PROFILE", "test")
    monkeypatch.setenv("MCP_ADAPTER_ENABLED", "1")


async def test_initialize_paginated_list_and_call(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = ForwardingProxy()
    await proxy.start()
    try:
        async with running_server() as (server, port):
            provider = make_provider(port)
            transport, _ = make_transport(proxy.port, provider.endpoint_origin.origin)
            async with transport.open(
                provider=provider,
                credential=None,
                on_list_changed=lambda: None,
                limits=AdapterLimits(),
            ) as session:
                first = await session.list_page(None, timeout_s=5.0)
                assert [t["name"] for t in first.tools] == ["echo"]
                assert first.next_cursor == "page-2"
                second = await session.list_page("page-2", timeout_s=5.0)
                assert [t["name"] for t in second.tools] == ["other"]
            async with transport.open(
                provider=provider,
                credential=None,
                on_list_changed=lambda: None,
                limits=AdapterLimits(),
                permit="gateway-admitted-permit",
                effect_tool="echo",
                effect_arguments_digest=digest({"text": "hello"}),
            ) as session:
                raw = await session.call_tool("echo", {"text": "hello"}, timeout_s=5.0)
                assert raw["content"][0]["text"] == "echo:hello"
                assert raw["structuredContent"] == {"receipt": "rcpt-e2e"}
            assert server.list_calls == 2
            assert server.call_calls == [("echo", {"text": "hello"})]
            assert proxy.requests[0].startswith(b"POST http://127.0.0.1:")
            effect_posts = [
                request
                for request in proxy.requests
                if b"Proxy-Authorization: Aiagent tool_gateway:gateway-admitted-permit" in request
            ]
            assert len(effect_posts) == 1
            assert b'"method":"tools/call"' in effect_posts[0]
            purposes = transport._permit_issuer.purposes
            assert purposes.count("mcp_initialize") >= 1
            assert purposes.count("mcp_list") == 2
            assert "mcp_notification" in purposes
            assert set(purposes) <= {
                "mcp_initialize",
                "mcp_notification",
                "mcp_list",
                "mcp_stream",
                "mcp_close",
            }
    finally:
        await proxy.stop()


async def test_credential_is_injected_only_into_the_egress_request() -> None:
    proxy = ForwardingProxy()
    await proxy.start()
    try:
        async with running_server(paginate=False) as (_, port):
            provider = make_provider(port, auth=True)
            transport, _ = make_transport(proxy.port, provider.endpoint_origin.origin)
            async with transport.open(
                provider=provider,
                credential=Credential("Authorization", "secret-value", version=2),
                on_list_changed=lambda: None,
                limits=AdapterLimits(),
            ) as session:
                await session.list_page(None, timeout_s=5.0)
            assert any(b"Authorization: Bearer secret-value" in r for r in proxy.requests)
    finally:
        await proxy.stop()


async def test_stale_credential_is_refused_before_any_request() -> None:
    proxy = ForwardingProxy()
    await proxy.start()
    try:
        async with running_server(paginate=False) as (_, port):
            provider = make_provider(port, auth=True)
            transport, _ = make_transport(proxy.port, provider.endpoint_origin.origin)
            with pytest.raises(McpError) as err:
                async with transport.open(
                    provider=provider,
                    credential=Credential("Authorization", "old", version=1),
                    on_list_changed=lambda: None,
                    limits=AdapterLimits(),
                ):
                    pass
            assert err.value.code is ErrorCode.CREDENTIAL_STALE
            assert proxy.requests == []
    finally:
        await proxy.stop()


def test_sdk_session_loss_is_classified_as_uncertain_for_calls() -> None:
    from aiagent_mcp_adapter.sdk_transport import _map_sdk_error

    error = McpError(ErrorCode.HTTP_SESSION_LOST, "session_terminated", effect_uncertain=True)
    assert error.effect_uncertain is True

    from mcp.shared.exceptions import MCPError as SdkError

    mapped = _map_sdk_error(SdkError(code=-32600, message="Session terminated"), context="call")
    assert mapped.code is ErrorCode.HTTP_SESSION_LOST
    assert mapped.effect_uncertain is True
    listed = _map_sdk_error(SdkError(code=-32600, message="Session terminated"), context="list")
    assert listed.code is ErrorCode.SESSION_FAILED
    assert listed.effect_uncertain is False


async def test_admitted_permit_is_used_instead_of_minting_another() -> None:
    proxy = ForwardingProxy()
    await proxy.start()
    try:
        async with running_server(paginate=False) as (server, port):
            provider = make_provider(port)
            transport, _ = make_transport(proxy.port, provider.endpoint_origin.origin)
            async with transport.open(
                provider=provider,
                credential=None,
                on_list_changed=lambda: None,
                limits=AdapterLimits(),
                permit="gateway-admitted-permit",
                effect_tool="echo",
                effect_arguments_digest=digest({"text": "permit"}),
            ) as session:
                await session.call_tool("echo", {"text": "permit"}, timeout_s=5.0)
            assert server.call_calls == [("echo", {"text": "permit"})]
            posts = [r for r in proxy.requests if r.startswith(b"POST ")]
            assert posts
            effect_posts = [
                r for r in posts if b"Proxy-Authorization: Aiagent tool_gateway:gateway-admitted-permit" in r
            ]
            assert len(effect_posts) == 1
            assert b'"method":"tools/call"' in effect_posts[0]
    finally:
        await proxy.stop()
