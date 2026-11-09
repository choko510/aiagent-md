"""The single int64 wire codec (Architecture Rules E-3).

Wire: canonical signed decimal string. Internal: Python ``int``. DB: BIGINT.
JSON numbers, booleans, floats, leading zeros, ``+``, ``-0``, whitespace, exponent and non-ASCII digits are rejected.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import BeforeValidator, PlainSerializer, ValidationInfo, WithJsonSchema

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
_CANON = re.compile(r"\A(0|-?[1-9][0-9]{0,18})\Z", re.ASCII)


class Int64WireError(ValueError):
    pass


def parse_int64_wire(value: Any, *, minimum: int = INT64_MIN, maximum: int = INT64_MAX) -> int:
    # bool is a subclass of int and str subclasses could smuggle behaviour: require exact str.
    if type(value) is not str:
        raise Int64WireError("int64 must be a canonical decimal string")
    if not _CANON.match(value):
        raise Int64WireError("int64 string is not canonical")
    v = int(value)
    if not (INT64_MIN <= v <= INT64_MAX):
        raise Int64WireError("int64 out of range")
    if not (minimum <= v <= maximum):
        raise Int64WireError("int64 outside the field range")
    return v


def format_int64_wire(value: int, *, minimum: int = INT64_MIN, maximum: int = INT64_MAX) -> str:
    if type(value) is not int:
        raise Int64WireError("internal int64 must be int")
    if not (max(INT64_MIN, minimum) <= value <= min(INT64_MAX, maximum)):
        raise Int64WireError("int64 out of range")
    return str(value)


def check_int64(value: int, *, minimum: int = INT64_MIN) -> int:
    """Range check for values that go to the DB without a wire hop."""
    if type(value) is not int or not (minimum <= value <= INT64_MAX):
        raise Int64WireError("int64 out of range")
    return value


INTERNAL: dict[str, bool] = {"aiagent_internal": True}
"""Validation context for trusted in-process construction from Python ints (DB rows). Wire input never uses it."""


def _codec(minimum: int, pattern: str) -> tuple[BeforeValidator, PlainSerializer, WithJsonSchema]:
    def _parse(v: Any, info: ValidationInfo) -> int:
        # Trusted construction from DB rows may pass an int (never a bool). Wire input (default) must be a string,
        # so a JSON number is rejected even though it would be numerically exact.
        if type(v) is int and isinstance(info.context, dict) and info.context.get("aiagent_internal"):
            return check_int64(v, minimum=minimum)
        return parse_int64_wire(v, minimum=minimum)

    return (
        BeforeValidator(_parse),
        PlainSerializer(lambda v: format_int64_wire(v, minimum=minimum), return_type=str),
        WithJsonSchema({"type": "string", "pattern": pattern}),
    )


Int64 = Annotated[int, *_codec(INT64_MIN, r"^(0|-?[1-9][0-9]{0,18})$")]
NonNegativeInt64 = Annotated[int, *_codec(0, r"^(0|[1-9][0-9]{0,18})$")]
PositiveInt64 = Annotated[int, *_codec(1, r"^[1-9][0-9]{0,18}$")]
