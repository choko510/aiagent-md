"""MCP Python SDK (streamable HTTP) adapter. The only module that imports `mcp`.

Design decisions tied to SDK 2.3.0 behaviour:
* `send_request` is used for `tools/list` and `tools/call` so the SDK's auto-caching/auto-relisting and
  automatic output-schema validation cannot alter listing or call semantics. The pinned schema is
  validated by the adapter itself.
* the SDK's own httpx2 client is supplied with `EgressHttpTransport`, so no request can bypass Sentinel.
* server instructions, sampling, roots and elicitation are not exposed or answered.
* stdio is not implemented at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError as SdkMcpError

from .canonical import digest
from .config import AdapterLimits, ProviderConfig
from .egress import EgressHttpTransport, EgressRequest, McpEgress, PermitIssuer
from .errors import ErrorCode, McpError
from .transport import Credential, SessionInfo, ToolPage, parse_tool_page

SUPPORTED_PROTOCOL_REVISIONS = frozenset(
    {
        "2025-06-18",
        "2025-11-25",
        "2026-07-28",
    }
)
_PRE_SEND_CODES = frozenset(
    {
        ErrorCode.ADAPTER_DISABLED,
        ErrorCode.CONFIG_INVALID,
        ErrorCode.ENDPOINT_DENIED,
        ErrorCode.EGRESS_PRE_SEND_DENIED,
        ErrorCode.CREDENTIAL_UNAVAILABLE,
        ErrorCode.CREDENTIAL_STALE,
        ErrorCode.PROTOCOL_VERSION_UNSUPPORTED,
        ErrorCode.CAPABILITY_MISSING,
        ErrorCode.BINDING_MISMATCH,
        ErrorCode.ARGUMENTS_INVALID,
        ErrorCode.POLICY_DENIED,
        ErrorCode.STALE_TOOL_REF,
    }
)
_EFFECT_UNCERTAIN_CODES = frozenset(
    {
        ErrorCode.TRANSPORT_TIMEOUT,
        ErrorCode.TRANSPORT_RESET,
        ErrorCode.HTTP_SESSION_LOST,
        ErrorCode.HTTP_SERVER_ERROR,
        ErrorCode.HTTP_UNEXPECTED_STATUS,
        ErrorCode.EFFECT_UNCERTAIN,
    }
)


def _uncertain(error: McpError) -> McpError:
    if error.effect_uncertain or error.code in _PRE_SEND_CODES:
        return error
    return McpError(
        error.code,
        error.detail,
        retryable=error.retryable,
        effect_uncertain=True,
    )


@dataclass
class SdkSession:
    """Plain-data view of one live MCP session. Owns no policy and no registry state."""

    _session: ClientSession
    info: SessionInfo
    _background_error: BaseException | None = field(default=None, repr=False)
    _session_id_digest: str | None = field(default=None, repr=False)

    def note_background_error(self, exc: BaseException) -> None:
        self._background_error = exc

    def _health(self) -> None:
        if self._background_error is not None:
            raise McpError(ErrorCode.SESSION_CLOSED, "transport_background_error") from self._background_error

    @property
    def protocol_revision(self) -> str:
        return self.info.protocol_revision

    @property
    def server_name(self) -> str:
        return self.info.server_name

    @property
    def server_version(self) -> str:
        return self.info.server_version

    @property
    def session_id_digest(self) -> str | None:
        return self._session_id_digest

    async def list_page(self, cursor: str | None, *, timeout_s: float) -> ToolPage:
        self._health()
        try:
            result = await self._session.send_request(
                types.ListToolsRequest(params=types.PaginatedRequestParams(cursor=cursor)),
                types.ListToolsResult,
                request_read_timeout_seconds=timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except McpError:
            raise
        except SdkMcpError as exc:
            raise _map_sdk_error(exc, context="list") from exc
        except Exception as exc:
            raise _map_transport_exception(exc, context="list") from exc
        raw = result.model_dump(by_alias=True, exclude_none=True)
        return parse_tool_page(raw)

    async def call_tool(self, name: str, arguments: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        self._health()
        try:
            result = await self._session.send_request(
                types.CallToolRequest(params=types.CallToolRequestParams(name=name, arguments=arguments)),
                types.CallToolResult,
                request_read_timeout_seconds=timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except McpError:
            raise
        except SdkMcpError as exc:
            raise _map_sdk_error(exc, context="call") from exc
        except Exception as exc:
            raise _map_transport_exception(exc, context="call") from exc
        raw = result.model_dump(by_alias=True, exclude_none=True)
        return {
            "content": raw.get("content", []),
            "structuredContent": raw.get("structuredContent"),
            "isError": bool(raw.get("isError", False)),
        }

    async def close(self) -> None:
        # The enclosing `streamable_http_client` context owns transport shutdown.
        await asyncio.sleep(0)


class SdkStreamableHttpTransport:
    """Opens one SDK session against one admin-approved endpoint through `McpEgress`."""

    def __init__(self, egress: McpEgress, *, permit_issuer: PermitIssuer) -> None:
        self._egress = egress
        self._permit_issuer = permit_issuer

    async def _issue_control(self, provider: ProviderConfig, target: Any, purpose: str) -> str:
        return await self._permit_issuer.issue(
            provider_id=provider.provider_id,
            tenant_id=provider.tenant_id,
            target=target,
            purpose=purpose,
        )

    @asynccontextmanager
    async def open(
        self,
        *,
        provider: ProviderConfig,
        credential: Credential | None,
        on_list_changed: Callable[[], Awaitable[None] | None],
        limits: AdapterLimits,
        permit: str | None = None,
        effect_tool: str | None = None,
        effect_arguments_digest: str | None = None,
        before_effect_send: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[SdkSession]:
        if provider.auth_mode != "none" and credential is None:
            raise McpError(ErrorCode.CREDENTIAL_UNAVAILABLE, "credential_required")
        if credential is not None and credential.version != provider.credential_version:
            raise McpError(ErrorCode.CREDENTIAL_STALE, "credential_version_mismatch")
        origin = provider.endpoint_origin.origin
        headers: dict[str, str] = {"User-Agent": "aiagent-mcp-adapter/0.1.0"}
        if credential is not None:
            header = provider.credential_header or ("Authorization" if provider.auth_mode == "bearer" else None)
            if header is None:
                raise McpError(ErrorCode.CONFIG_INVALID, "credential_header_missing")
            value = credential.header_value()
            headers[header] = f"Bearer {value}" if provider.auth_mode == "bearer" else value

        effect_spent = False
        list_changed = asyncio.Event()

        async def permit_for(request: EgressRequest) -> str:
            from .egress import EgressTarget

            nonlocal effect_spent
            target: EgressTarget = request.target
            if target.origin != origin:
                raise McpError(ErrorCode.EGRESS_DENIED, "origin_not_allowlisted")
            method = request.method.upper()
            if method == "GET":
                return await self._issue_control(provider, target, "mcp_stream")
            if method == "DELETE":
                return await self._issue_control(provider, target, "mcp_close")
            if method != "POST":
                raise McpError(ErrorCode.EGRESS_DENIED, "method_not_allowed")

            envelope = _jsonrpc_envelope(request.body)
            rpc_method = envelope.get("method")
            if not isinstance(rpc_method, str) or not rpc_method:
                raise McpError(ErrorCode.INVALID_JSONRPC, "missing_jsonrpc_method")
            if rpc_method == "tools/call":
                # The Gateway's effect permit is spendable for exactly one matching tools/call.
                # Initialization, notifications, list, GET and close always use separate control
                # permits, so a one-use Sentinel permit is never consumed before the effect.
                if permit is None or effect_tool is None or effect_arguments_digest is None:
                    raise McpError(ErrorCode.BINDING_MISMATCH, "effect_permit_not_bound")
                if effect_spent:
                    raise McpError(ErrorCode.BINDING_MISMATCH, "effect_permit_already_spent")
                if list_changed.is_set():
                    raise McpError(ErrorCode.STALE_TOOL_REF, "list_changed_before_effect")
                params = envelope.get("params")
                if not isinstance(params, dict):
                    raise McpError(ErrorCode.BINDING_MISMATCH, "tool_call_params_missing")
                if params.get("name") != effect_tool:
                    raise McpError(ErrorCode.BINDING_MISMATCH, "tool_name_mismatch")
                arguments = params.get("arguments", {})
                if not isinstance(arguments, dict) or digest(arguments) != effect_arguments_digest:
                    raise McpError(ErrorCode.BINDING_MISMATCH, "tool_arguments_digest_mismatch")
                if before_effect_send is not None:
                    await before_effect_send()
                effect_spent = True
                return permit
            # An admitted call must not be reusable as a broad control credential; every
            # non-effect request mints a narrowly-purposed control permit instead.
            return await self._issue_control(provider, target, _control_purpose(rpc_method))

        transport = EgressHttpTransport(
            self._egress,
            permit_for=permit_for,
            allowed_origins=frozenset({origin}),
            max_bytes=limits.max_response_bytes,
            timeout_s=limits.call_timeout_s,
            allow_loopback=provider.allow_loopback_for_tests,
            max_request_bytes=limits.max_request_bytes,
        )
        timeout = httpx2.Timeout(
            connect=limits.connect_timeout_s,
            read=limits.call_timeout_s,
            write=limits.call_timeout_s,
            pool=limits.connect_timeout_s,
        )
        http_client = httpx2.AsyncClient(
            transport=transport,
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )
        session_holder: dict[str, SdkSession] = {}

        async def handle_message(message: Any) -> None:
            if isinstance(message, types.ToolListChangedNotification):
                list_changed.set()
                result = on_list_changed()
                if inspect.isawaitable(result):
                    await result
                return
            if isinstance(message, BaseException):
                holder = session_holder.get("session")
                if holder is not None:
                    holder.note_background_error(message)

        # Enter the SDK context managers manually so an exception raised by the caller inside the
        # transport.open body is never rewritten as a transport/session error.
        stack = AsyncExitStack()
        try:
            await stack.enter_async_context(http_client)
            read_stream, write_stream = await stack.enter_async_context(
                streamable_http_client(
                    provider.endpoint,
                    http_client=http_client,
                    terminate_on_close=True,
                    max_sse_event_size=limits.max_sse_event_bytes,
                )
            )
            session = await stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    message_handler=handle_message,
                    client_info=types.Implementation(name="aiagent-mcp-adapter", version="0.1.0"),
                )
            )
            try:
                init = await asyncio.wait_for(session.initialize(), timeout=limits.connect_timeout_s)
            except TimeoutError as exc:
                raise McpError(ErrorCode.CONNECT_TIMEOUT, "initialize_timeout") from exc
            revision = init.protocol_version
            if revision not in SUPPORTED_PROTOCOL_REVISIONS:
                raise McpError(ErrorCode.PROTOCOL_VERSION_UNSUPPORTED, f"revision:{revision}")
            if init.capabilities is None or init.capabilities.tools is None:
                raise McpError(ErrorCode.CAPABILITY_MISSING, "tools_capability_absent")
            capabilities = init.capabilities.model_dump(by_alias=True, exclude_none=True)
            instructions = init.instructions or ""
            info = SessionInfo(
                protocol_revision=revision,
                server_name=init.server_info.name[:128],
                server_version=init.server_info.version[:64],
                capabilities=capabilities,
                instructions_digest=(
                    hashlib.sha256(instructions.encode("utf-8")).hexdigest() if instructions else None
                ),
            )
            sdk_session = SdkSession(_session=session, info=info)
            session_holder["session"] = sdk_session
        except McpError:
            await _close_quietly(stack)
            raise
        except ExceptionGroup as group:
            await _close_quietly(stack)
            raise _map_transport_exception(group, context="open") from group
        except Exception as exc:
            await _close_quietly(stack)
            raise _map_transport_exception(exc, context="open") from exc

        try:
            yield sdk_session
        except BaseException:
            await _close_quietly(stack)
            raise
        else:
            try:
                await stack.aclose()
            except McpError:
                raise
            except Exception as exc:
                raise _map_transport_exception(exc, context="open") from exc


async def _close_quietly(stack: AsyncExitStack) -> None:
    try:
        await stack.aclose()
    except Exception:
        # The mapped primary error is the useful fact; a cleanup error must not replace it.
        pass


def _map_sdk_error(error: SdkMcpError, *, context: str) -> McpError:
    code = getattr(error, "code", None)
    if code == -32601:
        return McpError(ErrorCode.CAPABILITY_MISSING, "method_not_found")
    if code == -32602:
        return McpError(
            ErrorCode.ARGUMENTS_INVALID if context == "call" else ErrorCode.INVALID_JSONRPC,
            "invalid_params",
            effect_uncertain=context == "call",
        )
    if code == -32600 and "session terminated" in str(getattr(error, "message", "")).lower():
        if context == "call":
            return McpError(ErrorCode.HTTP_SESSION_LOST, "session_terminated", effect_uncertain=True)
        return McpError(ErrorCode.SESSION_FAILED, "session_terminated")
    if code == -32000:
        return McpError(
            ErrorCode.SESSION_FAILED,
            "connection_closed",
            effect_uncertain=context == "call",
        )
    if code in (-32603, -32000, -32001, -32002) or (isinstance(code, int) and -32099 <= code <= -32000):
        return McpError(ErrorCode.TOOL_ERROR, "server_error", effect_uncertain=True)
    return McpError(ErrorCode.INVALID_JSONRPC, f"jsonrpc_error:{code}")


def _map_transport_exception(exc: BaseException, *, context: str) -> McpError:
    if isinstance(exc, McpError):
        return _uncertain(exc) if context == "call" else exc
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            mapped = _map_transport_exception(inner, context=context)
            if mapped.code not in (ErrorCode.SESSION_FAILED,):
                return mapped
        return McpError(ErrorCode.SESSION_FAILED, "exception_group", effect_uncertain=context == "call")
    if isinstance(exc, TimeoutError):
        return McpError(ErrorCode.TRANSPORT_TIMEOUT, "transport_timeout", effect_uncertain=context == "call")
    if isinstance(exc, httpx2.HTTPError):
        return McpError(ErrorCode.TRANSPORT_RESET, "http_transport_error", effect_uncertain=context == "call")
    if isinstance(exc, (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError)):
        return McpError(ErrorCode.TRANSPORT_RESET, "connection_lost", effect_uncertain=context == "call")
    text = type(exc).__name__
    if "EndOfStream" in text or "BrokenResource" in text or "ClosedResource" in text:
        return McpError(ErrorCode.SESSION_CLOSED, "session_closed", effect_uncertain=context == "call")
    return McpError(ErrorCode.SESSION_FAILED, "session_failure", effect_uncertain=context == "call")


def _jsonrpc_envelope(body: bytes) -> dict[str, Any]:
    """Return one JSON-RPC object from an already size-bounded request body. Fail closed on batches."""
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise McpError(ErrorCode.INVALID_JSONRPC, "invalid_request_body") from exc
    if not isinstance(value, dict):
        raise McpError(ErrorCode.INVALID_JSONRPC, "batch_or_non_object_request")
    return value


def _control_purpose(rpc_method: str) -> str:
    if rpc_method == "initialize":
        return "mcp_initialize"
    if rpc_method.startswith("notifications/"):
        return "mcp_notification"
    if rpc_method == "tools/list":
        return "mcp_list"
    if rpc_method == "ping":
        return "mcp_ping"
    # The adapter does not expose resources/prompts/logging or other methods. Do not hand them a
    # generic control permit that could be replayed across purposes.
    raise McpError(ErrorCode.CAPABILITY_MISSING, "unsupported_rpc_method")
