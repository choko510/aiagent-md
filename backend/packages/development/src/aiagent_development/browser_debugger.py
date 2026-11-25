"""DEV-06 Autonomous Browser Debugger bound strictly to short-lived loopback PreviewBroker leases.

Never exposes raw cookies, credentials, external URLs, or Chrome DevTools WebSocket URLs.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from .contracts import ScopeDenied
from .design_demo import redact_dev_text
from .pg_store import PreviewBroker

_VIEWPORTS: tuple[tuple[str, int, int], ...] = (
    ("desktop", 1280, 800),
    ("mobile", 390, 844),
)


@dataclass(frozen=True)
class DebugReport:
    commit_sha: str
    source_url_redacted: str
    console_errors: tuple[str, ...]
    network_errors: tuple[str, ...]
    layout_issues: tuple[str, ...]
    status: str  # OPEN | VERIFIED


async def run_browser_debug_probe(
    *,
    target_url: str,
    commit_sha: str,
) -> DebugReport:
    """Inspect a loopback preview URL in headless Chromium across desktop & mobile viewports."""
    pu = urlsplit(target_url)
    if pu.scheme != "http" or pu.hostname != "127.0.0.1" or pu.port is None:
        raise ScopeDenied("browser debugger permits loopback 127.0.0.1 preview URLs only")

    try:
        pw_mod = importlib.import_module("playwright.async_api")
    except ImportError as exc:
        raise RuntimeError("BROWSER_DEBUG_UNAVAILABLE") from exc

    console_errors: list[str] = []
    network_errors: list[str] = []
    layout_issues: list[str] = []

    async with pw_mod.async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for vp_name, width, height in _VIEWPORTS:
                context = await browser.new_context(viewport={"width": width, "height": height})
                page = await context.new_page()
                page.on(
                    "console",
                    lambda msg, vp=vp_name: (
                        console_errors.append(redact_dev_text(f"[{vp}] {msg.text}", limit=240))
                        if msg.type == "error" and len(console_errors) < 30
                        else None
                    ),
                )
                page.on(
                    "response",
                    lambda resp, vp=vp_name: (
                        network_errors.append(
                            redact_dev_text(
                                f"[{vp}] HTTP {resp.status} {urlsplit(resp.url).path}",
                                limit=240,
                            )
                        )
                        if resp.status >= 400 and len(network_errors) < 30
                        else None
                    ),
                )
                page.on(
                    "requestfailed",
                    lambda req, vp=vp_name: (
                        network_errors.append(
                            redact_dev_text(
                                f"[{vp}] FAILED {urlsplit(req.url).path}",
                                limit=240,
                            )
                        )
                        if len(network_errors) < 30
                        else None
                    ),
                )
                try:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=10000)
                    overflow = await page.evaluate(
                        "() => ({sw: document.documentElement.scrollWidth, iw: window.innerWidth})"
                    )
                    if isinstance(overflow, dict) and int(overflow.get("sw", 0)) > int(overflow.get("iw", 0)):
                        layout_issues.append(
                            f"[{vp_name}] horizontal overflow: scrollWidth={overflow['sw']} > innerWidth={overflow['iw']}"
                        )
                finally:
                    await context.close()
        finally:
            await browser.close()

    has_issues = bool(console_errors or network_errors or layout_issues)
    redacted_url = f"http://127.0.0.1:{pu.port}{pu.path or '/'}"
    return DebugReport(
        commit_sha=commit_sha,
        source_url_redacted=redacted_url,
        console_errors=tuple(console_errors),
        network_errors=tuple(network_errors),
        layout_issues=tuple(layout_issues),
        status="OPEN" if has_issues else "VERIFIED",
    )


class AutonomousBrowserDebugger:
    """Binds Playwright debug probes strictly to valid PreviewBroker capability leases."""

    def __init__(self, previews: PreviewBroker) -> None:
        self._previews = previews

    def debug_preview(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        lease_id: str,
        token: str,
        port: int,
        path: str = "/",
        revoke_after: bool = True,
        probe_fn: Callable[..., DebugReport] | None = None,
    ) -> DebugReport:
        if not path.startswith("/") or ".." in path or "\\" in path or len(path) > 256:
            raise ScopeDenied("invalid preview path")
        commit_sha = self._previews.verify_lease(tenant_id, owner_user_id, lease_id, token, port)
        target_url = f"http://127.0.0.1:{port}{path}"
        try:
            if probe_fn is not None:
                return probe_fn(target_url=target_url, commit_sha=commit_sha)
            return asyncio.run(run_browser_debug_probe(target_url=target_url, commit_sha=commit_sha))
        finally:
            if revoke_after:
                self._previews.revoke_lease(tenant_id, owner_user_id, lease_id)
