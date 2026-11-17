"""Execution grants: the ONLY authority the Untrusted Execution VM accepts. Issued by the Tool Gateway after final
admission of a `sandbox_exec` intent; bound to the exact argv/cwd/limits, the run, its lease epoch and the intent;
single use; short expiry. The supervisor cannot reach the control DB, so everything it needs is in the signed grant."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

from aiagent_domain import is_uuid7

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
    attempt_id: str | None = None
    attempt_binding_version: int | None = None
    execution_binding_version: int | None = None
    execution_id: str | None = None
    execution_binding_sha256: str | None = None

    def payload(self) -> dict[str, Any]:
        d = asdict(self)
        d["argv"] = list(self.argv)
        if self.attempt_binding_version is None:
            del d["attempt_binding_version"]
        if (
            self.execution_binding_version is None
            and self.execution_id is None
            and self.execution_binding_sha256 is None
        ):
            del d["execution_binding_version"]
            del d["execution_id"]
            del d["execution_binding_sha256"]
        return d


def _mac(key: bytes, payload: dict[str, Any]) -> str:
    return hmac.new(
        key, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256
    ).hexdigest()


def sign(key: bytes, g: Grant) -> dict[str, Any]:
    p = g.payload()
    return {"grant": p, "mac": _mac(key, p)}


def verify(
    key: bytes, token: Any, argv: list[str], cwd: str, *, now: float | None = None, allow_legacy: bool = False
) -> Grant:
    if not isinstance(token, dict) or not isinstance(token.get("grant"), dict) or not isinstance(token.get("mac"), str):
        raise GrantError("MALFORMED")
    p = token["grant"]
    if not hmac.compare_digest(token["mac"], _mac(key, p)):
        raise GrantError("BAD_SIGNATURE")
    try:
        g = Grant(**{**p, "argv": tuple(p["argv"])})
    except TypeError as e:
        raise GrantError("MALFORMED") from e
    if g.attempt_binding_version is None and not allow_legacy:
        raise GrantError("LEGACY_DISABLED")
    if g.attempt_binding_version is not None and (
        type(g.attempt_binding_version) is not int or g.attempt_binding_version != 1
    ):
        raise GrantError("UNSUPPORTED_ATTEMPT_BINDING")
    if g.attempt_binding_version == 1 and not g.attempt_id:
        raise GrantError("MISSING_ATTEMPT")
    if g.attempt_id is not None and (not isinstance(g.attempt_id, str) or not 1 <= len(g.attempt_id) <= 128):
        raise GrantError("MALFORMED")
    if any(
        value is not None for value in (g.execution_binding_version, g.execution_id, g.execution_binding_sha256)
    ) and (
        type(g.execution_binding_version) is not int
        or g.execution_binding_version != 1
        or g.attempt_binding_version != 1
        or not is_uuid7(g.execution_id)
        or not isinstance(g.execution_binding_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", g.execution_binding_sha256) is None
    ):
        raise GrantError("INVALID_EXECUTION_BINDING")
    if list(g.argv) != argv or g.cwd != cwd:
        raise GrantError("BINDING")  # the request differs from what was admitted
    if g.expires < (now if now is not None else time.time()):
        raise GrantError("EXPIRED")
    return g
