"""Authenticated, short-lived source-session authorization envelope.

The trusted Gateway alone issues tickets after a canonical DB transaction. A
ticket does not replace a live authority watchdog or authenticate an image.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from typing import Any

from aiagent_domain import is_uuid7

_FIELDS = frozenset(
    {
        "version",
        "direction",
        "session",
        "tenant",
        "owner",
        "task",
        "run",
        "goal",
        "lease",
        "execution",
        "attempt",
        "snapshot",
        "recipe",
        "binding_sha256",
        "repository_binding",
        "revision",
        "commit",
        "tree",
        "manifest_sha256",
        "source_sha256",
        "source_bytes",
        "expires_at",
    }
)


def sign_source_ticket(key: bytes, payload: dict[str, Any]) -> dict[str, Any]:
    if len(key) < 32 or set(payload) != _FIELDS:
        raise ValueError("invalid source ticket")
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {"payload": payload, "mac": hmac.new(key, data, hashlib.sha256).hexdigest()}


def source_transfer_key(key: bytes, session_id: str) -> bytes:
    """Derive a direction-specific key; never send the authorization key."""
    if len(key) < 32 or not isinstance(session_id, str) or not 1 <= len(session_id) <= 128:
        raise ValueError("invalid source session key")
    return hmac.digest(key, b"dev13-source-transfer-v1\0" + session_id.encode("ascii"), "sha256")


def source_binding(
    session: str, execution: str, attempt: str, binding: str, snapshot: str, size: int, sha256: str
) -> bytes:
    payload = {
        "version": "dev13-source-session-v1",
        "session": session,
        "direction": "gateway-to-supervisor",
        "execution": execution,
        "attempt": attempt,
        "binding": binding,
        "snapshot": snapshot,
        "size": size,
        "sha256": sha256,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).digest()


def artifact_transfer_key(source_key: bytes) -> bytes:
    if len(source_key) < 32:
        raise ValueError("invalid artifact transfer key")
    return hmac.digest(source_key, b"dev13-artifact-transfer-v1\0", "sha256")


def artifact_binding(source_session_binding: bytes, sha256: str, size: int) -> bytes:
    if len(source_session_binding) != 32 or re.fullmatch(r"[0-9a-f]{64}", sha256) is None or not 1 <= size <= 4194304:
        raise ValueError("invalid artifact transfer binding")
    return hashlib.sha256(
        b"dev13-artifact-binding-v1\0" + source_session_binding + bytes.fromhex(sha256) + size.to_bytes(8, "big")
    ).digest()


def verify_source_ticket(key: bytes, envelope: Any, *, now: float | None = None) -> dict[str, Any]:
    if len(key) < 32 or not isinstance(envelope, dict) or set(envelope) != {"payload", "mac"}:
        raise ValueError("invalid source ticket")
    payload, mac = envelope["payload"], envelope["mac"]
    if not isinstance(payload, dict) or set(payload) != _FIELDS or not isinstance(mac, str):
        raise ValueError("invalid source ticket")
    try:
        expected = sign_source_ticket(key, payload)["mac"]
        timestamp = time.time() if now is None else now
        strings = (
            "session",
            "tenant",
            "owner",
            "task",
            "run",
            "execution",
            "attempt",
            "snapshot",
            "recipe",
            "repository_binding",
            "revision",
        )
        if (
            not hmac.compare_digest(mac, expected)
            or payload["version"] != 1
            or type(payload["version"]) is not int
            or payload["direction"] != "gateway-to-supervisor"
            or not is_uuid7(payload["session"])
            or not is_uuid7(payload["execution"])
            or any(type(payload[k]) is not str or not 1 <= len(payload[k]) <= 128 for k in strings)
            or any(type(payload[k]) is not int or payload[k] < 1 for k in ("goal", "source_bytes", "expires_at"))
            or type(payload["lease"]) is not int
            or payload["lease"] < 0
            or payload["source_bytes"] > 128 * 1024 * 1024
            or not timestamp < payload["expires_at"] <= timestamp + 60
            or any(
                type(payload[k]) is not str or re.fullmatch(r"[0-9a-f]{64}", payload[k]) is None
                for k in ("binding_sha256", "manifest_sha256", "source_sha256")
            )
            or any(
                type(payload[k]) is not str or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", payload[k]) is None
                for k in ("commit", "tree")
            )
            or len(payload["commit"]) != len(payload["tree"])
        ):
            raise ValueError("invalid source ticket")
    except (TypeError, OverflowError) as exc:
        raise ValueError("invalid source ticket") from exc
    return dict(payload)
