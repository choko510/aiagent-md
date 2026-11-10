"""Process lifecycle shared by every long-running service: /healthz (liveness), /readyz (dependencies + schema
compatibility), SIGTERM/SIGINT graceful shutdown with a drain deadline."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable

log = logging.getLogger("aiagent.service")
ReadyCheck = Callable[[], Awaitable[tuple[bool, str]]]


class Lifecycle:
    def __init__(self, name: str, *, health_port: int | None = None, health_host: str = "127.0.0.1") -> None:
        self.name = name
        self.stopping = asyncio.Event()
        self.health_port, self.health_host = health_port, health_host
        self.checks: dict[str, ReadyCheck] = {}
        self._server: asyncio.base_events.Server | None = None

    def install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):  # Windows: SIGTERM via loop unsupported
                loop.add_signal_handler(sig, self.stop)

    def stop(self) -> None:
        if not self.stopping.is_set():
            log.info("shutdown requested", extra={"fields": {"service": self.name}})
            self.stopping.set()

    async def ready(self) -> tuple[bool, dict[str, str]]:
        detail: dict[str, str] = {}
        ok = not self.stopping.is_set()
        if not ok:
            detail["lifecycle"] = "draining"
        for name, check in self.checks.items():
            try:
                good, why = await asyncio.wait_for(check(), timeout=5)
            except Exception as e:
                good, why = False, type(e).__name__
            ok = ok and good
            detail[name] = "ok" if good else why
        return ok, detail

    async def start_health(self) -> None:
        if self.health_port is None:
            return

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            import json

            try:
                line = (await asyncio.wait_for(reader.readline(), timeout=5)).decode("latin-1")
                path = line.split(" ")[1] if " " in line else "/"
                code: int
                body: dict[str, object]
                if path == "/healthz":
                    code, body = 200, {"status": "alive", "service": self.name}
                elif path == "/readyz":
                    ok, detail = await self.ready()
                    code, body = (200 if ok else 503), {"ready": ok, "checks": detail}
                else:
                    code, body = 404, {"error": "not found"}
                data = json.dumps(body).encode()
                writer.write(
                    f"HTTP/1.1 {code} X\r\ncontent-type: application/json\r\ncontent-length: {len(data)}\r\n"
                    "connection: close\r\n\r\n".encode()
                    + data
                )
                await writer.drain()
            except Exception:
                pass
            finally:
                writer.close()

        self._server = await asyncio.start_server(handle, self.health_host, self.health_port)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
