"""DEV-06 Autonomous Browser Debugger bound strictly to attested loopback PreviewBroker leases.

Hardens P0-02 and P2-11:
- Verifies PreviewBroker lease against exact (tenant_id, owner_user_id, cycle_id, lease_id, token, port)
  AND live attested `dev_preview_instances` BEFORE spawning browser execution.
- Runs Chromium ONLY inside an isolated worker subprocess (`aiagent_development.browser_debug_worker`)
  with a stripped environment (zero AIAGENT_* secrets or DB DSNs) and network-layer DNS + route/redirect
  confinement to `http://127.0.0.1:<leased_port>`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from .contracts import ScopeDenied
from .pg_store import PreviewBroker

_ALLOWED_WORKER_ENV_KEYS = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "PLAYWRIGHT_BROWSERS_PATH",
    "PYTHONPATH",
    "LANG",
    "LC_ALL",
)
_MAX_STDOUT_BYTES = 131_072


@dataclass(frozen=True)
class DebugReport:
    commit_sha: str
    source_url_redacted: str
    console_errors: tuple[str, ...]
    network_errors: tuple[str, ...]
    layout_issues: tuple[str, ...]
    status: str  # OPEN | VERIFIED


def _sanitized_worker_env() -> dict[str, str]:
    """Strip all AIAGENT_* secrets, DB DSNs, tokens, and proxy credentials before spawning browser worker."""
    return {k: v for k, v in os.environ.items() if k in _ALLOWED_WORKER_ENV_KEYS}


def run_isolated_browser_debug_worker(
    *,
    target_url: str,
    leased_port: int,
    commit_sha: str,
    timeout_s: float = 30.0,
) -> DebugReport:
    pu = urlsplit(target_url)
    if pu.scheme != "http" or pu.hostname != "127.0.0.1" or pu.port != leased_port:
        raise ScopeDenied("browser debugger permits attested loopback 127.0.0.1 preview origin only")

    payload = json.dumps({"targetUrl": target_url, "leasedPort": leased_port, "commitSha": commit_sha}).encode("utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "aiagent_development.browser_debug_worker"],
        input=payload,
        capture_output=True,
        env=_sanitized_worker_env(),
        timeout=timeout_s,
        shell=False,
        check=False,
    )
    if proc.returncode == 3 or b"BROWSER_DEBUG_UNAVAILABLE" in proc.stderr:
        raise RuntimeError("BROWSER_DEBUG_UNAVAILABLE")
    if proc.returncode != 0:
        raise RuntimeError(f"BROWSER_DEBUG_WORKER_FAILED:{proc.returncode}")
    if len(proc.stdout) > _MAX_STDOUT_BYTES:
        raise RuntimeError("BROWSER_DEBUG_OUTPUT_TOO_LARGE")
    data = json.loads(proc.stdout.decode("utf-8"))
    return DebugReport(
        commit_sha=str(data["commitSha"]),
        source_url_redacted=str(data["sourceUrlRedacted"]),
        console_errors=tuple(str(x) for x in data.get("consoleErrors", ())),
        network_errors=tuple(str(x) for x in data.get("networkErrors", ())),
        layout_issues=tuple(str(x) for x in data.get("layoutIssues", ())),
        status=str(data["status"]),
    )


class AutonomousBrowserDebugger:
    """Binds isolated Playwright debug worker strictly to cycle-bound PreviewBroker capability leases."""

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
            return run_isolated_browser_debug_worker(
                target_url=target_url,
                leased_port=port,
                commit_sha=commit_sha,
            )
        finally:
            if revoke_after:
                self._previews.revoke_lease(tenant_id, owner_user_id, lease_id)
