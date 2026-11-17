"""Typed, non-trusting decoding of provider responses.

Only the values needed for the fixed synthetic fixtures are read. Model reasoning
fields, tool calls, provider-specific metadata and raw bodies are discarded: nothing
here can execute a command, write a file, call another endpoint or echo raw output.
Malformed JSON (duplicate keys, ``NaN``/``Infinity``, excessive nesting, wrong types)
is rejected instead of coerced.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from . import ReasonCode
from .catalog import ProviderSpec

MAX_CONTENT_CHARS = 4096
MAX_JSON_DEPTH = 8
MAX_MODEL_ID_CHARS = 128

#: A reported model id is only kept when it is a short, printable token. Anything else
#: (control characters, newlines, huge strings) is treated as "not reported" so that no
#: provider-controlled text can reach stdout or the JSONL log.
_MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*")


class ResponseFormatError(ValueError):
    """The provider response does not satisfy the expected schema; no value is trusted."""

    def __init__(self, reason_code: ReasonCode) -> None:
        super().__init__("provider response failed schema validation")
        self.reason_code: ReasonCode = reason_code


@dataclass(frozen=True, slots=True)
class NormalizedResponse:
    """The small, typed subset of a response that scoring is allowed to use."""

    text: str | None
    noul: float | None
    model_observed: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_reported_usd: float | None
    tool_output_present: bool


def _reject_non_finite(token: str) -> object:
    raise ValueError("non-finite number in provider response")


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key in provider response")
        result[key] = value
    return result


def _check_shape(value: object, depth: int) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ResponseFormatError("INVALID_SCHEMA")
    if isinstance(value, str) or value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, Mapping):
        for nested in value.values():
            _check_shape(nested, depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _check_shape(item, depth + 1)
        return
    raise ResponseFormatError("INVALID_SCHEMA")


def decode_object(raw: bytes) -> dict[str, object]:
    """Decode a bounded response body into a mapping, rejecting ambiguous JSON."""
    try:
        text = raw.decode("utf-8")
        parsed = json.loads(text, object_pairs_hook=_pairs_without_duplicates, parse_constant=_reject_non_finite)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        # An extremely deep document aborts the C scanner with RecursionError; that is a
        # malformed response, not a local crash.
        raise ResponseFormatError("INVALID_JSON") from exc
    if not isinstance(parsed, dict):
        raise ResponseFormatError("INVALID_SCHEMA")
    payload = cast("dict[str, object]", parsed)
    _check_shape(payload, 0)
    return payload


def _optional_non_negative_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResponseFormatError("INVALID_SCHEMA")
    if value < 0:
        raise ResponseFormatError("INVALID_SCHEMA")
    return value


def _first_present(usage: Mapping[str, object], preferred: str, fallback: str) -> int | None:
    value = usage.get(preferred)
    if value is not None:
        return _optional_non_negative_int(value)
    if fallback in usage:
        return _optional_non_negative_int(usage.get(fallback))
    return None


def _usage_tokens(payload: Mapping[str, object]) -> tuple[int | None, int | None]:
    if "usage" not in payload:
        return (None, None)
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        raise ResponseFormatError("INVALID_SCHEMA")
    input_tokens = _first_present(usage, "prompt_tokens", "input_tokens")
    output_tokens = _first_present(usage, "completion_tokens", "output_tokens")
    return (input_tokens, output_tokens)


def _to_finite_float(value: object) -> float:
    """Convert a JSON number to a finite float, never leaking the conversion error."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResponseFormatError("INVALID_SCHEMA")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        # A 309-digit integer is valid JSON but cannot be a sane cost or probability.
        raise ResponseFormatError("INVALID_SCHEMA") from None
    if not math.isfinite(number):
        raise ResponseFormatError("INVALID_SCHEMA")
    return number


def _cost(payload: Mapping[str, object]) -> float | None:
    if "cost" not in payload:
        return None
    amount = _to_finite_float(payload.get("cost"))
    if amount < 0:
        raise ResponseFormatError("INVALID_SCHEMA")
    return amount


def _model_observed(payload: Mapping[str, object]) -> str | None:
    value = payload.get("model")
    if not isinstance(value, str) or len(value) > MAX_MODEL_ID_CHARS:
        return None
    return value if _MODEL_ID_PATTERN.fullmatch(value) else None


def _has_tool_output(message: Mapping[str, object]) -> bool:
    if message.get("tool_calls") not in (None, [], {}):
        return True
    return message.get("function_call") not in (None, [], {})


def normalize_chat(payload: Mapping[str, object], spec: ProviderSpec) -> NormalizedResponse:
    """Normalize an OpenAI-compatible chat completion response."""
    if spec.wire_kind != "chat_completions":
        raise ResponseFormatError("INVALID_SCHEMA")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ResponseFormatError("INVALID_SCHEMA")
    first = choices[0]
    if not isinstance(first, dict):
        raise ResponseFormatError("INVALID_SCHEMA")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ResponseFormatError("INVALID_SCHEMA")
    content = message.get("content")
    if not isinstance(content, str):
        raise ResponseFormatError("INVALID_SCHEMA")
    if len(content) > MAX_CONTENT_CHARS:
        raise ResponseFormatError("INVALID_SCHEMA")
    if not content.strip():
        # Empty content (for example a reasoning-only answer) is not a usable answer.
        raise ResponseFormatError("INVALID_SCHEMA")
    input_tokens, output_tokens = _usage_tokens(payload)
    return NormalizedResponse(
        text=content,
        noul=None,
        model_observed=_model_observed(payload),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_reported_usd=_cost(payload),
        tool_output_present=_has_tool_output(message),
    )


def normalize_system_one(payload: Mapping[str, object], spec: ProviderSpec) -> NormalizedResponse:
    """Normalize a System One (Jev) response with a ``noul`` answer."""
    if spec.wire_kind != "system_one":
        raise ResponseFormatError("INVALID_SCHEMA")
    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        raise ResponseFormatError("INVALID_SCHEMA")
    refund = answers.get("refund")
    if not isinstance(refund, Mapping):
        raise ResponseFormatError("INVALID_SCHEMA")
    if refund.get("type") != "noul":
        raise ResponseFormatError("INVALID_SCHEMA")
    raw_noul = refund.get("noul")
    noul = _to_finite_float(raw_noul)
    if not 0.0 <= noul <= 1.0:
        raise ResponseFormatError("INVALID_SCHEMA")
    input_tokens, output_tokens = _usage_tokens(payload)
    return NormalizedResponse(
        text=None,
        noul=noul,
        model_observed=_model_observed(payload),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_reported_usd=_cost(payload),
        tool_output_present=False,
    )


__all__ = [
    "MAX_CONTENT_CHARS",
    "MAX_MODEL_ID_CHARS",
    "NormalizedResponse",
    "ResponseFormatError",
    "decode_object",
    "normalize_chat",
    "normalize_system_one",
]
