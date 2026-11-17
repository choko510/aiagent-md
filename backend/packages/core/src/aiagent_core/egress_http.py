"""Trusted Forward-Proxy HTTP/1.1 client shared by Human Takeover and Notification Delivery.

Enforces single-request strict framing (no chunked request, explicit Content-Length), HMAC proof validation
on 403/407 Sentinel denials, strict chunked EOF/trailer termination, and explicit pre-send vs post-send
outcome separation so post-send disconnects/timeouts are never blindly retried as unsent failures.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import re
from urllib.parse import urlsplit

_CHUNK_HEX_RE = re.compile(rb"^[0-9a-fA-F]{1,16}$")
_MAX_CHUNK_LINE_BYTES = 512
_MAX_CHUNKS = 4096
_CHUNK_READ_BLOCK = 8192


class ForwardProxyError(Exception):
    """Typed error distinguishing pre-send rejections from post-send uncertain outcomes."""

    def __init__(self, code: str, *, http_status: int = 502, post_send: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.post_send = post_send


def is_authenticated_proxy_denial(status_code: int, hmap: dict[str, str], permit: str) -> bool:
    if status_code != 403 or hmap.get("x-aiagent-egress") != "denied" or not permit:
        return False
    proof = hmap.get("x-aiagent-egress-proof", "")
    code_part, sep, mac_part = proof.partition(":")
    if not sep or not code_part or not mac_part:
        return False
    key = hashlib.sha256(permit.encode("utf-8")).digest()
    expected = hmac.new(key, f"DENIED:{code_part}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac_part, expected)


async def send_via_forward_proxy(
    proxy_url: str,
    *,
    identity: str,
    permit: str,
    method: str,
    target_url: str,
    host_header: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout_s: float = 15.0,
    max_response_bytes: int = 524_288,
) -> tuple[int, list[str], str]:
    """Send ONE HTTP/1.1 request through Sentinel Forward Proxy (never CONNECT)."""
    pu = urlsplit(proxy_url)
    if pu.scheme != "http" or not pu.hostname:
        raise ForwardProxyError("INVALID_EGRESS_PROXY", http_status=502, post_send=False)
    lines = [
        f"{method} {target_url} HTTP/1.1",
        f"Host: {host_header}",
        f"Proxy-Authorization: Aiagent {identity}:{permit}",
        "Connection: close",
    ]
    for k, v in headers.items():
        if k.lower() not in ("host", "proxy-authorization", "connection", "content-length"):
            lines.append(f"{k}: {v}")
    if body is not None:
        lines.append(f"Content-Length: {len(body)}")
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    if body:
        raw += body

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s

    def rem_timeout() -> float:
        rem = deadline - loop.time()
        if rem <= 0:
            raise TimeoutError("forward proxy timeout")
        return rem

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(pu.hostname, pu.port or 80), timeout=rem_timeout()
        )
    except OSError:
        raise ForwardProxyError("EGRESS_PROXY_UNREACHABLE", http_status=502, post_send=False) from None
    except TimeoutError:
        raise ForwardProxyError("EGRESS_PROXY_TIMEOUT", http_status=504, post_send=False) from None

    sent_bytes = False
    try:
        writer.write(raw)
        sent_bytes = True
        await asyncio.wait_for(writer.drain(), timeout=rem_timeout())
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=rem_timeout())
        head_lines = head[:-4].decode("latin-1").split("\r\n")
        parts = head_lines[0].split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise ForwardProxyError("INVALID_PROXY_RESPONSE", http_status=502, post_send=True)
        status_code = int(parts[1])
        set_cookies: list[str] = []
        hmap: dict[str, str] = {}
        for ln in head_lines[1:]:
            if ":" in ln:
                hk, _, hv = ln.partition(":")
                kc, vc = hk.strip(), hv.strip()
                hmap[kc.lower()] = vc
                if kc.lower() == "set-cookie":
                    set_cookies.append(vc)
        if is_authenticated_proxy_denial(status_code, hmap, permit):
            reason = str(hmap.get("x-aiagent-egress-proof") or "").partition(":")[0].strip() or "EGRESS_DENIED"
            # Sentinel denied at the proxy BEFORE dialing or forwarding upstream.
            raise ForwardProxyError(reason, http_status=403, post_send=False)
        chunks: list[bytes] = []
        total = 0
        cap = max_response_bytes
        if "chunked" in hmap.get("transfer-encoding", "").lower():
            chunk_count = 0
            saw_eof = False
            while True:
                chunk_count += 1
                if chunk_count > _MAX_CHUNKS:
                    raise ForwardProxyError("INVALID_PROXY_RESPONSE", http_status=502, post_send=True)
                try:
                    sz_line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=rem_timeout())
                except asyncio.LimitOverrunError:
                    raise ForwardProxyError("INVALID_PROXY_RESPONSE", http_status=502, post_send=True) from None
                except asyncio.IncompleteReadError:
                    raise ForwardProxyError("INCOMPLETE_PROXY_RESPONSE", http_status=502, post_send=True) from None
                if len(sz_line) > _MAX_CHUNK_LINE_BYTES:
                    raise ForwardProxyError("INVALID_PROXY_RESPONSE", http_status=502, post_send=True)
                sz_hex = sz_line.strip().split(b";", 1)[0].strip()
                if not _CHUNK_HEX_RE.fullmatch(sz_hex):
                    raise ForwardProxyError("INVALID_PROXY_RESPONSE", http_status=502, post_send=True)
                clen = int(sz_hex, 16)
                if clen == 0:
                    while True:
                        try:
                            end_line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=rem_timeout())
                        except asyncio.LimitOverrunError:
                            raise ForwardProxyError("INVALID_PROXY_RESPONSE", http_status=502, post_send=True) from None
                        except asyncio.IncompleteReadError:
                            raise ForwardProxyError(
                                "INCOMPLETE_PROXY_RESPONSE", http_status=502, post_send=True
                            ) from None
                        if len(end_line) > _MAX_CHUNK_LINE_BYTES or not end_line.endswith(b"\r\n"):
                            raise ForwardProxyError("INVALID_PROXY_RESPONSE", http_status=502, post_send=True)
                        if end_line == b"\r\n":
                            break
                    saw_eof = True
                    break
                if clen > cap - total:
                    raise ForwardProxyError("RESPONSE_TOO_LARGE", http_status=502, post_send=True)
                remaining = clen
                try:
                    while remaining > 0:
                        blk = await asyncio.wait_for(
                            reader.readexactly(min(_CHUNK_READ_BLOCK, remaining)), timeout=rem_timeout()
                        )
                        chunks.append(blk)
                        total += len(blk)
                        remaining -= len(blk)
                    crlf = await asyncio.wait_for(reader.readexactly(2), timeout=rem_timeout())
                except asyncio.IncompleteReadError:
                    raise ForwardProxyError("INCOMPLETE_PROXY_RESPONSE", http_status=502, post_send=True) from None
                if crlf != b"\r\n":
                    raise ForwardProxyError("INVALID_PROXY_RESPONSE", http_status=502, post_send=True)
            if not saw_eof:
                raise ForwardProxyError("INCOMPLETE_PROXY_RESPONSE", http_status=502, post_send=True)
        elif hmap.get("content-length", "").strip():
            cl_str = hmap["content-length"].strip()
            if not cl_str.isdigit():
                raise ForwardProxyError("INVALID_PROXY_RESPONSE", http_status=502, post_send=True)
            expected = int(cl_str)
            if expected > cap:
                raise ForwardProxyError("RESPONSE_TOO_LARGE", http_status=502, post_send=True)
            while total < expected:
                blk = await asyncio.wait_for(
                    reader.read(min(_CHUNK_READ_BLOCK, expected - total)), timeout=rem_timeout()
                )
                if not blk:
                    raise ForwardProxyError("INCOMPLETE_PROXY_RESPONSE", http_status=502, post_send=True)
                chunks.append(blk)
                total += len(blk)
        else:
            while True:
                blk = await asyncio.wait_for(reader.read(_CHUNK_READ_BLOCK), timeout=rem_timeout())
                if not blk:
                    break
                if total + len(blk) > cap:
                    raise ForwardProxyError("RESPONSE_TOO_LARGE", http_status=502, post_send=True)
                chunks.append(blk)
                total += len(blk)
        return status_code, set_cookies, b"".join(chunks).decode("utf-8", "replace")
    except ForwardProxyError:
        raise
    except TimeoutError:
        raise ForwardProxyError("PROXY_TIMEOUT", http_status=504, post_send=sent_bytes) from None
    except (OSError, asyncio.IncompleteReadError, ValueError):
        raise ForwardProxyError("INVALID_PROXY_RESPONSE", http_status=502, post_send=sent_bytes) from None
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
