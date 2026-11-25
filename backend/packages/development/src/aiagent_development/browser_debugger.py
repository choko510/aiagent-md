"""DEV-06 Autonomous Browser Debugger bound strictly to attested PreviewBroker leases & PR #110 Sandbox Cell (R2-01).

Hardens R2-01, R2-06, and P2-11:
- Verifies PreviewBroker lease against exact (tenant_id, owner_user_id, cycle_id, lease_id, token, port)
  AND live attested `dev_preview_instances` (HMAC launch receipt + active heartbeat + generation) BEFORE dispatch.
- NEVER spawns Chromium directly from the API process. Dispatches strictly to PR #110's OS-isolated Sandbox Cell
  Supervisor (`preview.debug` RPC with signed single-use Grant -> Landlock + unprivileged UID + CLONE_NEWUSER |
  CLONE_NEWPID | CLONE_NEWNET + single-target Unix bridge to ONLY host 127.0.0.1:<leased_port>).
- Fails closed (`ScopeDenied("SANDBOX_CELL_REQUIRED")`) if Sandbox Cell RPC credentials are not configured.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .contracts import ScopeDenied
from .pg_store import PreviewBroker

_MAX_STDOUT_BYTES = 131_072
_PREVIEW_DEBUG_ARGV = ("python", "-m", "aiagent_development.browser_debug_worker")


@dataclass(frozen=True)
class DebugReport:
    commit_sha: str
    source_url_redacted: str
    console_errors: tuple[str, ...]
    network_errors: tuple[str, ...]
    layout_issues: tuple[str, ...]
    status: str  # OPEN | VERIFIED


def _resolve_sandbox_cell_config() -> tuple[str, bytes, bytes]:
    """Resolve authenticated Sandbox Cell endpoint, RPC key, and Grant signing key from environment/secrets."""
    from aiagent_core import config  # noqa: PLC0415

    endpoint = (os.environ.get("AIAGENT_SANDBOX_ENDPOINT") or "").strip()
    rpc_key_sec = config.secret("AIAGENT_API_SANDBOX_RPC_KEY", required=False) or config.secret(
        "AIAGENT_SANDBOX_RPC_KEY", required=False
    )
    grant_key_sec = config.secret("AIAGENT_SANDBOX_GRANT_KEY", required=False)
    if not endpoint or rpc_key_sec is None or grant_key_sec is None:
        raise ScopeDenied("SANDBOX_CELL_REQUIRED: configure AIAGENT_SANDBOX_ENDPOINT, RPC key, and Grant key")
    try:
        rpc_key = bytes.fromhex(rpc_key_sec.reveal().strip())
        grant_key = bytes.fromhex(grant_key_sec.reveal().strip())
    except ValueError as exc:
        raise ScopeDenied("SANDBOX_CELL_KEYS_INVALID") from exc
    return endpoint, rpc_key, grant_key


def run_sandbox_cell_browser_debug(
    *,
    tenant_id: str,
    task_id: str,
    run_id: str,
    lease_id: str,
    target_url: str,
    leased_port: int,
    commit_sha: str,
    sandbox_endpoint: str | None = None,
    sandbox_rpc_key: bytes | None = None,
    sandbox_grant_key: bytes | None = None,
    caller_identity: str = "api",
    wall_ms: int = 30_000,
) -> DebugReport:
    """Execute Browser Debugger inside PR #110 Sandbox Cell over authenticated RPC with a signed single-use Grant."""
    from aiagent_core.rpc import RpcClient  # noqa: PLC0415
    from aiagent_domain.rpcwire import RpcAuthError, RpcError  # noqa: PLC0415
    from aiagent_sandbox.grant import Grant, sign  # noqa: PLC0415

    pu = urlsplit(target_url)
    if pu.scheme != "http" or pu.hostname != "127.0.0.1" or pu.port != leased_port:
        raise ScopeDenied("browser debugger permits attested loopback 127.0.0.1 preview origin only")

    if sandbox_endpoint and sandbox_rpc_key and sandbox_grant_key:
        endpoint, rpc_key, grant_key = sandbox_endpoint, sandbox_rpc_key, sandbox_grant_key
    else:
        endpoint, rpc_key, grant_key = _resolve_sandbox_cell_config()

    attempt_id = f"dbg-{uuid.uuid4().hex[:16]}"
    grant = Grant(
        grant_id=uuid.uuid4().hex,
        tenant_id=tenant_id,
        task_id=task_id,
        run_id=run_id,
        intent_id=lease_id,
        lease_epoch=1,
        argv=_PREVIEW_DEBUG_ARGV,
        cwd=".",
        wall_ms=wall_ms,
        memory_bytes=2 * 1024 * 1024 * 1024,
        expires=int(time.time()) + 120,
        attempt_id=attempt_id,
        attempt_binding_version=1,
    )
    signed_grant = sign(grant_key, grant)
    client = RpcClient(endpoint, caller_identity, rpc_key, timeout_s=wall_ms / 1000.0 + 30.0)
    try:
        res: dict[str, Any] = asyncio.run(
            client.call(
                "preview.debug",
                {
                    "grant": signed_grant,
                    "argv": list(_PREVIEW_DEBUG_ARGV),
                    "cwdRelative": ".",
                    "targetPort": leased_port,
                    "targetUrl": target_url,
                    "commitSha": commit_sha,
                },
            )
        )
    except (RpcError, RpcAuthError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"BROWSER_DEBUG_SANDBOX_RPC_FAILED:{type(exc).__name__}") from exc

    if res.get("status") != "EXITED" or res.get("attemptId") != attempt_id:
        raise RuntimeError("BROWSER_DEBUG_SANDBOX_INVALID_RESPONSE")
    exit_code = res.get("exitCode")
    stderr_text = str(res.get("stderr") or "")
    stdout_text = str(res.get("stdout") or "")
    if exit_code == 3 or "BROWSER_DEBUG_UNAVAILABLE" in stderr_text:
        raise RuntimeError("BROWSER_DEBUG_UNAVAILABLE")
    if exit_code != 0 or res.get("timedOut") is True:
        tail = stderr_text[-800:] if len(stderr_text) > 800 else stderr_text
        raise RuntimeError(f"BROWSER_DEBUG_WORKER_FAILED:{exit_code}:{tail}")
    if len(stdout_text.encode("utf-8")) > _MAX_STDOUT_BYTES:
        raise RuntimeError("BROWSER_DEBUG_OUTPUT_TOO_LARGE")
    data = json.loads(stdout_text)
    return DebugReport(
        commit_sha=str(data["commitSha"]),
        source_url_redacted=str(data["sourceUrlRedacted"]),
        console_errors=tuple(str(x) for x in data.get("consoleErrors", ())),
        network_errors=tuple(str(x) for x in data.get("networkErrors", ())),
        layout_issues=tuple(str(x) for x in data.get("layoutIssues", ())),
        status=str(data["status"]),
    )


class AutonomousBrowserDebugger:
    """Binds OS-isolated Sandbox Cell Playwright debug worker strictly to cycle-bound PreviewBroker leases."""

    def __init__(self, previews: PreviewBroker) -> None:
        self._previews = previews

    def debug_preview(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        cycle_id: str,
        lease_id: str,
        token: str,
        port: int,
        task_id: str = "",
        run_id: str = "",
        path: str = "/",
        revoke_after: bool = True,
        probe_fn: Callable[..., DebugReport] | None = None,
    ) -> DebugReport:
        if not path.startswith("/") or ".." in path or "\\" in path or len(path) > 256:
            raise ScopeDenied("invalid preview path")
        commit_sha = self._previews.verify_lease(
            tenant_id,
            owner_user_id,
            lease_id,
            token,
            port,
            expected_cycle_id=cycle_id,
        )
        target_url = f"http://127.0.0.1:{port}{path}"
        try:
            if probe_fn is not None:
                return probe_fn(target_url=target_url, commit_sha=commit_sha)
            return run_sandbox_cell_browser_debug(
                tenant_id=tenant_id,
                task_id=task_id or cycle_id,
                run_id=run_id or lease_id,
                lease_id=lease_id,
                target_url=target_url,
                leased_port=port,
                commit_sha=commit_sha,
            )
        finally:
            if revoke_after:
                self._previews.revoke_lease(tenant_id, owner_user_id, lease_id)
