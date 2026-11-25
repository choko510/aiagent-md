"""Bounded, secret-free telemetry. Field names are allowlisted; values are hashes or counters."""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


_ALLOWED_FIELDS = frozenset(
    {
        "provider_id",
        "tenant_id",
        "tool_id",
        "alias",
        "generation",
        "policy_revision",
        "credential_version",
        "protocol_revision",
        "pages",
        "bytes",
        "tools",
        "pending",
        "duration_ms",
        "status",
        "error_code",
        "retryable",
        "effect_uncertain",
        "session_id",
    }
)


class TelemetrySink(Protocol):
    def emit(self, event: str, fields: Mapping[str, Any]) -> None: ...


class NullTelemetry:
    def emit(self, event: str, fields: Mapping[str, Any]) -> None:
        return None


@dataclass
class InMemoryTelemetry:
    capacity: int = 200
    events: deque[tuple[str, dict[str, Any]]] = field(default_factory=deque)

    def emit(self, event: str, fields: Mapping[str, Any]) -> None:
        safe = {k: v for k, v in fields.items() if k in _ALLOWED_FIELDS and isinstance(v, (str, int, float, bool))}
        self.events.append((event[:64], safe))
        while len(self.events) > self.capacity:
            self.events.popleft()
