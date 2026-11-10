"""aiagent-md canonical shared contracts (pure, no I/O, no secrets)."""

from . import generated as models
from .canonical import canonical_json, sha256_hex
from .ids import is_uuid7, require_uuid7, utc_instant, uuid7
from .int64 import INT64_MAX, INT64_MIN, INTERNAL, Int64WireError, format_int64_wire, parse_int64_wire

TERMINAL_RUN_STATES = frozenset({"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "EXPIRED", "BUDGET_EXHAUSTED"})
TERMINAL_TASK_STATES = TERMINAL_RUN_STATES

__all__ = [
    "INT64_MAX",
    "INT64_MIN",
    "INTERNAL",
    "TERMINAL_RUN_STATES",
    "TERMINAL_TASK_STATES",
    "Int64WireError",
    "canonical_json",
    "format_int64_wire",
    "is_uuid7",
    "models",
    "parse_int64_wire",
    "require_uuid7",
    "sha256_hex",
    "utc_instant",
    "uuid7",
]
