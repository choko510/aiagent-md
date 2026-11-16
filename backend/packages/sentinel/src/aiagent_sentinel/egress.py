"""Trusted Egress proxy - the only component with an Internet route (enforced by the network layout, see
deploy/: internal-only Docker networks / nftables; this proxy is the policy enforcement point on top of that).

Per connection: authenticate the client identity (`Proxy-Authorization: Aiagent <identity>:<token>`), bind the
request to a configured destination, verify the Sentinel permit at send time (single use, intent still admitted,
approval not revoked, run not cancelled, same restore generation), resolve DNS *here*, vet every address, then
connect to the vetted IP (no second resolution -> no DNS rebinding). Only absolute-form HTTP is allowed for Tool permits; opaque HTTPS CONNECT is exclusively for
the independent model-provider identity. Everything else (origin-form, tunnels to other ports, chunked uploads,
unknown identities) is refused with 403 `x-aiagent-egress: denied`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import socket
import ssl
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from .authority import EgressConfig, Sentinel
from .netpolicy import EgressDenied, normalize_host, vet_addresses

log = logging.getLogger("aiagent.egress")
MAX_HEAD = 16 * 1024
MAX_FORWARD_BODY = 1_048_576
Resolver = Callable[[str, int], Awaitable[list[str]]]


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for *_, sa in infos:
        if sa[0] not in seen:
            seen.append(str(sa[0]))
    return seen


class EgressProxy:
    def __init__(
        self,
        sentinel: Sentinel,
        config: EgressConfig,
        *,
        resolver: Resolver = system_resolver,
        connect_timeout_s: float = 10.0,
        idle_timeout_s: float = 120.0,
        upstream_ssl: ssl.SSLContext | None = None,
    ) -> None:
        self.sentinel, self.config, self.resolver = sentinel, config, resolver
        self.connect_timeout_s, self.idle_timeout_s = connect_timeout_s, idle_timeout_s
        self.upstream_ssl = upstream_ssl
        self._server: asyncio.base_events.Server | None = None

    async def start(self, host: str, port: int) -> int:
        self._server = await asyncio.start_server(self._handle, host, port)
        return int(self._server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self._server:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    @staticmethod
    async def _deny(w: asyncio.StreamWriter, code: str) -> None:
        body = f'{{"denied":"{code}"}}'.encode()
        w.write(
            b"HTTP/1.1 403 Forbidden\r\nx-aiagent-egress: denied\r\ncontent-type: application/json\r\n"
            + f"content-length: {len(body)}\r\nconnection: close\r\n\r\n".encode()
            + body
        )
        with contextlib.suppress(Exception):
            await w.drain()

    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        tenant = pid = host = ip = None
        port = None
        try:
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout=10)
            if len(head) > MAX_HEAD:
                raise EgressDenied("HEAD_TOO_LARGE")
            lines = head.decode("latin-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            headers: list[tuple[str, str]] = []
            for ln in lines[1:]:
                if ln:
                    k, _, v = ln.partition(":")
                    headers.append((k.strip(), v.strip()))
            hmap = {k.lower(): v for k, v in headers}
            if method == "CONNECT":
                h, _, p = target.rpartition(":")
                scheme, host, port, inner = "https", normalize_host(h), int(p), None
            else:
                u = urlsplit(target)
                if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
                    raise EgressDenied("ORIGIN_FORM_OR_BAD_URL")
                scheme = u.scheme.lower()
                host = normalize_host(u.hostname)
                port = u.port or (443 if scheme == "https" else 80)
                inner = (u.path or "/") + (f"?{u.query}" if u.query else "")
            dest = self.config.match(scheme, host, port)
            if dest is None:
                raise EgressDenied("DESTINATION_NOT_ALLOWED")
            auth = hmap.get("proxy-authorization", "")
            if not auth.startswith("Aiagent ") or ":" not in auth:
                raise EgressDenied("PROXY_AUTH")
            identity, token = auth[8:].split(":", 1)
            human_takeover = identity == "human_takeover" and dest.needs_permit and method != "CONNECT"
            if identity not in dest.identities and not human_takeover:
                raise EgressDenied("IDENTITY_NOT_ALLOWED")
            # A TLS tunnel can contain unlimited operations; only the separate model
            # provider identity can open one, never a Tool or Human Takeover permit.
            if method == "CONNECT" and (dest.needs_permit or identity != "model_gateway"):
                raise EgressDenied("CONNECT_TOOL_FORBIDDEN")
            if hmap.get("transfer-encoding"):
                raise EgressDenied("CHUNKED_NOT_SUPPORTED")
            pre_read_body: bytes | None = None
            if identity == "human_takeover":
                assert inner is not None
                raw_cl = hmap.get("content-length", "0").strip()
                if raw_cl and not raw_cl.isdigit():
                    raise EgressDenied("BODY_TOO_LARGE")
                cl_val = int(raw_cl or "0")
                if cl_val < 0 or cl_val > MAX_FORWARD_BODY:
                    raise EgressDenied("BODY_TOO_LARGE")
                pre_read_body = await asyncio.wait_for(r.readexactly(cl_val), timeout=10) if cl_val else b""
                body_sha = hashlib.sha256(pre_read_body).hexdigest()
                tenant, pid = await asyncio.to_thread(
                    self.sentinel.verify_human_takeover,
                    token,
                    scheme,
                    host,
                    port,
                    method,
                    inner,
                    body_sha,
                )
            elif dest.needs_permit:
                tenant, pid = await asyncio.to_thread(
                    self.sentinel.verify_and_consume, token, scheme, host, port, method
                )
            else:
                expected = self.config.static_tokens.get(identity, "")
                if not expected or not hmac.compare_digest(expected, token):
                    raise EgressDenied("PROXY_AUTH")
            if method != "CONNECT" and method not in dest.methods:
                raise EgressDenied("METHOD_NOT_ALLOWED")
            if method == "CONNECT" and (scheme != "https" or not dest.methods):
                raise EgressDenied("CONNECT_NOT_ALLOWED")
            addrs = vet_addresses(await self.resolver(host, port), allow_private=dest.allow_private)
            ip = addrs[0]
            if method != "CONNECT" and scheme == "https":
                ssl_ctx = self.upstream_ssl if self.upstream_ssl is not None else ssl.create_default_context()
                ur, uw = await asyncio.wait_for(
                    asyncio.open_connection(ip, port, ssl=ssl_ctx, server_hostname=host),
                    timeout=self.connect_timeout_s,
                )
            else:
                ur, uw = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=self.connect_timeout_s)
            await asyncio.to_thread(self.sentinel.receipt, tenant, pid, "ALLOWED", "FORWARDED", host, port, ip)
            # Static Model Gateway egress has no Tool permit/receipt, so emit a
            # metadata-only record at the actual forwarding boundary. Never log
            # URL paths, request headers/body, provider keys or proxy tokens.
            log.info(
                "egress forwarded",
                extra={
                    "fields": {
                        "identity": identity,
                        "scheme": scheme,
                        "host": host,
                        "port": port,
                        "method": method,
                        "authorization": "permit" if dest.needs_permit else "static_destination",
                    }
                },
            )
            if method == "CONNECT":
                w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await w.drain()
            else:
                hop = {
                    "proxy-authorization",
                    "proxy-connection",
                    "connection",
                    "keep-alive",
                    "te",
                    "trailer",
                    "upgrade",
                }
                host_header = host if (scheme, port) in (("http", 80), ("https", 443)) else f"{host}:{port}"
                out = [f"{method} {inner} {version}", f"Host: {host_header}", "Connection: close"]
                out += [f"{k}: {v}" for k, v in headers if k.lower() not in hop and k.lower() != "host"]
                if pre_read_body is not None:
                    uw.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
                    if pre_read_body:
                        uw.write(pre_read_body)
                else:
                    n = int(hmap.get("content-length", "0") or 0)
                    if n < 0 or n > MAX_FORWARD_BODY:
                        raise EgressDenied("BODY_TOO_LARGE")
                    uw.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
                    if n:
                        uw.write(await r.readexactly(n))
                await uw.drain()
            await self._splice(r, w, ur, uw, method == "CONNECT")
        except EgressDenied as e:
            await asyncio.to_thread(self.sentinel.receipt, tenant, pid, "DENIED", e.code, host, port, ip)
            await self._deny(w, e.code)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ValueError, OSError) as e:
            if host is not None:  # a bare TCP probe (health check) is not an egress attempt
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.sentinel.receipt, tenant, pid, "DENIED", "PROXY_ERROR", host, port, ip)
            log.info("egress error", extra={"fields": {"exc": type(e).__name__}})
            await self._deny(w, "PROXY_ERROR")
        finally:
            with contextlib.suppress(Exception):
                w.close()

    async def _splice(
        self,
        cr: asyncio.StreamReader,
        cw: asyncio.StreamWriter,
        ur: asyncio.StreamReader,
        uw: asyncio.StreamWriter,
        both: bool,
    ) -> None:
        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            with contextlib.suppress(Exception):
                while data := await asyncio.wait_for(src.read(65536), timeout=self.idle_timeout_s):
                    dst.write(data)
                    await dst.drain()
            with contextlib.suppress(Exception):
                dst.close()

        tasks = [asyncio.create_task(pump(ur, cw))]
        if both:
            tasks.append(asyncio.create_task(pump(cr, uw)))
        await asyncio.gather(*tasks)
        with contextlib.suppress(Exception):
            uw.close()
