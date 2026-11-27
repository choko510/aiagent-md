"""DEV-06 Autonomous Browser Debugger bound to attested PreviewBroker leases & dedicated PreviewCapability (R3-02/R3-05).

Hardens R2-01, R3-02, R3-04, and R3-05:
- API NEVER holds `AIAGENT_SANDBOX_GRANT_KEY`, `AIAGENT_SANDBOX_RPC_KEY`, or `AIAGENT_PREVIEW_CAPABILITY_KEY`.
- Production flow: API -> ToolGateway (`tool.preview_debug`, authenticated as caller `api`) -> ToolGateway verifies
  PreviewBroker lease + live attested instance -> ToolGateway signs a single-use `PreviewCapability` with dedicated
  `AIAGENT_PREVIEW_CAPABILITY_KEY` (distinct from `AIAGENT_SANDBOX_GRANT_KEY`) binding instance ID, generation,
  target port/URL, endpoint_ref, commit SHA, lease ID, resource caps, and short expiry -> Sandbox Supervisor
  (`preview.debug`, authenticated as caller `tool_gateway`).
- Monitors live lease revocation / heartbeat expiry during execution and cancels the running Browser Cell via
  `preview.cancel` RPC immediately if revoked.
- Fails closed (`ScopeDenied("SANDBOX_CELL_REQUIRED")`) if the preview execution authority is unconfigured.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .contracts import ScopeDenied
from .pg_store import PreviewBroker, PreviewLeaseDetails

_MAX_STDOUT_BYTES = 131_072


@dataclass(frozen=True)
class DebugReport:
    commit_sha: str
    source_url_redacted: str
    console_errors: tuple[str, ...]
    network_errors: tuple[str, ...]
    layout_issues: tuple[str, ...]
    status: str  # OPEN | VERIFIED


def _resolve_sandbox_preview_config() -> tuple[str, bytes, bytes]:
    """Resolve ToolGateway -> Sandbox Cell endpoint, RPC key, and dedicated PreviewCapability key (NEVER grant_key)."""
    from aiagent_core import config  # noqa: PLC0415

    endpoint = (os.environ.get("AIAGENT_SANDBOX_ENDPOINT") or "").strip()
    rpc_key_sec = config.secret("AIAGENT_SANDBOX_RPC_KEY", required=False)
    cap_key_sec = config.secret("AIAGENT_PREVIEW_CAPABILITY_KEY", required=False)
    if not endpoint or rpc_key_sec is None or cap_key_sec is None:
        raise ScopeDenied(
            "SANDBOX_CELL_REQUIRED: configure AIAGENT_SANDBOX_ENDPOINT, AIAGENT_SANDBOX_RPC_KEY, and AIAGENT_PREVIEW_CAPABILITY_KEY"
        )
    try:
        rpc_key = bytes.fromhex(rpc_key_sec.reveal().strip())
        cap_key = bytes.fromhex(cap_key_sec.reveal().strip())
    except ValueError as exc:
        raise ScopeDenied("SANDBOX_CELL_KEYS_INVALID") from exc
    grant_key_sec = config.secret("AIAGENT_SANDBOX_GRANT_KEY", required=False)
    if grant_key_sec is not None:
        try:
            if bytes.fromhex(grant_key_sec.reveal().strip()) == cap_key:
                raise ScopeDenied("PREVIEW_CAPABILITY_KEY_MUST_DIFFER_FROM_GRANT_KEY")
        except ValueError:
            pass
    return endpoint, rpc_key, cap_key


def _parse_debug_result(res: dict[str, Any], *, expected_attempt_id: str) -> DebugReport:
    status_val = res.get("status")
    if status_val == "REVOKED":
        if res.get("memoryExceeded") is True:
            raise RuntimeError("BROWSER_DEBUG_MEMORY_LIMIT_EXCEEDED")
        raise ScopeDenied("PREVIEW_LEASE_REVOKED_DURING_EXECUTION")
    if status_val != "EXITED" or res.get("attemptId") != expected_attempt_id:
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


async def run_sandbox_cell_preview_debug_async(  # noqa: PLR0912, PLR0915
    *,
    tenant_id: str,
    owner_user_id: str,
    task_id: str,
    run_id: str,
    lease: PreviewLeaseDetails,
    target_url: str,
    sandbox_endpoint: str | None = None,
    sandbox_rpc_key: bytes | None = None,
    preview_capability_key: bytes | None = None,
    caller_identity: str = "tool_gateway",
    wall_ms: int = 30_000,
    memory_bytes: int = 2 * 1024 * 1024 * 1024,
    pids_max: int = 256,
    cpu_quota_us: int = 100_000,
    lease_alive_check: Callable[[], bool] | None = None,
) -> DebugReport:
    """Execute Browser Debugger inside PR #110 Sandbox Cell via dedicated PreviewCapability + live revocation watch (R3-02/R3-05)."""
    from aiagent_core.rpc import RpcClient  # noqa: PLC0415
    from aiagent_domain.rpcwire import RpcAuthError, RpcError  # noqa: PLC0415
    from aiagent_sandbox.grant import PreviewCapability, sign_preview_capability  # noqa: PLC0415

    pu = urlsplit(target_url)
    if pu.scheme != "http" or pu.hostname != "127.0.0.1" or pu.port != lease.target_port:
        raise ScopeDenied("browser debugger permits attested loopback 127.0.0.1 preview origin only")
    if task_id != lease.task_id or run_id != lease.run_id:
        raise ScopeDenied("PREVIEW_TASK_OR_RUN_MISMATCH")

    if sandbox_endpoint and sandbox_rpc_key and preview_capability_key:
        endpoint, rpc_key, cap_key = sandbox_endpoint, sandbox_rpc_key, preview_capability_key
    else:
        endpoint, rpc_key, cap_key = _resolve_sandbox_preview_config()

    now_s = int(time.time())
    max_exp = min(
        now_s + 60,
        int(lease.lease_expires_at.timestamp()),
        int(lease.heartbeat_expires_at.timestamp()),
    )
    if max_exp <= now_s:
        raise ScopeDenied("preview lease or instance heartbeat already expired")

    capability_id = f"prvcap-{uuid.uuid4().hex}"
    attempt_id = f"dbg-{uuid.uuid4().hex[:16]}"
    cap = PreviewCapability(
        capability_id=capability_id,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        project_id=lease.project_id,
        cycle_id=lease.cycle_id,
        workspace_id=lease.workspace_id,
        task_id=lease.task_id,
        run_id=lease.run_id,
        lease_id=lease.lease_id,
        preview_instance_id=lease.instance_id,
        instance_generation=lease.generation,
        target_port=lease.target_port,
        target_url=target_url,
        endpoint_ref=lease.endpoint_ref,
        commit_sha=lease.commit_sha,
        wall_ms=wall_ms,
        memory_bytes=memory_bytes,
        pids_max=pids_max,
        cpu_quota_us=cpu_quota_us,
        expires=max_exp,
        attempt_id=attempt_id,
    )
    signed_cap = sign_preview_capability(cap_key, cap)
    client = RpcClient(endpoint, caller_identity, rpc_key, timeout_s=wall_ms / 1000.0 + 30.0)

    call_task = asyncio.create_task(client.call("preview.debug", {"capability": signed_cap}))
    watch_task: asyncio.Task[None] | None = None
    watcher_triggered = False
    if lease_alive_check is not None:

        async def _watch_lease() -> None:
            nonlocal watcher_triggered
            cancel_client = RpcClient(endpoint, caller_identity, rpc_key, timeout_s=5.0)
            while not call_task.done():
                await asyncio.sleep(0.3)
                if call_task.done():
                    return
                try:
                    alive = bool(await asyncio.to_thread(lease_alive_check))
                except Exception:  # noqa: BLE001 - R4-04: fail closed on any watcher/DB error
                    alive = False
                if not alive:
                    watcher_triggered = True
                    with contextlib.suppress(RpcError, RpcAuthError, OSError, TimeoutError):
                        await cancel_client.call(
                            "preview.cancel", {"tenantId": tenant_id, "capabilityId": capability_id}
                        )
                    # Fail closed even if cancel RPC could not immediately terminate the call.
                    await asyncio.sleep(0.5)
                    if not call_task.done():
                        call_task.cancel()
                    return

        watch_task = asyncio.create_task(_watch_lease())

    try:
        res: dict[str, Any] = await call_task
    except asyncio.CancelledError:
        if watcher_triggered:
            raise ScopeDenied("PREVIEW_LEASE_REVOKED_OR_WATCHER_FAILED") from None
        raise
    except (RpcError, RpcAuthError, OSError, TimeoutError) as exc:
        if watcher_triggered:
            raise ScopeDenied("PREVIEW_LEASE_REVOKED_OR_WATCHER_FAILED") from None
        raise RuntimeError(f"BROWSER_DEBUG_SANDBOX_RPC_FAILED:{type(exc).__name__}:{exc}") from exc
    finally:
        if watch_task is not None:
            watch_task.cancel()
            await asyncio.gather(watch_task, return_exceptions=True)

    if watcher_triggered:
        raise ScopeDenied("PREVIEW_LEASE_REVOKED_OR_WATCHER_FAILED")
    return _parse_debug_result(res, expected_attempt_id=attempt_id)


def run_sandbox_cell_browser_debug(
    *,
    tenant_id: str,
    owner_user_id: str,
    task_id: str,
    run_id: str,
    lease: PreviewLeaseDetails,
    target_url: str,
    sandbox_endpoint: str | None = None,
    sandbox_rpc_key: bytes | None = None,
    preview_capability_key: bytes | None = None,
    caller_identity: str = "tool_gateway",
    wall_ms: int = 30_000,
    lease_alive_check: Callable[[], bool] | None = None,
) -> DebugReport:
    return asyncio.run(
        run_sandbox_cell_preview_debug_async(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            task_id=task_id,
            run_id=run_id,
            lease=lease,
            target_url=target_url,
            sandbox_endpoint=sandbox_endpoint,
            sandbox_rpc_key=sandbox_rpc_key,
            preview_capability_key=preview_capability_key,
            caller_identity=caller_identity,
            wall_ms=wall_ms,
            lease_alive_check=lease_alive_check,
        )
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
        lease = self._previews.verify_lease_details(
            tenant_id,
            owner_user_id,
            lease_id,
            token,
            port,
            expected_cycle_id=cycle_id,
        )
        if (task_id and task_id != lease.task_id) or (run_id and run_id != lease.run_id):
            raise ScopeDenied("PREVIEW_TASK_OR_RUN_MISMATCH")
        target_url = f"http://127.0.0.1:{port}{path}"
        try:
            if probe_fn is not None:
                return probe_fn(target_url=target_url, commit_sha=lease.commit_sha)

            def _is_alive() -> bool:
                try:
                    live = self._previews.verify_lease_details(
                        tenant_id, owner_user_id, lease_id, token, port, expected_cycle_id=cycle_id
                    )
                    return (
                        live.task_id == lease.task_id
                        and live.run_id == lease.run_id
                        and live.goal_revision == lease.goal_revision
                    )
                except Exception:  # noqa: BLE001
                    return False

            return run_sandbox_cell_browser_debug(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                task_id=lease.task_id,
                run_id=lease.run_id,
                lease=lease,
                target_url=target_url,
                lease_alive_check=_is_alive,
            )
        finally:
            if revoke_after:
                self._previews.revoke_lease(tenant_id, owner_user_id, lease_id)
