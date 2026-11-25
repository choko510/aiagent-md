"""Bounded, all-or-nothing tool discovery over paginated `tools/list`.

Rules:
* opaque cursors only, each seen cursor is remembered (loop -> fail);
* page, tool and byte ceilings are enforced before the next request;
* a tool with an invalid/unsafe schema is quarantined, never published and never classified as read;
* duplicate names are ambiguous identity -> the whole scan fails (the previous generation stays);
* a `list_changed` notification observed during the scan aborts it (no torn snapshot);
* the admin allowlist and an exact-revision policy record are both required to publish a tool.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .canonical import canonical_json, digest
from .config import AdapterLimits, ProviderConfig
from .contracts import (
    ToolDescriptor,
    ToolEntry,
    build_entry,
    descriptor_from_tool,
    utc_now,
)
from .errors import ErrorCode, McpError
from .policy import PolicySet
from .transport import TransportSession

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


@dataclass(frozen=True)
class PendingTool:
    name: str
    reason: str


@dataclass(frozen=True)
class DiscoveryResult:
    entries: tuple[ToolEntry, ...]
    pending: tuple[PendingTool, ...]
    missing_allowlisted: tuple[str, ...]
    pages: int
    bytes: int
    protocol_revision: str
    server_name: str
    server_version: str


class GenerationMarker(Protocol):
    """Monotonic counter bumped by `notifications/tools/list_changed` and provider invalidation."""

    def __call__(self) -> int: ...


async def discover_tools(
    *,
    provider: ProviderConfig,
    session: TransportSession,
    policy: PolicySet,
    generation: int,
    limits: AdapterLimits,
    generation_marker: GenerationMarker,
    started_at_generation: int | None = None,
) -> DiscoveryResult:
    if generation < 1:
        raise McpError(ErrorCode.BINDING_MISMATCH, "generation_must_be_positive")
    start_marker = generation_marker() if started_at_generation is None else started_at_generation
    seen_cursors: set[str] = set()
    seen_names: set[str] = set()
    pending: list[PendingTool] = []
    entries: list[ToolEntry] = []
    pages = 0
    total_bytes = 0
    cursor: str | None = None
    received = 0

    while True:
        if generation_marker() != start_marker:
            raise McpError(ErrorCode.LIST_CHANGED_DURING_DISCOVERY, "tools_list_changed_during_discovery")
        if pages >= limits.max_pages:
            raise McpError(ErrorCode.PAGE_LIMIT_EXCEEDED, f"pages>{limits.max_pages}")
        page = await session.list_page(cursor, timeout_s=limits.list_timeout_s)
        pages += 1
        page_bytes = len(canonical_json(page.raw).encode("utf-8"))
        total_bytes += page_bytes
        if total_bytes > limits.max_listing_bytes:
            raise McpError(ErrorCode.BYTES_LIMIT_EXCEEDED, "listing_bytes_exceeded")
        received += len(page.tools)
        if received > limits.max_tools:
            raise McpError(ErrorCode.TOOL_LIMIT_EXCEEDED, f"tools>{limits.max_tools}")

        for raw in page.tools:
            descriptor = _descriptor_from_raw(raw, limits)
            if descriptor.source_name in seen_names:
                raise McpError(ErrorCode.DUPLICATE_TOOL_NAME, descriptor.source_name[:64])
            seen_names.add(descriptor.source_name)
            decision = policy.decide(descriptor)
            if decision.policy is None:
                pending.append(PendingTool(descriptor.source_name, decision.reason))
                continue
            entry = build_entry(
                descriptor=descriptor,
                provider_id=provider.provider_id,
                tenant_id=provider.tenant_id,
                owner_user_id=provider.owner_user_id,
                risk_class=decision.policy.risk_class,
                approval_required=decision.policy.approval_required,
                policy_revision=policy.revision,
                credential_version=provider.credential_version,
                generation=generation,
            )
            entries.append(entry)

        next_cursor = page.next_cursor
        if next_cursor is None:
            break
        if not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 4096:
            raise McpError(ErrorCode.INVALID_JSONRPC, "invalid_cursor")
        if next_cursor in seen_cursors:
            raise McpError(ErrorCode.CURSOR_LOOP, "cursor_repeated")
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    if generation_marker() != start_marker:
        raise McpError(ErrorCode.LIST_CHANGED_DURING_DISCOVERY, "tools_list_changed_during_discovery")
    # Provider-scoped aliases are unique iff the source names are: verify before publishing.
    aliases = [e.alias for e in entries]
    if len(set(aliases)) != len(aliases):
        raise McpError(ErrorCode.DUPLICATE_TOOL_NAME, "alias_collision")
    missing = tuple(sorted(policy.allowlist - seen_names))
    return DiscoveryResult(
        entries=tuple(entries),
        pending=tuple(pending),
        missing_allowlisted=missing,
        pages=pages,
        bytes=total_bytes,
        protocol_revision=session.protocol_revision,
        server_name=session.server_name,
        server_version=session.server_version,
    )


def _descriptor_from_raw(raw: Any, limits: AdapterLimits) -> ToolDescriptor:
    if not isinstance(raw, dict):
        raise McpError(ErrorCode.INVALID_JSONRPC, "tool_not_object")
    name = raw.get("name")
    if not isinstance(name, str) or not _TOOL_NAME.match(name):
        raise McpError(ErrorCode.SCHEMA_INVALID, "invalid_tool_name")
    description = raw.get("description", "")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise McpError(ErrorCode.SCHEMA_INVALID, "invalid_description")
    input_schema = raw.get("inputSchema")
    if not isinstance(input_schema, dict):
        raise McpError(ErrorCode.SCHEMA_INVALID, "missing_input_schema")
    output_schema = raw.get("outputSchema")
    if output_schema is not None and not isinstance(output_schema, dict):
        raise McpError(ErrorCode.SCHEMA_INVALID, "invalid_output_schema")
    # Size check before compilation (the compiler also enforces it).
    try:
        payload = canonical_json(raw).encode("utf-8")
    except McpError as exc:
        raise McpError(ErrorCode.SCHEMA_INVALID, "tool_not_canonical") from exc
    if len(payload) > limits.max_schema_bytes * 2:
        raise McpError(ErrorCode.SCHEMA_INVALID, "tool_descriptor_too_large")
    from .schema_validation import compile_schema

    compile_schema(input_schema, limits, where="input")
    if output_schema is not None:
        compile_schema(output_schema, limits, where="output")
    descriptor = descriptor_from_tool(
        name=name,
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
    )
    if descriptor.revision != digest(
        {
            "name": name,
            "description": descriptor.description,
            "input": input_schema,
            "output": output_schema,
        }
    ):
        raise McpError(ErrorCode.SCHEMA_INVALID, "descriptor_revision_mismatch")
    return descriptor


@dataclass
class RefreshCoordinator:
    """Serializes refreshes for one provider and returns one complete discovery for CAS publish.

    A quarantine (pending tools) does not block publishing the approved entries, but the new generation
    immediately supersedes the old one, so no stale ToolRef can execute. A failed/incomplete scan leaves
    the previous generation in place; callers that detect a descriptor change must revoke, not extend it.
    Publishing remains the caller's responsibility; this coordinator never mutates a snapshot itself.
    """

    provider: ProviderConfig
    policy: PolicySet
    limits: AdapterLimits
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def refresh(self, session: TransportSession, *, generation: int, marker: GenerationMarker) -> DiscoveryResult:
        async with self._lock:
            return await discover_tools(
                provider=self.provider,
                session=session,
                policy=self.policy,
                generation=generation,
                limits=self.limits,
                generation_marker=marker,
            )

    def now_entry_count(self, result: DiscoveryResult) -> int:
        return len(result.entries)


def descriptor_revision_changed(
    policy: PolicySet, result: DiscoveryResult, previous: dict[str, str]
) -> tuple[str, ...]:
    """Names whose descriptor revision differs from the last published revision (requires re-approval)."""
    changed: list[str] = []
    for entry in result.entries:
        old = previous.get(entry.source_name)
        if old is not None and old != entry.descriptor_revision:
            changed.append(entry.source_name)
    for item in result.pending:
        if item.reason == "descriptor_changed":
            changed.append(item.name)
    return tuple(sorted(set(changed)))


def stamp_now() -> str:
    return utc_now().isoformat()
