"""Canonical JSON and digests shared by discovery, policy and the registry."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .errors import ErrorCode, McpError


def canonical_json(value: Any) -> str:
    """Deterministic JSON for digests. Rejects NaN/Infinity (never valid MCP tool I/O)."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise McpError(ErrorCode.SCHEMA_INVALID, "non_canonical_json") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def byte_len(value: Any) -> int:
    return len(canonical_json(value).encode("utf-8"))


def stable_label(provider_id: str, name: str) -> str:
    """Stable tool_id that survives schema/policy changes; the schema is pinned separately."""
    return hashlib.sha256(f"{provider_id}\0{name}".encode()).hexdigest()[:32]


def model_alias(provider_id: str, name: str) -> str:
    """Provider-scoped alias shown to the model. Sanitized, length-bounded, case-insensitive unique."""
    safe = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in name.lower()).strip("_") or "tool"
    raw = f"mcp_{provider_id}_{safe}"
    if len(raw) <= 64:
        return raw
    return f"{raw[:55]}_{hashlib.sha256(raw.encode()).hexdigest()[:8]}"
