from __future__ import annotations

import base64
import hashlib

import pytest
from aiagent_mcp_adapter.contracts import build_entry, descriptor_from_tool
from aiagent_mcp_adapter.errors import ErrorCode, McpError
from aiagent_mcp_adapter.normalization import normalize_result

from .helpers import ECHO_SCHEMA, entry, limits


def test_text_is_bounded_and_control_characters_are_neutralized() -> None:
    result = normalize_result(
        {"content": [{"type": "text", "text": "a\x00b\x07c"}]},
        entry=entry(),
        limits=limits(),
    )
    assert result.text == "a b c"

    long = normalize_result(
        {"content": [{"type": "text", "text": "a" * 1100}]},
        entry=entry(),
        limits=limits(max_text_bytes=1024),
    )
    assert len(long.text.encode("utf-8")) == 1024
    assert long.truncated is True
    assert "text_truncated" in long.notes


def test_binary_is_hashed_not_inlined() -> None:
    payload = base64.b64encode(b"png-bytes").decode()
    result = normalize_result(
        {"content": [{"type": "image", "data": payload, "mimeType": "image/png"}]},
        entry=entry(),
        limits=limits(),
    )
    assert result.blocks[0].sha256 == hashlib.sha256(b"png-bytes").hexdigest()
    assert result.blocks[0].size_bytes == 9
    assert result.text == ""
    assert "image_not_inlined" in result.notes


def test_tool_error_and_unsupported_blocks() -> None:
    result = normalize_result(
        {"content": [{"type": "unknown", "data": "x"}], "isError": True},
        entry=entry(),
        limits=limits(),
    )
    assert result.status == "TOOL_ERROR"

    result = normalize_result(
        {"content": [{"type": "text", "text": "boom"}], "isError": True},
        entry=entry(),
        limits=limits(),
    )
    assert result.status == "TOOL_ERROR"


def test_resource_link_is_not_fetched() -> None:
    result = normalize_result(
        {"content": [{"type": "resource_link", "uri": "https://evil.example/x", "mimeType": "text/html"}]},
        entry=entry(),
        limits=limits(),
    )
    assert result.blocks[0].uri == "https://evil.example/x"
    assert "resource_link_not_fetched" in result.notes


def test_output_schema_is_enforced_when_pinned() -> None:
    out = {"type": "object", "properties": {"receipt": {"type": "string"}}, "required": ["receipt"]}
    desc = descriptor_from_tool(name="echo", description="Echo", input_schema=ECHO_SCHEMA, output_schema=out)
    tool_entry = build_entry(
        descriptor=desc,
        provider_id="demo",
        tenant_id="tenant-a",
        owner_user_id="owner-a",
        risk_class="write_reversible",
        approval_required=True,
        policy_revision=2,
        credential_version=1,
        generation=1,
    )
    with pytest.raises(McpError) as err:
        normalize_result(
            {"content": [], "structuredContent": {"receipt": 1}},
            entry=tool_entry,
            limits=limits(),
            output_schema=out,
        )
    assert err.value.code is ErrorCode.OUTPUT_SCHEMA_MISMATCH


def test_output_schema_requires_structured_content_not_just_text() -> None:
    out = {"type": "object", "properties": {"receipt": {"type": "string"}}, "required": ["receipt"]}
    desc = descriptor_from_tool(name="echo", description="Echo", input_schema=ECHO_SCHEMA, output_schema=out)
    tool_entry = build_entry(
        descriptor=desc,
        provider_id="demo",
        tenant_id="tenant-a",
        owner_user_id="owner-a",
        risk_class="read",
        approval_required=False,
        policy_revision=2,
        credential_version=1,
        generation=1,
    )
    with pytest.raises(McpError) as err:
        normalize_result(
            {"content": [{"type": "text", "text": "looks fine"}]},
            entry=tool_entry,
            limits=limits(),
            output_schema=out,
        )
    assert err.value.code is ErrorCode.OUTPUT_SCHEMA_MISMATCH


def test_empty_text_is_not_ok_evidence() -> None:
    result = normalize_result(
        {"content": [{"type": "text", "text": "  "}]},
        entry=entry(),
        limits=limits(),
    )
    assert result.status == "UNSUPPORTED"
    assert result.text == ""
    assert "empty_text_block" in result.notes


def test_structured_content_is_bounded() -> None:
    with pytest.raises(McpError) as err:
        normalize_result(
            {"content": [{"type": "text", "text": "ok"}], "structuredContent": {"blob": "x" * 2048}},
            entry=entry(),
            limits=limits(max_text_bytes=1024),
        )
    assert err.value.code is ErrorCode.RESULT_TRUNCATED
