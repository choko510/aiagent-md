"""Isolated headless Chromium debug worker executed INSIDE the Sandbox Cell (DEV-06 / R2-01 / R4-01).

Self-contained inside `aiagent_sandbox` so the minimal Untrusted Execution VM / Cell image NEVER needs
database drivers, gateways, `aiagent_core`, or `aiagent_development`:
- Landlock filesystem confinement (only task workspace + single bridge socket writable; zero /run/secrets)
- Unprivileged cell UID/GID + PR_SET_DUMPABLE=0 + seccomp cross-task process isolation
- Fresh CLONE_NEWUSER | CLONE_NEWPID | CLONE_NEWNET network namespace (ZERO external route, ZERO host loopback)
- Bridges ONLY `127.0.0.1:<leased_port>` inside the private netns through the Supervisor's instance-verified
  Unix domain socket (`--bridge-sock`).
- Defense-in-depth Playwright route, 3xx redirect, and WebSocket origin confinement + secret/CDP redaction.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import re
import socket
import struct
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

_VIEWPORTS: tuple[tuple[str, int, int], ...] = (
    ("desktop", 1280, 800),
    ("mobile", 390, 844),
)
_MAX_STDIN_BYTES = 16_384
_SIOCGIFFLAGS = 0x8913
_SIOCSIFFLAGS = 0x8914
_IFF_UP = 0x1

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)bearer\s+[a-z0-9._\-+/=]{8,}"),
    re.compile(r"(?i)(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}"),
    re.compile(r"(?i)github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)sk-[A-Za-z0-9\-_]{16,}"),
    re.compile(r"(?i)(?:cookie|set-cookie|authorization|x-api-key)\s*[:=]\s*[^\s;,\"']+"),
    re.compile(r"(?i)ws://127\.0\.0\.1:[0-9]+/devtools/[^\s\"']+"),
    re.compile(r"(?i)(?:token|secret|password|passwd|api[_-]?key)=[^&\s\"']+"),
)


def redact_debug_text(text: str, *, limit: int = 240) -> str:
    redacted = text
    for pat in _SECRET_PATTERNS:
        redacted = pat.sub("[REDACTED]", redacted)
    return redacted[:limit]


def _is_leased_origin(url: str, leased_port: int) -> bool:
    try:
        pu = urlsplit(url)
        port = pu.port
    except ValueError:
        return False
    return pu.scheme in ("http", "ws") and pu.hostname == "127.0.0.1" and port == leased_port


def _bring_up_isolated_loopback() -> None:
    """Bring up `lo` inside the child's private CLONE_NEWNET namespace (no external interfaces exist)."""
    if sys.platform != "linux":
        return
    import fcntl

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        ifreq = struct.pack("16sH14s", b"lo", 0, b"")
        res = fcntl.ioctl(sock.fileno(), _SIOCGIFFLAGS, ifreq)
        flags = struct.unpack("16sH14s", res)[1]
        if not (flags & _IFF_UP):
            fcntl.ioctl(sock.fileno(), _SIOCSIFFLAGS, struct.pack("16sH14s", b"lo", flags | _IFF_UP, b""))
    except OSError:
        pass
    finally:
        sock.close()


async def _start_local_unix_bridge(leased_port: int, bridge_sock: str) -> asyncio.AbstractServer:
    """Expose ONLY 127.0.0.1:<leased_port> inside the isolated netns, forwarding strictly to bridge_sock."""
    _bring_up_isolated_loopback()

    async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while chunk := await src.read(16384):
                dst.write(chunk)
                await dst.drain()
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            with contextlib.suppress(OSError):
                dst.close()

    async def _handle(tr: asyncio.StreamReader, tw: asyncio.StreamWriter) -> None:
        try:
            ur, uw = await asyncio.open_unix_connection(bridge_sock)
        except OSError:
            tw.close()
            return
        t1 = asyncio.create_task(_pipe(tr, uw))
        t2 = asyncio.create_task(_pipe(ur, tw))
        await asyncio.gather(t1, t2, return_exceptions=True)

    return await asyncio.start_server(_handle, "127.0.0.1", leased_port)


async def _run_isolated_probe(
    *,
    target_url: str,
    leased_port: int,
    commit_sha: str,
    bridge_sock: str | None = None,
) -> dict[str, Any]:
    if not _is_leased_origin(target_url, leased_port):
        raise ValueError("TARGET_OUTSIDE_LEASED_PREVIEW_ORIGIN")

    try:
        pw_mod = importlib.import_module("playwright.async_api")
    except ImportError as exc:
        raise RuntimeError("BROWSER_DEBUG_UNAVAILABLE") from exc

    bridge_srv: asyncio.AbstractServer | None = None
    if bridge_sock:
        bridge_srv = await _start_local_unix_bridge(leased_port, bridge_sock)

    console_errors: list[str] = []
    network_errors: list[str] = []
    layout_issues: list[str] = []

    launch_args = [
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-dev-shm-usage",
    ]
    if bridge_sock:
        launch_args.extend(
            [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--no-zygote",
                "--in-process-gpu",
                "--use-gl=disabled",
                "--disable-gpu",
                "--disable-gpu-compositing",
                "--disable-software-rasterizer",
            ]
        )

    try:
        async with pw_mod.async_playwright() as p:
            for vp_name, width, height in _VIEWPORTS:
                profile_dir = Path("tmp") / f"pw-{vp_name}"
                profile_dir.mkdir(parents=True, exist_ok=True)
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=True,
                    args=launch_args,
                    ignore_default_args=["--enable-unsafe-swiftshader"] if bridge_sock else None,
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
                                redact_debug_text(
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
                                    redact_debug_text(
                                        f"[{vp}] BLOCKED_REDIRECT HTTP {resp.status} -> {urlsplit(resolved).scheme}://{urlsplit(resolved).netloc}",
                                        limit=240,
                                    )
                                )
                            await route.abort("blockedbyclient")
                            return
                    await route.fulfill(response=resp)

                await context.route("**/*", _guard_route)
                page = context.pages[0] if context.pages else await context.new_page()
                page.on(
                    "websocket",
                    lambda ws, vp=vp_name: (
                        network_errors.append(
                            redact_debug_text(f"[{vp}] BLOCKED_WEBSOCKET {urlsplit(ws.url).netloc}", limit=240)
                        )
                        if not _is_leased_origin(str(ws.url), leased_port) and len(network_errors) < 30
                        else None
                    ),
                )
                page.on(
                    "console",
                    lambda msg, vp=vp_name: (
                        console_errors.append(redact_debug_text(f"[{vp}] {msg.text}", limit=240))
                        if msg.type == "error" and len(console_errors) < 30
                        else None
                    ),
                )
                page.on(
                    "response",
                    lambda resp, vp=vp_name: (
                        network_errors.append(
                            redact_debug_text(
                                f"[{vp}] HTTP {resp.status} {urlsplit(resp.url).path}",
                                limit=240,
                            )
                        )
                        if resp.status >= 400 and len(network_errors) < 30
                        else None
                    ),
                )
                page.on(
                    "pageerror",
                    lambda exc, vp=vp_name: (
                        console_errors.append(redact_debug_text(f"[{vp}] PageError: {exc}", limit=240))
                        if len(console_errors) < 30
                        else None
                    ),
                )
                try:
                    await page.goto(target_url, wait_until="networkidle", timeout=15000)
                    overflow = await page.evaluate(
                        "() => document.documentElement.scrollWidth > document.documentElement.clientWidth"
                    )
                    if overflow:
                        layout_issues.append(f"[{vp_name}] horizontal overflow detected at {width}x{height}")
                finally:
                    await context.close()
    finally:
        if bridge_srv is not None:
            bridge_srv.close()
            await bridge_srv.wait_closed()

    redacted_url = f"http://127.0.0.1:{leased_port}{urlsplit(target_url).path or '/'}"
    status = "OPEN" if (console_errors or network_errors or layout_issues) else "VERIFIED"
    return {
        "commitSha": commit_sha,
        "sourceUrlRedacted": redact_debug_text(redacted_url, limit=240),
        "consoleErrors": console_errors,
        "networkErrors": network_errors,
        "layoutIssues": layout_issues,
        "status": status,
    }


def main() -> int:
    Path("tmp").mkdir(parents=True, exist_ok=True)
    bridge_sock: str | None = None
    argv = sys.argv[1:]
    if len(argv) == 2 and argv[0] == "--bridge-sock":
        bridge_sock = argv[1]
    elif argv:
        sys.stderr.write("INVALID_ARGS\n")
        return 2
    raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
    if not raw or len(raw) > _MAX_STDIN_BYTES:
        sys.stderr.write("INVALID_WORKER_INPUT\n")
        return 2
    try:
        req = json.loads(raw.decode("utf-8"))
        target_url = str(req["targetUrl"])
        leased_port = int(req["leasedPort"])
        commit_sha = str(req["commitSha"])
    except (ValueError, KeyError, TypeError):
        sys.stderr.write("INVALID_WORKER_JSON\n")
        return 2
    try:
        out = asyncio.run(
            _run_isolated_probe(
                target_url=target_url,
                leased_port=leased_port,
                commit_sha=commit_sha,
                bridge_sock=bridge_sock,
            )
        )
    except RuntimeError as exc:
        if str(exc) == "BROWSER_DEBUG_UNAVAILABLE":
            sys.stderr.write("BROWSER_DEBUG_UNAVAILABLE\n")
            return 3
        sys.stderr.write(f"WORKER_RUNTIME_ERROR:{exc}\n")
        return 1
    except (OSError, ValueError, TimeoutError) as exc:
        sys.stderr.write(f"WORKER_ERROR:{type(exc).__name__}:{exc}\n")
        return 1
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
