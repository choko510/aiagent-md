"""Internal signing for captured bytes after a trusted Supervisor execution.

This is not VM/image attestation. Call only from a Supervisor-controlled path;
no RPC endpoint exposes this function to a Cell program or model.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import stat
import sys
import time
from collections.abc import Callable
from typing import Any

from aiagent_domain import canonical_json, sha256_hex

from .grant import Grant, GrantError


def validate_capture_binding(key: bytes, grant: Grant, binding: dict[str, Any]) -> None:
    """Check the signed grant's binding before any projected process starts."""
    if (
        len(key) < 32
        or grant.expires < time.time()
        or grant.execution_binding_version != 1
        or grant.execution_id is None
        or grant.execution_binding_sha256 is None
        or sha256_hex(binding) != grant.execution_binding_sha256
        or binding.get("execution") != grant.execution_id
        or binding.get("attempt") != grant.attempt_id
        or binding.get("intent") != grant.intent_id
        or binding.get("tenant") != grant.tenant_id
        or binding.get("task") != grant.task_id
        or binding.get("run") != grant.run_id
        or binding.get("lease_epoch") != grant.lease_epoch
        or not isinstance(binding.get("owner"), str)
        or not binding.get("owner")
        or type(binding.get("goal_revision")) is not int
        or binding["goal_revision"] < 1
    ):
        raise GrantError("CAPTURE_BINDING_INVALID")


def sign_capture(
    key: bytes,
    grant: Grant,
    binding: dict[str, Any],
    fd: int,
    *,
    expected_sha256: str,
    size: int,
    deadline: float,
    active: Callable[[], bool] = lambda: True,
) -> bytes:
    """Bind a captured unlinked file to a previously signed canonical binding."""
    validate_capture_binding(key, grant, binding)
    if (
        sys.platform != "linux"
        or not math.isfinite(deadline)
        or not time.monotonic() < deadline <= time.monotonic() + 300
        or type(size) is not int
        or not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        or not 1 <= size <= 4 * 1024 * 1024
    ):
        raise GrantError("CAPTURE_BINDING_INVALID")
    import fcntl

    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 0
            or before.st_uid != os.geteuid()
            or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY
            or before.st_size != size
        ):
            raise GrantError("CAPTURE_DESCRIPTOR_INVALID")
    except OSError as exc:
        raise GrantError("CAPTURE_DESCRIPTOR_INVALID") from exc
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        if time.monotonic() > deadline or not active():
            raise GrantError("CAPTURE_DEADLINE")
        block = os.pread(fd, min(65536, size - offset), offset)
        if not block:
            raise GrantError("CAPTURE_INCOMPLETE")
        digest.update(block)
        offset += len(block)
    after = os.fstat(fd)
    if (
        time.monotonic() > deadline
        or not active()
        or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    ):
        raise GrantError("CAPTURE_CHANGED")
    if not hmac.compare_digest(digest.hexdigest(), expected_sha256):
        raise GrantError("CAPTURE_DIGEST_MISMATCH")
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
        "artifact_sha256": expected_sha256,
        "artifact_bytes": size,
        "issued_at": int(time.time()),
    }
    mac = hmac.new(key, canonical_json(payload).encode("utf-8"), hashlib.sha256).hexdigest()
    return json.dumps({"payload": payload, "mac": mac}, sort_keys=True, separators=(",", ":")).encode("utf-8")
