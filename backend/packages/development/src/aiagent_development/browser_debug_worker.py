"""Isolated headless Chromium debug worker process (DEV-06 / P0-02).

Executed as a dedicated subprocess with a stripped environment (zero AIAGENT_* secrets or DB DSNs).
Enforces BOTH network-layer DNS confinement (`--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1`)
AND Playwright route/redirect interception restricted strictly to `http://127.0.0.1:<leased_port>`.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from typing import Any
from urllib.parse import urljoin, urlsplit

from .design_demo import redact_dev_text

_VIEWPORTS: tuple[tuple[str, int, int], ...] = (
    ("desktop", 1280, 800),
    ("mobile", 390, 844),
)
_MAX_STDIN_BYTES = 16_384


def _is_leased_origin(url: str, leased_port: int) -> bool:
    try:
        pu = urlsplit(url)
        port = pu.port
    except ValueError:
        return False
    return pu.scheme == "http" and pu.hostname == "127.0.0.1" and port == leased_port


async def _run_isolated_probe(*, target_url: str, leased_port: int, commit_sha: str) -> dict[str, Any]:  # noqa: PLR0915
    if not _is_leased_origin(target_url, leased_port):
        raise ValueError("TARGET_OUTSIDE_LEASED_PREVIEW_ORIGIN")

    try:
        pw_mod = importlib.import_module("playwright.async_api")
    except ImportError as exc:
        raise RuntimeError("BROWSER_DEBUG_UNAVAILABLE") from exc

    console_errors: list[str] = []
    network_errors: list[str] = []
    layout_issues: list[str] = []

    async with pw_mod.async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                "--disable-background-networking",
                "--disable-sync",
            ],
        )
        try:
            for vp_name, width, height in _VIEWPORTS:
                context = await browser.new_context(
                    viewport={"width": width, "height": height},
                    service_workers="block",
                    accept_downloads=False,
                    ignore_https_errors=False,
                )

                async def _guard_route(route: Any, vp: str = vp_name) -> None:
                    req_url = str(route.request.url)
                    if not _is_leased_origin(req_url, leased_port):
                        if len(network_errors) < 30:
                            network_errors.append(
                                redact_dev_text(
                                    f"[{vp}] BLOCKED_EGRESS {urlsplit(req_url).scheme}://{urlsplit(req_url).netloc}{urlsplit(req_url).path}",
                                    limit=240,
                                )
                            )
                        await route.abort("blockedbyclient")
                        return
                    try:
                        resp = await route.fetch(max_redirects=0)
                    except (OSError, TimeoutError, RuntimeError):
                        await route.abort("failed")
                        return
                    if 300 <= resp.status < 400:
                        loc = resp.headers.get("location") or resp.headers.get("Location") or ""
                        resolved = urljoin(req_url, loc) if loc else ""
                        if not resolved or not _is_leased_origin(resolved, leased_port):
                            if len(network_errors) < 30:
                                network_errors.append(
                                    redact_dev_text(
                                        f"[{vp}] BLOCKED_REDIRECT HTTP {resp.status} -> {urlsplit(resolved).scheme}://{urlsplit(resolved).netloc}",
                                        limit=240,
                                    )
                                )
                            await route.abort("blockedbyclient")
                            return
                    await route.fulfill(response=resp)

                await context.route("**/*", _guard_route)
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
                try:
                    try:
                        await page.goto(target_url, wait_until="domcontentloaded", timeout=10000)
                        await page.wait_for_timeout(150)
                    except RuntimeError:
                        # Blocked top-level redirect or aborted navigation surfaces via network_errors.
                        pass
                    overflow = await page.evaluate(
                        "() => ({sw: document.documentElement ? document.documentElement.scrollWidth : 0, iw: window.innerWidth})"
                    )
                    if isinstance(overflow, dict) and int(overflow.get("sw", 0)) > int(overflow.get("iw", 0)):
                        layout_issues.append(
                            f"[{vp_name}] horizontal overflow: scrollWidth={overflow['sw']} > innerWidth={overflow['iw']}"
                        )
                finally:
                    await context.close()
        finally:
            await browser.close()

    pu = urlsplit(target_url)
    has_issues = bool(console_errors or network_errors or layout_issues)
    return {
        "commitSha": commit_sha,
        "sourceUrlRedacted": f"http://127.0.0.1:{pu.port}{pu.path or '/'}",
        "consoleErrors": console_errors,
        "networkErrors": network_errors,
        "layoutIssues": layout_issues,
        "status": "OPEN" if has_issues else "VERIFIED",
    }


def main() -> int:
    raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
    if not raw or len(raw) > _MAX_STDIN_BYTES:
        sys.stderr.write("INVALID_WORKER_INPUT\n")
        return 2
    try:
        req = json.loads(raw.decode("utf-8"))
        target_url = str(req["targetUrl"])
        leased_port = int(req["leasedPort"])
        commit_sha = str(req["commitSha"])
    except (KeyError, ValueError, TypeError):
        sys.stderr.write("INVALID_WORKER_JSON\n")
        return 2
    try:
        out = asyncio.run(_run_isolated_probe(target_url=target_url, leased_port=leased_port, commit_sha=commit_sha))
    except RuntimeError as exc:
        if str(exc) == "BROWSER_DEBUG_UNAVAILABLE":
            sys.stderr.write("BROWSER_DEBUG_UNAVAILABLE\n")
            return 3
        sys.stderr.write(f"WORKER_RUNTIME_ERROR: {type(exc).__name__}\n")
        return 4
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
