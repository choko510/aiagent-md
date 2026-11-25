from __future__ import annotations

import pytest
from aiagent_mcp_adapter.errors import ErrorCode, McpError
from aiagent_mcp_adapter.normalization import normalize_result
from aiagent_mcp_adapter.schema_validation import (
    compile_schema,
    loads_strict,
    sanitize_description,
    validate_arguments,
    validate_output,
)

from .helpers import ECHO_SCHEMA, limits


def test_valid_schema_compiles_and_validates() -> None:
    compiled = compile_schema(ECHO_SCHEMA, limits(), where="input")
    assert compiled.digest
    validate_arguments(compiled, {"text": "hello"})
    with pytest.raises(McpError) as err:
        validate_arguments(compiled, {"text": 3})
    assert err.value.code is ErrorCode.ARGUMENTS_INVALID


def test_external_ref_is_rejected() -> None:
    schema = {"type": "object", "properties": {"x": {"$ref": "https://evil.example/schema.json"}}}
    with pytest.raises(McpError) as err:
        compile_schema(schema, limits(), where="input")
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    assert "external_ref" in err.value.detail


def test_dynamic_ref_and_absolute_id_rejected() -> None:
    for schema in (
        {"type": "object", "properties": {"x": {"$dynamicRef": "#foo"}}},
        {"type": "object", "$id": "https://evil.example/x", "properties": {}},
    ):
        with pytest.raises(McpError):
            compile_schema(schema, limits(), where="input")


def test_catastrophic_regex_shapes_are_rejected() -> None:
    for pattern in ("(a+)+$", "(a|aa)+$", "(?:x+)*"):
        schema = {"type": "object", "properties": {"x": {"type": "string", "pattern": pattern}}}
        with pytest.raises(McpError) as err:
            compile_schema(schema, limits(), where="input")
        assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_depth_and_property_limits() -> None:
    deep: dict[str, object] = {"type": "object"}
    cursor = deep
    for _ in range(40):
        child: dict[str, object] = {"type": "object"}
        cursor["properties"] = {"x": child}
        cursor = child
    with pytest.raises(McpError):
        compile_schema(deep, limits(), where="input")

    many = {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(300)}}
    with pytest.raises(McpError):
        compile_schema(many, limits(max_properties=16), where="input")


def test_strict_json_rejects_duplicates_nan_and_oversize() -> None:
    with pytest.raises(McpError) as err:
        loads_strict('{"a": 1, "a": 2}', max_bytes=1024)
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    with pytest.raises(McpError):
        loads_strict('{"a": NaN}', max_bytes=1024)
    with pytest.raises(McpError) as err:
        loads_strict("{}", max_bytes=1)
    assert err.value.code is ErrorCode.BYTES_LIMIT_EXCEEDED


def test_description_is_sanitized_and_bounded() -> None:
    text = "line\x00" + "a" * 5000
    safe = sanitize_description(text)
    assert "\x00" not in safe
    assert len(safe) <= 1024


def test_output_schema_mismatch_is_an_error() -> None:
    out = {
        "type": "object",
        "properties": {"receipt": {"type": "string"}},
        "required": ["receipt"],
        "additionalProperties": False,
    }
    from aiagent_mcp_adapter.contracts import build_entry, descriptor_from_tool

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
    compiled = compile_schema(out, limits(), where="output")
    assert validate_output(compiled, {"receipt": 5})
    with pytest.raises(McpError) as err:
        normalize_result(
            {"content": [{"type": "text", "text": "ok"}], "structuredContent": {"receipt": 5}},
            entry=tool_entry,
            limits=limits(),
            output_schema=out,
        )
    assert err.value.code is ErrorCode.OUTPUT_SCHEMA_MISMATCH
