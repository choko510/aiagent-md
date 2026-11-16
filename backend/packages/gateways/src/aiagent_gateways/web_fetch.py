"""SSRF-safe Web Search Router & HTTP/HTTPS Fetch executors behind ToolGateway + Sentinel.

DNS resolution happens right before TCP/TLS connection via `vet_addresses()`, and the
connection pins to the vetted IP while preserving `Host` header and TLS `sni_hostname`
to prevent DNS rebinding. Redirects are validated at every hop.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
from aiagent_core import config
from aiagent_core.config import Secret
from aiagent_core.logging import redact as redact_secrets
from aiagent_sentinel.netpolicy import EgressDenied, is_public, normalize_host, vet_addresses

from .executors import Destination, ExecutorFailed, ExecutorUnknown, is_authenticated_proxy_denial

Resolver = Callable[[str, int], Awaitable[list[str]]]
PermitIssuer = Callable[[Destination], Awaitable[str]]
DEFAULT_MAX_FETCH_BYTES = 131_072
HARD_MAX_FETCH_BYTES = 1_048_576
MAX_REDIRECTS = 5
_CHUNK_HEX_RE = re.compile(rb"^[0-9a-fA-F]{1,16}$")
_MAX_CHUNK_LINE_BYTES = 512
_MAX_CHUNKS = 4096
_CHUNK_READ_BLOCK = 8192


async def system_resolver(host: str, port: int) -> list[str]:
    import asyncio
    import socket

    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for *_, sa in infos:
        addr = str(sa[0]).split("%", 1)[0]
        if addr not in seen:
            seen.append(addr)
    return seen


class _HtmlTextExtractor(HTMLParser):
    """Minimal dependency-free HTML to visible text extractor skipping scripts/styles."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        t = tag.lower()
        if t in ("script", "style", "noscript", "template"):
            self._skip_depth += 1
        elif t in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("script", "style", "noscript", "template") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            stripped = " ".join(data.split())
            if stripped:
                self._parts.append(stripped)

    def extracted(self) -> str:
        lines: list[str] = []
        buf: list[str] = []
        for p in self._parts:
            if p == "\n":
                if buf:
                    lines.append(" ".join(buf))
                    buf.clear()
            else:
                buf.append(p)
        if buf:
            lines.append(" ".join(buf))
        return "\n".join(lines)


def extract_readable_text(raw_bytes: bytes, content_type: str) -> str:
    decoded = raw_bytes.decode("utf-8", "replace")
    ct = content_type.split(";", 1)[0].strip().lower()
    if ct in ("text/html", "application/xhtml+xml"):
        parser = _HtmlTextExtractor()
        parser.feed(decoded)
        parser.close()
        return parser.extracted()
    return decoded


@dataclass(frozen=True)
class ParsedHttpUrl:
    scheme: str
    host: str
    port: int
    path_and_query: str
    canonical_url: str


def parse_safe_http_url(url: str) -> ParsedHttpUrl:
    if not isinstance(url, str) or not url or len(url) > 2048 or any(c in url for c in ("\\", "\r", "\n", "\0")):
        raise ExecutorFailed("invalid_url")
    try:
        u = urlsplit(url.strip())
    except ValueError as exc:
        raise ExecutorFailed("invalid_url") from exc
    scheme = u.scheme.lower()
    if scheme not in ("http", "https") or not u.hostname or u.username is not None or u.password is not None:
        raise ExecutorFailed("egress_denied")
    try:
        host = normalize_host(u.hostname)
    except EgressDenied as exc:
        raise ExecutorFailed(f"egress_denied:{exc.code}") from exc
    try:
        port = u.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise ExecutorFailed("invalid_url") from exc
    if not 1 <= port <= 65535:
        raise ExecutorFailed("invalid_url")
    path = u.path or "/"
    pq = path + (f"?{u.query}" if u.query else "")
    host_net = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    netloc = host_net if port == default_port else f"{host_net}:{port}"
    canonical = f"{scheme}://{netloc}{pq}"
    return ParsedHttpUrl(scheme, host, port, pq, canonical)


async def resolve_and_vet_url(
    url: str,
    *,
    resolver: Resolver | None = None,
    allow_private_addrs: frozenset[str] = frozenset(),
) -> tuple[ParsedHttpUrl, str]:
    parsed = parse_safe_http_url(url)
    res_fn = resolver if resolver is not None else system_resolver
    try:
        addrs = await res_fn(parsed.host, parsed.port)
        vetted = vet_addresses(addrs, allow_private=allow_private_addrs)
    except EgressDenied as exc:
        raise ExecutorFailed(f"egress_denied:{exc.code}") from exc
    except OSError as exc:
        raise ExecutorFailed("dns_failed") from exc
    return parsed, vetted[0]


@dataclass(frozen=True)
class FetchedHttpResponse:
    status: int
    final_url: str
    content_type: str
    body: bytes
    truncated: bool
    observed_at: str
    content_sha256: str
    location: str | None = None
    set_cookies: tuple[str, ...] = ()


def _pinned_target_url(parsed: ParsedHttpUrl, vetted_ip: str) -> tuple[str, dict[str, str], dict[str, Any]]:
    ip_obj = ipaddress.ip_address(vetted_ip)
    ip_host = f"[{vetted_ip}]" if ip_obj.version == 6 else vetted_ip
    default_port = 443 if parsed.scheme == "https" else 80
    host_net = f"[{parsed.host}]" if ":" in parsed.host else parsed.host
    host_header = host_net if parsed.port == default_port else f"{host_net}:{parsed.port}"
    pinned_url = f"{parsed.scheme}://{ip_host}:{parsed.port}{parsed.path_and_query}"
    headers = {"Host": host_header}
    extensions: dict[str, Any] = {"sni_hostname": parsed.host} if parsed.scheme == "https" else {}
    return pinned_url, headers, extensions


def _check_literal_ip_allowed(host: str, allow_private_addrs: frozenset[str]) -> None:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if not is_public(ip) and str(ip) not in allow_private_addrs:
        raise ExecutorFailed("egress_denied:PRIVATE_ADDRESS")


async def _send_via_forward_proxy(
    proxy_url: str,
    *,
    method: str,
    parsed: ParsedHttpUrl,
    permit: str,
    extra_headers: dict[str, str],
    request_body: bytes | None,
    max_bytes: int,
    timeout_s: float,
) -> tuple[int, list[tuple[str, str]], bytes, bool]:
    """Send ONE absolute-form HTTP/HTTPS request through Trusted Egress Proxy (never uses CONNECT tunnels)."""
    pu = urlsplit(proxy_url)
    if pu.scheme != "http" or not pu.hostname:
        raise ExecutorFailed("invalid_proxy_url")
    proxy_host = pu.hostname
    proxy_port = pu.port or 80

    default_port = 443 if parsed.scheme == "https" else 80
    host_net = f"[{parsed.host}]" if ":" in parsed.host else parsed.host
    host_header = host_net if parsed.port == default_port else f"{host_net}:{parsed.port}"

    skip = {"host", "proxy-authorization", "proxy-connection", "connection", "transfer-encoding", "content-length"}
    hdr_lines = [
        f"{method} {parsed.canonical_url} HTTP/1.1",
        f"Host: {host_header}",
        f"Proxy-Authorization: Aiagent tool_gateway:{permit}",
        "Connection: close",
    ]
    for k, v in extra_headers.items():
        if k.lower() not in skip and "\r" not in k and "\n" not in k and "\r" not in v and "\n" not in v:
            hdr_lines.append(f"{k}: {v}")
    if request_body is not None:
        hdr_lines.append(f"Content-Length: {len(request_body)}")
    raw_req = ("\r\n".join(hdr_lines) + "\r\n\r\n").encode("latin-1")
    if request_body:
        raw_req += request_body

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s

    def rem_t() -> float:
        rem = deadline - loop.time()
        if rem <= 0:
            raise TimeoutError("proxy_read_timeout")
        return rem

    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(proxy_host, proxy_port), timeout=rem_t())
    except (OSError, TimeoutError) as exc:
        raise ExecutorFailed("connect") from exc

    is_write = method.upper() not in ("GET", "HEAD")

    try:
        writer.write(raw_req)
        await asyncio.wait_for(writer.drain(), timeout=rem_t())
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=rem_t())
        lines = head[:-4].decode("latin-1").split("\r\n")
        parts = lines[0].split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise ExecutorUnknown("invalid_proxy_response")
        status_code = int(parts[1])
        resp_headers: list[tuple[str, str]] = []
        hmap: dict[str, str] = {}
        for ln in lines[1:]:
            if ":" in ln:
                hk, _, hv = ln.partition(":")
                k_clean = hk.strip()
                v_clean = hv.strip()
                resp_headers.append((k_clean, v_clean))
                hmap[k_clean.lower()] = v_clean

        if is_authenticated_proxy_denial(status_code, hmap, permit):
            raise ExecutorFailed("egress_denied")

        if method == "HEAD" or status_code in (204, 304):
            return status_code, resp_headers, b"", False

        chunks: list[bytes] = []
        total = 0
        truncated = False
        if "chunked" in hmap.get("transfer-encoding", "").lower():
            chunk_count = 0
            saw_eof = False
            max_single_chunk = max(HARD_MAX_FETCH_BYTES, max_bytes * 4)
            while True:
                chunk_count += 1
                if chunk_count > _MAX_CHUNKS:
                    raise ExecutorUnknown("chunk_count_exceeded")
                sz_line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=rem_t())
                if len(sz_line) > _MAX_CHUNK_LINE_BYTES:
                    raise ExecutorUnknown("chunk_line_too_large")
                sz_hex = sz_line.strip().split(b";", 1)[0].strip()
                if not _CHUNK_HEX_RE.fullmatch(sz_hex):
                    raise ExecutorUnknown("invalid_chunk_size")
                chunk_len = int(sz_hex, 16)
                if chunk_len == 0:
                    while True:
                        end_line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=rem_t())
                        if len(end_line) > _MAX_CHUNK_LINE_BYTES or not end_line.endswith(b"\r\n"):
                            raise ExecutorUnknown("invalid_chunk_trailer")
                        if end_line == b"\r\n":
                            break
                    saw_eof = True
                    break
                if chunk_len > max_single_chunk or (chunk_len > max(max_bytes, 65536) and chunk_len > max_bytes * 4):
                    if is_write:
                        raise ExecutorUnknown("response_too_large")
                    raise ExecutorFailed("response_too_large")
                if chunk_len > max_bytes - total:
                    to_take = max(0, max_bytes - total)
                    while to_take > 0:
                        blk = await asyncio.wait_for(
                            reader.readexactly(min(_CHUNK_READ_BLOCK, to_take)), timeout=rem_t()
                        )
                        chunks.append(blk)
                        total += len(blk)
                        to_take -= len(blk)
                    truncated = True
                    saw_eof = True
                    break
                remaining = chunk_len
                while remaining > 0:
                    blk = await asyncio.wait_for(reader.readexactly(min(_CHUNK_READ_BLOCK, remaining)), timeout=rem_t())
                    chunks.append(blk)
                    total += len(blk)
                    remaining -= len(blk)
                crlf = await asyncio.wait_for(reader.readexactly(2), timeout=rem_t())
                if crlf != b"\r\n":
                    raise ExecutorUnknown("invalid_chunk_crlf")
            if not saw_eof:
                raise ExecutorUnknown("incomplete_chunked_body")
        elif hmap.get("content-length", "").strip():
            cl_str = hmap["content-length"].strip()
            if not cl_str.isdigit():
                raise ExecutorUnknown("invalid_content_length")
            expected_len = int(cl_str)
            to_read = min(expected_len, max_bytes)
            if expected_len > max_bytes:
                truncated = True
            while total < to_read:
                blk = await asyncio.wait_for(reader.read(min(_CHUNK_READ_BLOCK, to_read - total)), timeout=rem_t())
                if not blk:
                    raise ExecutorUnknown("incomplete_body")
                chunks.append(blk)
                total += len(blk)
        else:
            while True:
                blk = await asyncio.wait_for(reader.read(_CHUNK_READ_BLOCK), timeout=rem_t())
                if not blk:
                    break
                if total + len(blk) > max_bytes:
                    keep = max_bytes - total
                    if keep > 0:
                        chunks.append(blk[:keep])
                    truncated = True
                    break
                chunks.append(blk)
                total += len(blk)
        return status_code, resp_headers, b"".join(chunks), truncated
    except (ExecutorFailed, ExecutorUnknown):
        raise
    except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError) as exc:
        raise ExecutorUnknown(type(exc).__name__) from exc
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def safe_http_get(
    url: str,
    *,
    method: str = "GET",
    request_body: bytes | None = None,
    resolver: Resolver | None = None,
    allow_private_addrs: frozenset[str] = frozenset(),
    proxy: str | None = None,
    proxy_permit: str | None = None,
    permit_issuer: PermitIssuer | None = None,
    extra_headers: dict[str, str] | None = None,
    max_bytes: int = DEFAULT_MAX_FETCH_BYTES,
    timeout_s: float = 15.0,
    verify: bool | ssl.SSLContext = True,
    max_redirects: int = MAX_REDIRECTS,
    follow_redirects: bool = True,
    raise_on_http_error: bool = True,
    require_proxy: bool = False,
) -> FetchedHttpResponse:
    """Perform SSRF-vetted HTTP(S) request strictly through Trusted Egress Proxy (when configured/production)."""
    bounded_max = max(256, min(max_bytes, HARD_MAX_FETCH_BYTES))
    req_method = method.upper()
    if req_method not in ("GET", "HEAD", "POST"):
        raise ExecutorFailed("unsupported_method")
    if (require_proxy or config.profile() == "production") and not proxy:
        raise ExecutorFailed("egress_proxy_required")

    current_url = url
    current_method = req_method
    current_body = request_body
    current_permit = proxy_permit
    visited: set[str] = set()

    for hop in range(max_redirects + 1):
        parsed = parse_safe_http_url(current_url)
        _check_literal_ip_allowed(parsed.host, allow_private_addrs)
        if parsed.canonical_url in visited:
            raise ExecutorFailed("redirect_loop")
        visited.add(parsed.canonical_url)

        use_proxy = bool(proxy)
        if hop > 0 and use_proxy:
            if permit_issuer is None:
                raise ExecutorFailed("redirect_permit_unavailable")
            try:
                current_permit = await permit_issuer(
                    Destination(parsed.scheme, parsed.host, parsed.port, current_method)
                )
            except Exception as exc:
                raise ExecutorFailed(f"egress_denied:{exc}") from exc

        vetted_ip: str | None = None
        if not use_proxy or resolver is not None:
            _, vetted_ip = await resolve_and_vet_url(
                current_url, resolver=resolver, allow_private_addrs=allow_private_addrs
            )

        req_headers = dict(extra_headers or {})
        if use_proxy and proxy:
            if not current_permit:
                raise ExecutorFailed("egress_denied:missing_permit")
            status_code, resp_hdrs_list, body, truncated = await _send_via_forward_proxy(
                proxy,
                method=current_method,
                parsed=parsed,
                permit=current_permit,
                extra_headers=req_headers,
                request_body=current_body,
                max_bytes=bounded_max,
                timeout_s=timeout_s,
            )
            hdr_map = {k.lower(): v for k, v in resp_hdrs_list}
            set_cookies = tuple(v for k, v in resp_hdrs_list if k.lower() == "set-cookie")
        else:
            assert vetted_ip is not None
            target_url, pinned_headers, req_extensions = _pinned_target_url(parsed, vetted_ip)
            req_headers.update(pinned_headers)
            try:
                async with httpx.AsyncClient(
                    proxy=None,
                    timeout=timeout_s,
                    trust_env=False,
                    verify=verify,
                    follow_redirects=False,
                ) as client:
                    req_kwargs: dict[str, Any] = {"headers": req_headers, "extensions": req_extensions}
                    if current_body is not None:
                        req_kwargs["content"] = current_body
                    async with client.stream(current_method, target_url, **req_kwargs) as resp:
                        status_code = resp.status_code
                        hdr_map = {k.lower(): v for k, v in resp.headers.items()}
                        set_cookies = tuple(resp.headers.get_list("set-cookie"))
                        chunks: list[bytes] = []
                        total = 0
                        truncated = False
                        async for chunk in resp.aiter_bytes():
                            if total + len(chunk) > bounded_max:
                                keep = bounded_max - total
                                if keep > 0:
                                    chunks.append(chunk[:keep])
                                truncated = True
                                break
                            chunks.append(chunk)
                            total += len(chunk)
                        body = b"".join(chunks)
            except (ExecutorFailed, ExecutorUnknown):
                raise
            except httpx.ConnectError as exc:
                raise ExecutorFailed("connect") from exc
            except httpx.HTTPError as exc:
                raise ExecutorUnknown(type(exc).__name__) from exc

        if status_code in (301, 302, 303, 307, 308):
            loc = hdr_map.get("location", "").strip()
            if not loc:
                raise ExecutorFailed("invalid_redirect")
            next_url = urljoin(parsed.canonical_url, loc)
            if not follow_redirects:
                return FetchedHttpResponse(
                    status=status_code,
                    final_url=parsed.canonical_url,
                    content_type=hdr_map.get("content-type", "text/plain"),
                    body=body,
                    truncated=truncated,
                    observed_at=datetime.now(UTC).isoformat(),
                    content_sha256=sha256(body).hexdigest(),
                    location=next_url,
                    set_cookies=set_cookies,
                )
            current_url = next_url
            if status_code in (301, 302, 303):
                current_method = "GET"
                current_body = None
            continue
        if status_code >= 500 or status_code == 408:
            raise ExecutorUnknown(f"http_{status_code}")
        if status_code >= 400 and current_method not in ("GET", "HEAD"):
            raise ExecutorUnknown(f"http_{status_code}")
        if status_code >= 400 and (raise_on_http_error or status_code not in (401, 403)):
            raise ExecutorFailed(f"http_{status_code}")
        return FetchedHttpResponse(
            status=status_code,
            final_url=parsed.canonical_url,
            content_type=hdr_map.get("content-type", "text/plain"),
            body=body,
            truncated=truncated,
            observed_at=datetime.now(UTC).isoformat(),
            content_sha256=sha256(body).hexdigest(),
            set_cookies=set_cookies,
        )
    raise ExecutorFailed("redirect_limit")


class HttpFetchExecutor:
    """Read-only HTTP/HTTPS fetch executor enforcing Sentinel binding + SSRF + secret redaction."""

    def __init__(
        self,
        *,
        proxy: str | None = None,
        resolver: Resolver | None = None,
        allow_private_addrs: frozenset[str] = frozenset(),
        timeout_s: float = 15.0,
        verify: bool | ssl.SSLContext = True,
        require_proxy: bool = False,
    ) -> None:
        self.proxy = proxy
        self.resolver = resolver
        self.allow_private_addrs = allow_private_addrs
        self.timeout_s = timeout_s
        self.verify = verify
        self.require_proxy = require_proxy
        self.context: dict[str, dict[str, Any]] = {}

    def destination(self) -> Destination | None:
        return None

    def destination_for(self, args: dict[str, Any]) -> Destination:
        parsed = parse_safe_http_url(str(args.get("url", "")))
        _check_literal_ip_allowed(parsed.host, self.allow_private_addrs)
        return Destination(parsed.scheme, parsed.host, parsed.port, "GET")

    async def execute(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        ctx = self.context.pop(effect_key, {})
        max_bytes = int(args.get("maxBytes", DEFAULT_MAX_FETCH_BYTES))
        resp = await safe_http_get(
            str(args["url"]),
            resolver=self.resolver,
            allow_private_addrs=self.allow_private_addrs,
            proxy=self.proxy,
            proxy_permit=permit,
            permit_issuer=ctx.get("permit_issuer"),
            max_bytes=max_bytes,
            timeout_s=self.timeout_s,
            verify=self.verify,
            require_proxy=self.require_proxy,
        )
        extracted = redact_secrets(extract_readable_text(resp.body, resp.content_type))
        return {
            "trustBoundary": "UNTRUSTED_WEB_CONTENT",
            "finalUrl": resp.final_url,
            "status": resp.status,
            "observedAt": resp.observed_at,
            "contentSha256": resp.content_sha256,
            "truncated": resp.truncated,
            "text": extracted[:32768],
        }

    async def query(self, effect_key: str, permit: str | None = None) -> dict[str, Any] | None:
        del effect_key, permit
        return None


class SearchRouterExecutor:
    """Real Search backend executor (SearXNG JSON / Brave Search API compatible) over Sentinel egress."""

    def __init__(
        self,
        url: str,
        *,
        backend_kind: str = "searxng",
        api_key: Secret | None = None,
        proxy: str | None = None,
        resolver: Resolver | None = None,
        allow_private_addrs: frozenset[str] = frozenset(),
        timeout_s: float = 15.0,
        verify: bool | ssl.SSLContext = True,
    ) -> None:
        self.url = url.rstrip("/")
        self.backend_kind = backend_kind.lower()
        self.api_key = api_key
        self.proxy = proxy
        self.resolver = resolver
        self.allow_private_addrs = allow_private_addrs
        self.timeout_s = timeout_s
        self.verify = verify
        parsed = parse_safe_http_url(self.url)
        self._dest = Destination(parsed.scheme, parsed.host, parsed.port, "GET")

    def destination(self) -> Destination | None:
        return self._dest

    async def execute(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        del effect_key
        q = quote(str(args["query"]), safe="")
        sep = "&" if "?" in self.url else "?"
        if self.backend_kind == "brave":
            query_url = f"{self.url}{sep}q={q}&count=10"
        else:
            query_url = f"{self.url}{sep}q={q}&format=json"
        headers: dict[str, str] = {"accept": "application/json"}
        if self.api_key is not None:
            if self.backend_kind == "brave":
                headers["X-Subscription-Token"] = self.api_key.reveal()
            else:
                headers["Authorization"] = "Bearer " + self.api_key.reveal()
        resp = await safe_http_get(
            query_url,
            resolver=self.resolver,
            allow_private_addrs=self.allow_private_addrs,
            proxy=self.proxy,
            proxy_permit=permit,
            extra_headers=headers,
            max_bytes=HARD_MAX_FETCH_BYTES,
            timeout_s=self.timeout_s,
            verify=self.verify,
            max_redirects=0,
        )
        try:
            import json

            data = json.loads(resp.body.decode("utf-8", "strict"))
        except ValueError as exc:
            raise ExecutorFailed("invalid_search_response") from exc
        raw_items = (
            (data.get("web") or {}).get("results", []) if self.backend_kind == "brave" else data.get("results", [])
        )
        items: list[dict[str, str]] = []
        if isinstance(raw_items, list):
            for it in raw_items[:10]:
                if not isinstance(it, dict):
                    continue
                snippet = str(it.get("description") or it.get("content") or it.get("snippet") or "")
                items.append(
                    {
                        "title": redact_secrets(str(it.get("title") or ""))[:240],
                        "url": redact_secrets(str(it.get("url") or ""))[:1024],
                        "snippet": redact_secrets(snippet)[:1024],
                    }
                )
        return {
            "trustBoundary": "UNTRUSTED_SEARCH_RESULTS",
            "backend": self.backend_kind,
            "observedAt": resp.observed_at,
            "results": items,
        }

    async def query(self, effect_key: str, permit: str | None = None) -> dict[str, Any] | None:
        del effect_key, permit
        return None
