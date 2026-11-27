from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from aiagent_mcp_adapter.discovery import RefreshCoordinator, discover_tools
from aiagent_mcp_adapter.errors import ErrorCode, McpError
from aiagent_mcp_adapter.policy import PolicySet
from aiagent_mcp_adapter.transport import GenerationCounter, ToolPage, parse_tool_page

from .helpers import ECHO_SCHEMA, approved_policy, descriptor, limits, provider


def raw_tool(name: str = "echo", schema: dict | None = None) -> dict:
    return {"name": name, "description": "Echo text back.", "inputSchema": schema or ECHO_SCHEMA}


class FakeSession:
    protocol_revision = "2025-11-25"
    server_name = "fake"
    server_version = "1.0"
    info = None

    def __init__(self, pages: dict[str | None, ToolPage], *, bump_after: tuple[int, str] | None = None) -> None:
        self.pages = pages
        self.calls: list[str | None] = []
        self.bump_after = bump_after
        self.counter = GenerationCounter()

    async def list_page(self, cursor: str | None, *, timeout_s: float) -> ToolPage:
        self.calls.append(cursor)
        if self.bump_after is not None and len(self.calls) == self.bump_after[0]:
            self.counter.bump()
        return self.pages[cursor]

    async def close(self) -> None:
        return None


def page(tools: list[dict], next_cursor: str | None) -> ToolPage:
    return parse_tool_page({"tools": tools, "nextCursor": next_cursor})


async def test_paginated_discovery_publishes_only_approved_tools() -> None:
    session = FakeSession(
        {
            None: page([raw_tool("echo")], "c1"),
            "c1": page([raw_tool("other")], None),
        }
    )
    policy = approved_policy(descriptor("echo"))
    policy = policy.with_allowlist(("echo", "other"))
    result = await discover_tools(
        provider=provider(policy_revision=3),
        session=session,  # type: ignore[arg-type]
        policy=policy,
        generation=1,
        limits=limits(),
        generation_marker=session.counter.value,
    )
    assert [e.source_name for e in result.entries] == ["echo"]
    assert result.pending[0].name == "other"
    assert result.pending[0].reason == "unclassified"
    assert session.calls == [None, "c1"]


async def test_policy_must_match_provider_tenant_owner_and_revision() -> None:
    session = FakeSession({None: page([raw_tool("echo")], None)})
    base = approved_policy(descriptor("echo"))
    foreign = PolicySet(
        provider_id="demo",
        tenant_id="tenant-b",
        owner_user_id="owner-a",
        revision=2,
        allowlist=base.allowlist,
        policies={"echo": base._policies["echo"]},
    )
    with pytest.raises(McpError) as err:
        await discover_tools(
            provider=provider(),
            session=session,  # type: ignore[arg-type]
            policy=foreign,
            generation=1,
            limits=limits(),
            generation_marker=session.counter.value,
        )
    assert err.value.code is ErrorCode.POLICY_DENIED
    assert err.value.detail == "policy_scope_mismatch"

    with pytest.raises(McpError) as err:
        await discover_tools(
            provider=provider(policy_revision=3),
            session=session,  # type: ignore[arg-type]
            policy=base,
            generation=1,
            limits=limits(),
            generation_marker=session.counter.value,
        )
    assert err.value.code is ErrorCode.POLICY_DENIED
    assert err.value.detail == "policy_revision_mismatch"


async def test_transplanted_policy_record_fails_closed() -> None:
    session = FakeSession({None: page([raw_tool("echo")], None)})
    base = approved_policy(descriptor("echo"))
    transplanted = PolicySet(
        provider_id=base.provider_id,
        tenant_id=base.tenant_id,
        owner_user_id=base.owner_user_id,
        revision=base.revision,
        allowlist=base.allowlist,
        policies={"echo": replace(base._policies["echo"], tenant_id="tenant-b")},
    )
    with pytest.raises(McpError) as err:
        await discover_tools(
            provider=provider(),
            session=session,  # type: ignore[arg-type]
            policy=transplanted,
            generation=1,
            limits=limits(),
            generation_marker=session.counter.value,
        )
    assert err.value.code is ErrorCode.POLICY_DENIED
    assert err.value.detail == "policy_scope_mismatch"


async def test_cursor_loop_is_rejected() -> None:
    session = FakeSession(
        {
            None: page([raw_tool("echo")], "a"),
            "a": page([], "a"),
        }
    )
    with pytest.raises(McpError) as err:
        await discover_tools(
            provider=provider(),
            session=session,  # type: ignore[arg-type]
            policy=approved_policy(),
            generation=1,
            limits=limits(),
            generation_marker=session.counter.value,
        )
    assert err.value.code is ErrorCode.CURSOR_LOOP


async def test_duplicate_tool_name_fails_the_whole_scan() -> None:
    session = FakeSession({None: page([raw_tool("echo"), raw_tool("echo")], None)})
    with pytest.raises(McpError) as err:
        await discover_tools(
            provider=provider(),
            session=session,  # type: ignore[arg-type]
            policy=approved_policy(),
            generation=1,
            limits=limits(),
            generation_marker=session.counter.value,
        )
    assert err.value.code is ErrorCode.DUPLICATE_TOOL_NAME


async def test_list_changed_during_discovery_aborts() -> None:
    session = FakeSession(
        {None: page([raw_tool("echo")], "c1"), "c1": page([], None)},
        bump_after=(1, "c1"),
    )
    with pytest.raises(McpError) as err:
        await discover_tools(
            provider=provider(),
            session=session,  # type: ignore[arg-type]
            policy=approved_policy(),
            generation=1,
            limits=limits(),
            generation_marker=session.counter.value,
        )
    assert err.value.code is ErrorCode.LIST_CHANGED_DURING_DISCOVERY


async def test_page_and_tool_ceilings() -> None:
    session = FakeSession({None: page([raw_tool("echo")], "c1"), "c1": page([], None)})
    with pytest.raises(McpError) as err:
        await discover_tools(
            provider=provider(),
            session=session,  # type: ignore[arg-type]
            policy=approved_policy(),
            generation=1,
            limits=limits(max_pages=1),
            generation_marker=session.counter.value,
        )
    assert err.value.code is ErrorCode.PAGE_LIMIT_EXCEEDED

    session = FakeSession({None: page([raw_tool("echo"), raw_tool("other")], None)})
    with pytest.raises(McpError) as err:
        await discover_tools(
            provider=provider(),
            session=session,  # type: ignore[arg-type]
            policy=approved_policy(),
            generation=1,
            limits=limits(max_tools=1),
            generation_marker=session.counter.value,
        )
    assert err.value.code is ErrorCode.TOOL_LIMIT_EXCEEDED


async def test_changed_descriptor_requires_reapproval() -> None:
    approved = descriptor("echo")
    policy = approved_policy(approved)
    changed = raw_tool("echo", {**ECHO_SCHEMA, "properties": {"text": {"type": "string"}}})
    session = FakeSession({None: page([changed], None)})
    result = await discover_tools(
        provider=provider(),
        session=session,  # type: ignore[arg-type]
        policy=policy,
        generation=1,
        limits=limits(),
        generation_marker=session.counter.value,
    )
    assert result.entries == ()
    assert result.pending[0].reason == "descriptor_changed"


async def test_invalid_schema_aborts_discovery() -> None:
    unsafe = {"type": "object", "properties": {"x": {"$ref": "https://evil.example/x"}}}
    session = FakeSession({None: page([raw_tool("echo", unsafe)], None)})
    with pytest.raises(McpError) as err:
        await discover_tools(
            provider=provider(),
            session=session,  # type: ignore[arg-type]
            policy=approved_policy(),
            generation=1,
            limits=limits(),
            generation_marker=session.counter.value,
        )
    assert err.value.code is ErrorCode.SCHEMA_INVALID


class SlowSession(FakeSession):
    def __init__(self, pages: dict[str | None, ToolPage]) -> None:
        super().__init__(pages)
        self.active = 0
        self.max_active = 0

    async def list_page(self, cursor: str | None, *, timeout_s: float) -> ToolPage:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)
            return await super().list_page(cursor, timeout_s=timeout_s)
        finally:
            self.active -= 1


async def test_refresh_coordinator_serializes_same_provider() -> None:
    session = SlowSession({None: page([raw_tool("echo")], None)})
    coordinator = RefreshCoordinator(provider=provider(), policy=approved_policy(descriptor("echo")), limits=limits())
    first, second = await asyncio.gather(
        coordinator.refresh(session, generation=1, marker=session.counter.value),
        coordinator.refresh(session, generation=2, marker=session.counter.value),
    )
    assert session.max_active == 1
    assert [r.entries[0].generation for r in (first, second)] == [1, 2]
