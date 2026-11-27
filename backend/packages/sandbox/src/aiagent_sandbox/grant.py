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
    if not isinstance(g.grant_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", g.grant_id) is None:
        raise GrantError("MALFORMED")
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


PREVIEW_CAPABILITY_TTL_S = 60
_PREVIEW_CAP_DOMAIN = b"aiagent-preview-cap-v1:"
_SHA_HEX_RE = re.compile(r"^[0-9a-f]{40,64}$")
# R4-03: Preview endpoint MUST bind an instance-owned Unix domain socket + kernel inode + listening PID
# (NEVER a raw unauthenticated loopback TCP port that another process could hijack after exit).
_PREVIEW_UNIX_ENDPOINT_RE = re.compile(
    r"^unix://(/[A-Za-z0-9._/-]{1,180})\?ino=([1-9][0-9]{0,20})&pid=([1-9][0-9]{0,9})$"
)


def parse_preview_unix_endpoint_ref(endpoint_ref: str) -> tuple[str, int, int]:
    """Parse and validate an instance-bound Unix socket endpoint_ref (`unix://<abs_path>?ino=<ino>&pid=<pid>`)."""
    if not isinstance(endpoint_ref, str) or len(endpoint_ref) > 256:
        raise GrantError("PREVIEW_CAP_ENDPOINT_MISMATCH")
    m = _PREVIEW_UNIX_ENDPOINT_RE.fullmatch(endpoint_ref)
    if m is None:
        raise GrantError("PREVIEW_CAP_ENDPOINT_MISMATCH")
    sock_path = m.group(1)
    if ".." in sock_path.split("/") or "//" in sock_path:
        raise GrantError("PREVIEW_CAP_ENDPOINT_MISMATCH")
    return sock_path, int(m.group(2)), int(m.group(3))


@dataclass(frozen=True)
class PreviewCapability:
    """Dedicated signed capability for Browser Preview debugging (R3-02).

    Signed with `AIAGENT_PREVIEW_CAPABILITY_KEY` (strictly distinct from `AIAGENT_SANDBOX_GRANT_KEY`)
    and binds the exact preview instance, generation, target port/URL, commit SHA, lease, resource caps,
    and single-use capability ID.
    """

    capability_id: str
    tenant_id: str
    owner_user_id: str
    project_id: str
    cycle_id: str
    workspace_id: str
    task_id: str
    run_id: str
    lease_id: str
    preview_instance_id: str
    instance_generation: int
    target_port: int
    target_url: str
    endpoint_ref: str
    commit_sha: str
    wall_ms: int
    memory_bytes: int
    pids_max: int
    cpu_quota_us: int
    expires: int
    attempt_id: str
    cap_version: int = 1

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def _preview_mac(key: bytes, payload: dict[str, Any]) -> str:
    msg = _PREVIEW_CAP_DOMAIN + json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def sign_preview_capability(key: bytes, cap: PreviewCapability) -> dict[str, Any]:
    if len(key) < 32:
        raise GrantError("BAD_PREVIEW_KEY")
    p = cap.payload()
    return {"capability": p, "mac": _preview_mac(key, p)}


def verify_preview_capability(key: bytes, token: Any, *, now: float | None = None) -> PreviewCapability:
    if (
        not isinstance(token, dict)
        or not isinstance(token.get("capability"), dict)
        or not isinstance(token.get("mac"), str)
    ):
        raise GrantError("PREVIEW_CAP_MALFORMED")
    p = token["capability"]
    if not hmac.compare_digest(token["mac"], _preview_mac(key, p)):
        raise GrantError("PREVIEW_CAP_BAD_SIGNATURE")
    try:
        cap = PreviewCapability(**p)
    except TypeError as exc:
        raise GrantError("PREVIEW_CAP_MALFORMED") from exc
    if (
        type(cap.cap_version) is not int
        or cap.cap_version != 1
        or not isinstance(cap.capability_id, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", cap.capability_id) is None
        or not isinstance(cap.attempt_id, str)
        or not 1 <= len(cap.attempt_id) <= 128
        or not is_uuid7(cap.preview_instance_id)
        or not is_uuid7(cap.lease_id)
        or type(cap.instance_generation) is not int
        or cap.instance_generation < 1
        or type(cap.target_port) is not int
        or not 1024 <= cap.target_port <= 65535
        or not isinstance(cap.commit_sha, str)
        or _SHA_HEX_RE.fullmatch(cap.commit_sha) is None
        or type(cap.wall_ms) is not int
        or not 1000 <= cap.wall_ms <= 60_000
        or type(cap.memory_bytes) is not int
        or not 256 * 1024 * 1024 <= cap.memory_bytes <= 4 * 1024 * 1024 * 1024
        or type(cap.pids_max) is not int
        or not 32 <= cap.pids_max <= 512
        or type(cap.cpu_quota_us) is not int
        or not 10_000 <= cap.cpu_quota_us <= 200_000
        or type(cap.expires) is not int
    ):
        raise GrantError("PREVIEW_CAP_INVALID_FIELDS")
    from urllib.parse import urlsplit

    pu = urlsplit(cap.target_url)
    if pu.scheme != "http" or pu.hostname != "127.0.0.1" or pu.port != cap.target_port:
        raise GrantError("PREVIEW_CAP_TARGET_MISMATCH")
    parse_preview_unix_endpoint_ref(cap.endpoint_ref)
    if cap.expires < (now if now is not None else time.time()):
        raise GrantError("PREVIEW_CAP_EXPIRED")
    return cap
