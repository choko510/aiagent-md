"""Transport boundary. Only plain data crosses it; no MCP SDK type is visible to discovery/registry.

The SDK lives entirely inside `sdk_transport.SdkStreamableHttpTransport`. Every HTTP request it makes
goes through the injected `McpEgress` port, so swapping the SDK is a one-file change and there is no
second network path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import ErrorCode, McpError


@dataclass(frozen=True)
class Credential:
    """A short-lived credential resolved by the Credential Broker inside the worker.

    Never serialized, logged or placed in an error. `header_name` is constrained by provider config.
    """

    header_name: str
    value: str = field(repr=False)
    version: int = 1

    def header_value(self) -> str:
        """Raw broker value; the transport applies the provider's auth scheme explicitly."""
        return self.value


@dataclass(frozen=True)
class ToolPage:
    raw: Mapping[str, Any]
    tools: tuple[Mapping[str, Any], ...]
    next_cursor: str | None


@dataclass(frozen=True)
class SessionInfo:
    protocol_revision: str
    server_name: str
    server_version: str
    capabilities: Mapping[str, Any]
    instructions_digest: str | None = None


class TransportSession(Protocol):
    @property
    def info(self) -> SessionInfo: ...

    @property
    def protocol_revision(self) -> str: ...

    @property
    def server_name(self) -> str: ...

    @property
    def server_version(self) -> str: ...

    async def list_page(self, cursor: str | None, *, timeout_s: float) -> ToolPage: ...

    async def call_tool(self, name: str, arguments: dict[str, Any], *, timeout_s: float) -> Mapping[str, Any]: ...

    async def close(self) -> None: ...


class McpTransport(Protocol):
    def open(
        self,
        *,
        provider: Any,
        credential: Credential | None,
        on_list_changed: Any,
        limits: Any,
    ) -> Any: ...


class GenerationCounter:
    """Monotonic marker for `tools/list_changed`. stdout/logging is never used for control flow."""

    def __init__(self, initial: int = 1) -> None:
        self._value = initial

    def bump(self) -> int:
        self._value += 1
        return self._value

    def value(self) -> int:
        return self._value


def parse_tool_page(raw: Any) -> ToolPage:
    if not isinstance(raw, Mapping):
        raise McpError(ErrorCode.INVALID_JSONRPC, "list_result_not_object")
    tools = raw.get("tools")
    if not isinstance(tools, list):
        raise McpError(ErrorCode.INVALID_JSONRPC, "tools_not_list")
    cleaned: list[Mapping[str, Any]] = []
    for item in tools:
        if not isinstance(item, Mapping):
            raise McpError(ErrorCode.INVALID_JSONRPC, "tool_not_object")
        cleaned.append(item)
    cursor = raw.get("nextCursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 4096):
        raise McpError(ErrorCode.INVALID_JSONRPC, "invalid_cursor")
    return ToolPage(raw=raw, tools=tuple(cleaned), next_cursor=cursor)
