"""Signed bounded command outcomes; stdout/stderr never leave as plaintext."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

from aiagent_domain import canonical_json

from .capture_signing import validate_capture_binding
from .grant import Grant

_DOMAIN = b"aiagent-command-receipt-v1\0"
_FIELDS = frozenset(
    {
        "version",
        "tenant",
        "owner",
        "task",
        "run",
        "goal",
        "lease",
        "execution",
        "attempt",
        "binding_sha256",
        "snapshot",
        "workspace",
        "workspace_revision",
        "intent",
        "source_commit",
        "recipe",
        "kind",
        "test_id",
        "toolchain_sha256",
        "exit_code",
        "timed_out",
        "truncated",
        "stdout_sha256",
        "stderr_sha256",
        "issued_at",
    }
)


def sign_command_receipt(
    key: bytes,
    grant: Grant,
    binding: dict[str, Any],
    descriptor: dict[str, Any],
    result: dict[str, Any],
) -> tuple[bytes, bytes]:
    """Sign actual bounded process outcomes without returning command output."""
    validate_capture_binding(key, grant, binding)
    if (
        result.get("status") != "EXITED"
        or type(result.get("exitCode")) is not int
        or type(result.get("timedOut")) is not bool
        or type(result.get("truncated")) is not bool
        or not isinstance(result.get("stdout"), str)
        or not isinstance(result.get("stderr"), str)
        or result.get("executionId") != grant.execution_id
        or result.get("attemptId") != grant.attempt_id
    ):
        raise ValueError("command result unavailable")
    payload = {
        "version": 1,
        "tenant": grant.tenant_id,
        "owner": binding["owner"],
        "task": grant.task_id,
        "run": grant.run_id,
        "goal": binding["goal_revision"],
        "lease": grant.lease_epoch,
        "execution": grant.execution_id,
        "attempt": grant.attempt_id,
        "binding_sha256": grant.execution_binding_sha256,
        "snapshot": binding["snapshot"],
        "workspace": binding["workspace"],
        "workspace_revision": binding["workspace_revision"],
        "intent": binding["intent"],
        "source_commit": binding["commit"],
        "recipe": binding["recipe_id"],
        "kind": descriptor["kind"],
        "test_id": descriptor["test_id"],
        "toolchain_sha256": binding["toolchain_sha256"],
        "exit_code": result["exitCode"],
        "timed_out": result["timedOut"],
        "truncated": result["truncated"],
        "stdout_sha256": hashlib.sha256(result["stdout"].encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(result["stderr"].encode("utf-8")).hexdigest(),
        "issued_at": int(time.time()),
    }
    body = canonical_json(payload).encode("utf-8")
    proof = hmac.new(key, _DOMAIN + body, hashlib.sha256).hexdigest().encode("ascii")
    return body, proof


def verify_command_receipt(
    key: bytes,
    body: bytes,
    proof: bytes,
    binding: dict[str, Any],
    descriptor: dict[str, Any],
    *,
    now: float | None = None,
    max_age_seconds: int | None = 120,
) -> dict[str, Any]:
    """Authenticate the exact outcome and full canonical Execution identity."""
    if (
        len(key) < 32
        or not isinstance(body, bytes)
        or not 1 <= len(body) <= 16384
        or not isinstance(proof, bytes)
        or (max_age_seconds is not None and (type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 300))
    ):
        raise ValueError("command receipt invalid")
    try:
        expected_mac = hmac.new(key, _DOMAIN + body, hashlib.sha256).hexdigest().encode("ascii")
        payload = json.loads(body)
        if not hmac.compare_digest(proof, expected_mac) or not isinstance(payload, dict) or set(payload) != _FIELDS:
            raise ValueError("command receipt invalid")
        current = time.time() if now is None else now
        expected = {
            "version": 1,
            "tenant": binding["tenant"],
            "owner": binding["owner"],
            "task": binding["task"],
            "run": binding["run"],
            "goal": binding["goal_revision"],
            "lease": binding["lease_epoch"],
            "execution": binding["execution"],
            "attempt": binding["attempt"],
            "snapshot": binding["snapshot"],
            "workspace": binding["workspace"],
            "workspace_revision": binding["workspace_revision"],
            "intent": binding["intent"],
            "source_commit": binding["commit"],
            "recipe": binding["recipe_id"],
            "kind": descriptor["kind"],
            "test_id": descriptor["test_id"],
            "toolchain_sha256": binding["toolchain_sha256"],
        }
        from aiagent_domain import sha256_hex

        expected["binding_sha256"] = sha256_hex(binding)
        if (
            canonical_json(payload).encode("utf-8") != body
            or any(payload[k] != value for k, value in expected.items())
            or type(payload["version"]) is not int
            or type(payload["goal"]) is not int
            or type(payload["lease"]) is not int
            or type(payload["workspace_revision"]) is not int
            or type(payload["exit_code"]) is not int
            or not -(2**31) <= payload["exit_code"] < 2**31
            or type(payload["timed_out"]) is not bool
            or type(payload["truncated"]) is not bool
            or type(payload["issued_at"]) is not int
            or payload["issued_at"] <= 0
            or payload["issued_at"] > current + 5
            or (max_age_seconds is not None and payload["issued_at"] < current - max_age_seconds)
            or any(
                type(payload[k]) is not str
                or len(payload[k]) != 64
                or any(ch not in "0123456789abcdef" for ch in payload[k])
                for k in ("stdout_sha256", "stderr_sha256")
            )
        ):
            raise ValueError("command receipt invalid")
        return payload
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("command receipt invalid") from exc
