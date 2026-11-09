"""Fail-closed LangGraph checkpoint serializer (ADR-038 STEP 3 condition: LANGGRAPH_STRICT_MSGPACK equivalent).

LangGraph's own strict mode *silently* returns the raw payload for a blocked type. That is not good enough: a
tampered checkpoint would still be loaded (as data of the wrong shape). Here every non-allowlisted type, every
pickle / json / unknown envelope and every malformed blob RAISES `CheckpointRejected`; the runtime then discards the
derived cache and rebuilds from the canonical Checkpoint HEAD. The allowlist is explicit (no `True`), pickle fallback
is off, and the env var is also set so library code paths that consult it agree.
"""

from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

import ormsgpack  # noqa: E402
from langgraph.checkpoint.serde import _msgpack as lg_msgpack  # noqa: E402
from langgraph.checkpoint.serde.jsonplus import (  # noqa: E402
    EXT_CONSTRUCTOR_KW_ARGS,
    EXT_CONSTRUCTOR_POS_ARGS,
    EXT_CONSTRUCTOR_SINGLE_ARG,
    EXT_DELTA_SNAPSHOT,
    EXT_METHOD_SINGLE_ARG,
    EXT_NUMPY_ARRAY,
    EXT_PYDANTIC_V1,
    EXT_PYDANTIC_V2,
    JsonPlusSerializer,
)


class CheckpointRejected(Exception):
    """A framework checkpoint blob failed strict decoding; treat the derived cache as corrupt."""


# Types our graph state legitimately contains (messages, interrupts, sends, deepagents state). Nothing else.
ALLOWED_TYPES: frozenset[tuple[str, str]] = frozenset({
    ("langchain_core.messages.human", "HumanMessage"),
    ("langchain_core.messages.ai", "AIMessage"),
    ("langchain_core.messages.tool", "ToolMessage"),
    ("langchain_core.messages.system", "SystemMessage"),
    ("langchain_core.messages.base", "BaseMessage"),
    ("langchain_core.messages.modifier", "RemoveMessage"),
    ("langgraph.types", "Interrupt"),
    ("langgraph.types", "Send"),
    ("langgraph.types", "Command"),
    ("datetime", "datetime"),
    ("datetime", "timezone"),
    ("datetime", "timedelta"),
    ("uuid", "UUID"),
    ("builtins", "set"),
    ("builtins", "frozenset"),
    ("collections", "deque"),
})
ALLOWED_METHODS: frozenset[tuple[str, str, str]] = frozenset({
    ("datetime", "datetime", "fromisoformat"),
})
ALLOWED_ENVELOPES = frozenset({"null", "msgpack", "bytes", "bytearray"})


def _peek(data: bytes) -> Any:
    """Decode one ext payload without constructing anything (nested ext -> None)."""
    return ormsgpack.unpackb(data, ext_hook=lambda _c, _d: None, option=ormsgpack.OPT_NON_STR_KEYS)


class StrictSerializer(JsonPlusSerializer):
    def __init__(self) -> None:
        super().__init__(pickle_fallback=False, allowed_json_modules=None,
                         allowed_msgpack_modules=list(ALLOWED_TYPES))  # type: ignore[arg-type]
        inner = self._unpack_ext_hook

        def guarded(code: int, data: bytes) -> Any:
            if code == EXT_DELTA_SNAPSHOT:
                return inner(code, data)
            if code in (EXT_PYDANTIC_V1, EXT_NUMPY_ARRAY):
                raise CheckpointRejected(f"ext code {code} not allowed")
            try:
                tup = _peek(data)
            except Exception as e:
                raise CheckpointRejected("malformed ext payload") from e
            if not isinstance(tup, (list, tuple)) or len(tup) < 2:
                raise CheckpointRejected("malformed ext tuple")
            key = (str(tup[0]), str(tup[1]))
            if code in (EXT_CONSTRUCTOR_SINGLE_ARG, EXT_CONSTRUCTOR_POS_ARGS, EXT_CONSTRUCTOR_KW_ARGS, EXT_PYDANTIC_V2):
                if key not in ALLOWED_TYPES:
                    raise CheckpointRejected(f"type {key[0]}.{key[1]} not in allowlist")
            elif code == EXT_METHOD_SINGLE_ARG:
                if len(tup) < 3 or (key[0], key[1], str(tup[2])) not in ALLOWED_METHODS:
                    raise CheckpointRejected("method not in allowlist")
            else:
                raise CheckpointRejected(f"unknown ext code {code}")
            out = inner(code, data)
            if isinstance(out, (dict, list)) and code != EXT_PYDANTIC_V2 and key not in lg_msgpack.SAFE_MSGPACK_TYPES \
                    and key not in ALLOWED_TYPES:
                raise CheckpointRejected("blocked type returned as raw data")
            return out

        self._unpack_ext_hook = guarded

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        type_, payload = data
        if type_ not in ALLOWED_ENVELOPES:
            raise CheckpointRejected(f"envelope {type_!r} not allowed")
        try:
            return super().loads_typed(data)
        except CheckpointRejected:
            raise
        except Exception as e:
            raise CheckpointRejected(f"undecodable {type_} blob: {type(e).__name__}") from e

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        t, b = super().dumps_typed(obj)
        if t not in ALLOWED_ENVELOPES:
            raise CheckpointRejected(f"refusing to write envelope {t!r}")
        return t, b
