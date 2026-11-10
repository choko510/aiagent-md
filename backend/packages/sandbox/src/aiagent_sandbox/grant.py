"""Execution grants: the ONLY authority the Untrusted Execution VM accepts. Issued by the Tool Gateway after final
admission of a `sandbox_exec` intent; bound to the exact argv/cwd/limits, the run, its lease epoch and the intent;
single use; short expiry. The supervisor cannot reach the control DB, so everything it needs is in the signed grant."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import asdict, dataclass
from typing import Any

GRANT_TTL_S = 120


class GrantError(Exception):
    pass


@dataclass(frozen=True)
class Grant:
    grant_id: str
    tenant_id: str
    task_id: str
    run_id: str
    intent_id: str
    lease_epoch: int
    argv: tuple[str, ...]
    cwd: str
    wall_ms: int
    memory_bytes: int
    expires: int

    def payload(self) -> dict[str, Any]:
        d = asdict(self)
        d["argv"] = list(self.argv)
        return d


def _mac(key: bytes, payload: dict[str, Any]) -> str:
    return hmac.new(
        key, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256
    ).hexdigest()


def sign(key: bytes, g: Grant) -> dict[str, Any]:
    p = g.payload()
    return {"grant": p, "mac": _mac(key, p)}


def verify(key: bytes, token: Any, argv: list[str], cwd: str, *, now: float | None = None) -> Grant:
    if not isinstance(token, dict) or not isinstance(token.get("grant"), dict) or not isinstance(token.get("mac"), str):
        raise GrantError("MALFORMED")
    p = token["grant"]
    if not hmac.compare_digest(token["mac"], _mac(key, p)):
        raise GrantError("BAD_SIGNATURE")
    try:
        g = Grant(**{**p, "argv": tuple(p["argv"])})
    except TypeError as e:
        raise GrantError("MALFORMED") from e
    if list(g.argv) != argv or g.cwd != cwd:
        raise GrantError("BINDING")  # the request differs from what was admitted
    if g.expires < (now if now is not None else time.time()):
        raise GrantError("EXPIRED")
    return g
