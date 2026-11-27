"""Strict JSON-Schema handling. MCP schemas are untrusted input, not authority and not a parser.

Guarantees:
* bounded size/depth/property count/node count before compilation;
* no `$ref` at all in the initial safe subset (same-document cycles and fan-out are not
  proven bounded), no absolute `$id` rebasing;
* 2020-12 dialect only (or an omitted dialect), NaN/Infinity and duplicate keys rejected;
* regexes are length-bounded and screened for catastrophic backtracking shapes before use;
* validation errors are bounded and reduced to paths/messages that never echo raw arguments.

The adapter compiles with `jsonschema` but treats the schema only as a validator; it never executes
`default`, `examples`, or any server-provided instruction.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .canonical import canonical_json, digest
from .config import AdapterLimits
from .errors import ErrorCode, McpError

_SUPPORTED_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_DESCRIPTION = 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 200_000
MAX_JSON_STRING_BYTES = 1024 * 1024
MAX_JSON_INT_DIGITS = 4096


def sanitize_description(text: str) -> str:
    """Neutralize control characters and bound display text. The result is data for the model, never policy."""
    if not isinstance(text, str):
        return ""
    flat = _CONTROL.sub(" ", text)
    flat = " ".join(flat.split())
    if len(flat) > _MAX_DESCRIPTION:
        flat = flat[: _MAX_DESCRIPTION - 3].rstrip() + "..."
    return flat


def loads_strict(
    raw: bytes | str,
    *,
    max_bytes: int,
    max_depth: int = MAX_JSON_DEPTH,
    max_nodes: int = MAX_JSON_NODES,
) -> Any:
    """Parse untrusted JSON with duplicate-key, NaN/Infinity, depth, node and size rejection."""
    try:
        data = raw.encode("utf-8") if isinstance(raw, str) else raw
    except UnicodeEncodeError as exc:
        raise McpError(ErrorCode.SCHEMA_INVALID, "invalid_utf8") from exc
    if len(data) > max_bytes:
        raise McpError(ErrorCode.BYTES_LIMIT_EXCEEDED, "json_too_large")

    def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise McpError(ErrorCode.SCHEMA_INVALID, "duplicate_json_key")
            out[key] = value
        return out

    def _bad_constant(value: str) -> None:
        raise McpError(ErrorCode.SCHEMA_INVALID, f"non_finite_number:{value}")

    def _finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise McpError(ErrorCode.SCHEMA_INVALID, "non_finite_number")
        return parsed

    def _bounded_int(value: str) -> int:
        if len(value.lstrip("-")) > MAX_JSON_INT_DIGITS:
            raise McpError(ErrorCode.BYTES_LIMIT_EXCEEDED, "integer_too_large")
        return int(value)

    try:
        parsed = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_bad_constant,
            parse_float=_finite_float,
            parse_int=_bounded_int,
        )
    except UnicodeDecodeError as exc:
        raise McpError(ErrorCode.SCHEMA_INVALID, "invalid_utf8") from exc
    except json.JSONDecodeError as exc:
        raise McpError(ErrorCode.SCHEMA_INVALID, "invalid_json") from exc
    _bound_json_value(parsed, max_depth=max_depth, max_nodes=max_nodes)
    return parsed


def _bound_json_value(value: Any, *, max_depth: int, max_nodes: int) -> None:
    """Iterative bound check; never recurses on attacker-controlled nesting."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            raise McpError(ErrorCode.BYTES_LIMIT_EXCEEDED, "json_node_limit")
        if depth > max_depth:
            raise McpError(ErrorCode.SCHEMA_INVALID, "json_too_deep")
        if isinstance(item, str):
            try:
                size = len(item.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise McpError(ErrorCode.SCHEMA_INVALID, "invalid_utf8") from exc
            if size > MAX_JSON_STRING_BYTES:
                raise McpError(ErrorCode.BYTES_LIMIT_EXCEEDED, "json_string_too_large")
        elif isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 256:
                    raise McpError(ErrorCode.SCHEMA_INVALID, "json_key_invalid")
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            for child in item:
                stack.append((child, depth + 1))
        elif isinstance(item, float) and not math.isfinite(item):
            raise McpError(ErrorCode.SCHEMA_INVALID, "non_finite_number")
        elif not isinstance(item, (str, int, float, bool, type(None))):
            raise McpError(ErrorCode.SCHEMA_INVALID, "json_value_type")


def clone_arguments(arguments: Any, *, max_bytes: int, max_depth: int = MAX_JSON_DEPTH) -> dict[str, Any]:
    """Deep-copy one argument object with finite bounds, so awaits cannot observe caller mutation."""
    if not isinstance(arguments, dict):
        raise McpError(ErrorCode.ARGUMENTS_INVALID, "arguments_not_object")
    _bound_json_value(arguments, max_depth=max_depth, max_nodes=MAX_JSON_NODES)
    try:
        copied = json.loads(json.dumps(arguments, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise McpError(ErrorCode.ARGUMENTS_INVALID, "arguments_not_json") from exc
    if not isinstance(copied, dict):
        raise McpError(ErrorCode.ARGUMENTS_INVALID, "arguments_not_object")
    _bound_json_value(copied, max_depth=max_depth, max_nodes=MAX_JSON_NODES)
    if len(canonical_json(copied).encode("utf-8")) > max_bytes:
        raise McpError(ErrorCode.BYTES_LIMIT_EXCEEDED, "arguments_too_large")
    return copied


@dataclass(frozen=True)
class CompiledSchema:
    schema: dict[str, Any]
    digest: str
    _validator: Draft202012Validator

    def errors(self, value: Any, *, limit: int = 5) -> list[str]:
        found: list[str] = []
        for error in self._validator.iter_errors(value):
            path = "/".join(str(p) for p in error.absolute_path)[:200] or "$"
            found.append(f"{path}: {error.validator}")
            if len(found) >= limit:
                break
        return found


def _check_regex(pattern: str, limits: AdapterLimits) -> None:
    if not isinstance(pattern, str) or not pattern:
        raise McpError(ErrorCode.SCHEMA_INVALID, "invalid_regex")
    if len(pattern) > limits.max_regex_length:
        raise McpError(ErrorCode.SCHEMA_INVALID, "regex_too_long")
    if "\\1" in pattern or re.search(r"\\[2-9]", pattern):
        raise McpError(ErrorCode.SCHEMA_INVALID, "regex_backreference")
    if re.search(r"\((?:\?:)?[^()]*[+*][^()]*\)\s*(?:[+*]|\{\d+,)", pattern):
        raise McpError(ErrorCode.SCHEMA_INVALID, "regex_nested_quantifier")
    if re.search(r"\((?:\?:)?[^()]*\|[^()]*\)\s*(?:\+|\{\d+,\d*\})", pattern):
        raise McpError(ErrorCode.SCHEMA_INVALID, "regex_alternation_quantifier")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise McpError(ErrorCode.SCHEMA_INVALID, f"regex_invalid:{exc.msg}") from exc


def _walk(
    schema: Any,
    limits: AdapterLimits,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
) -> tuple[int, int]:
    if depth > limits.max_schema_depth:
        raise McpError(ErrorCode.SCHEMA_INVALID, "schema_too_deep")
    if seen is None:
        seen = set()
    if isinstance(schema, (dict, list)):
        marker = id(schema)
        if marker in seen:
            raise McpError(ErrorCode.SCHEMA_INVALID, "schema_cycle")
        seen.add(marker)
    if isinstance(schema, dict):
        nodes = 1
        props = 0
        for key, value in schema.items():
            if key in ("$ref", "$dynamicRef", "$recursiveRef"):
                raise McpError(ErrorCode.SCHEMA_INVALID, "ref_not_in_safe_subset")
            if key == "uniqueItems":
                raise McpError(ErrorCode.SCHEMA_INVALID, "unique_items_not_in_safe_subset")
            if key == "$id" and isinstance(value, str) and ("://" in value or value.startswith("//")):
                raise McpError(ErrorCode.SCHEMA_INVALID, "absolute_id_unsupported")
            if key == "$schema" and value != _SUPPORTED_DIALECT:
                raise McpError(ErrorCode.SCHEMA_INVALID, "unsupported_dialect")
            if key == "pattern":
                _check_regex(value, limits)
            if key == "patternProperties":
                if not isinstance(value, dict):
                    raise McpError(ErrorCode.SCHEMA_INVALID, "pattern_properties_invalid")
                for pattern in value:
                    _check_regex(pattern, limits)
                    props += 1
            if key == "properties":
                if not isinstance(value, dict):
                    raise McpError(ErrorCode.SCHEMA_INVALID, "properties_invalid")
                props += len(value)
            children, child_props = _walk(value, limits, depth=depth + 1, seen=seen)
            nodes += children
            props += child_props
            if nodes > 5000:
                raise McpError(ErrorCode.SCHEMA_INVALID, "schema_too_large")
        if props > limits.max_properties:
            raise McpError(ErrorCode.SCHEMA_INVALID, "too_many_properties")
        return nodes, props
    if isinstance(schema, list):
        nodes = 1
        props = 0
        for item in schema:
            children, child_props = _walk(item, limits, depth=depth + 1, seen=seen)
            nodes += children
            props += child_props
        return nodes, props
    if isinstance(schema, float) and (math.isnan(schema) or math.isinf(schema)):
        raise McpError(ErrorCode.SCHEMA_INVALID, "non_finite_number")
    return 0, 0


def compile_schema(schema: Any, limits: AdapterLimits, *, where: str) -> CompiledSchema:
    """Validate and compile one untrusted schema. `where` is a safe label (input/output), not user data."""
    if not isinstance(schema, dict):
        raise McpError(ErrorCode.SCHEMA_INVALID, f"{where}_not_object")
    _bound_json_value(schema, max_depth=MAX_JSON_DEPTH, max_nodes=MAX_JSON_NODES)
    try:
        raw = json.dumps(schema, allow_nan=False, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise McpError(ErrorCode.SCHEMA_INVALID, f"{where}_not_json") from exc
    if len(raw) > limits.max_schema_bytes:
        raise McpError(ErrorCode.SCHEMA_INVALID, f"{where}_too_large")
    _walk(schema, limits)
    if where == "input" and schema.get("type") != "object":
        raise McpError(ErrorCode.SCHEMA_INVALID, "input_not_object")
    if where == "output" and schema.get("type") not in (None, "object"):
        raise McpError(ErrorCode.SCHEMA_INVALID, "output_not_object")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise McpError(ErrorCode.SCHEMA_INVALID, f"{where}_schema_error:{exc.validator}") from exc
    return CompiledSchema(schema=schema, digest=digest(schema), _validator=Draft202012Validator(schema))


def validate_arguments(compiled: CompiledSchema, arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise McpError(ErrorCode.ARGUMENTS_INVALID, "arguments_not_object")
    errors = compiled.errors(arguments)
    if errors:
        raise McpError(ErrorCode.ARGUMENTS_INVALID, "arguments_invalid:" + ",".join(errors))
    return arguments


def validate_output(compiled: CompiledSchema, structured: Any) -> list[str]:
    if structured is None:
        return []
    return compiled.errors(structured)
