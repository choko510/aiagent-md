"""SSRF-safe Web Search Router & HTTP/HTTPS Fetch executors behind ToolGateway + Sentinel.

DNS resolution happens right before TCP/TLS connection via `vet_addresses()`, and the
connection pins to the vetted IP while preserving `Host` header and TLS `sni_hostname`
to prevent DNS rebinding. Redirects are validated at every hop.
"""

from __future__ import annotations

import ipaddress
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
from aiagent_core.config import Secret
from aiagent_core.logging import redact as redact_secrets
from aiagent_sentinel.netpolicy import EgressDenied, is_public, normalize_host, vet_addresses

from .executors import Destination, ExecutorFailed, ExecutorUnknown

Resolver = Callable[[str, int], Awaitable[list[str]]]
DEFAULT_MAX_FETCH_BYTES = 131_072
HARD_MAX_FETCH_BYTES = 1_048_576
MAX_REDIRECTS = 5


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


async def safe_http_get(
    url: str,
    *,
    resolver: Resolver | None = None,
    allow_private_addrs: frozenset[str] = frozenset(),
    proxy: str | None = None,
    proxy_permit: str | None = None,
    extra_headers: dict[str, str] | None = None,
    max_bytes: int = DEFAULT_MAX_FETCH_BYTES,
    timeout_s: float = 15.0,
    verify: bool | ssl.SSLContext = True,
    max_redirects: int = MAX_REDIRECTS,
) -> FetchedHttpResponse:
    """Perform SSRF-vetted HTTP(S) GET with per-hop redirect validation and bounded streaming read."""
    bounded_max = max(256, min(max_bytes, HARD_MAX_FETCH_BYTES))
    current_url = url
    visited: set[str] = set()
    permit_header = {"Proxy-Authorization": f"Aiagent tool_gateway:{proxy_permit or ''}"} if proxy else {}

    for _ in range(max_redirects + 1):
        parsed, vetted_ip = await resolve_and_vet_url(
            current_url, resolver=resolver, allow_private_addrs=allow_private_addrs
        )
        if parsed.canonical_url in visited:
            raise ExecutorFailed("redirect_loop")
        visited.add(parsed.canonical_url)

        use_proxy = bool(proxy and parsed.scheme == "http")
        client_proxy = httpx.Proxy(proxy, headers=permit_header) if (use_proxy and proxy) else None
        req_headers = dict(extra_headers or {})
        req_extensions: dict[str, Any] = {}
        if use_proxy:
            target_url = parsed.canonical_url
        else:
            target_url, pinned_headers, req_extensions = _pinned_target_url(parsed, vetted_ip)
            req_headers.update(pinned_headers)

        try:
            async with httpx.AsyncClient(
                proxy=client_proxy,
                timeout=timeout_s,
                trust_env=False,
                verify=verify,
                follow_redirects=False,
            ) as client:
                async with client.stream("GET", target_url, headers=req_headers, extensions=req_extensions) as resp:
                    if resp.status_code == 403 and resp.headers.get("x-aiagent-egress") == "denied":
                        raise ExecutorFailed("egress_denied")
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location", "").strip()
                        if not loc:
                            raise ExecutorFailed("invalid_redirect")
                        current_url = urljoin(parsed.canonical_url, loc)
                        continue
                    if resp.status_code >= 500 or resp.status_code == 408:
                        raise ExecutorUnknown(f"http_{resp.status_code}")
                    if resp.status_code >= 400:
                        raise ExecutorFailed(f"http_{resp.status_code}")
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
                    return FetchedHttpResponse(
                        status=resp.status_code,
                        final_url=parsed.canonical_url,
                        content_type=resp.headers.get("content-type", "text/plain"),
                        body=body,
                        truncated=truncated,
                        observed_at=datetime.now(UTC).isoformat(),
                        content_sha256=sha256(body).hexdigest(),
                    )
        except (ExecutorFailed, ExecutorUnknown):
            raise
        except httpx.ConnectError as exc:
            raise ExecutorFailed("connect") from exc
        except httpx.HTTPError as exc:
            raise ExecutorUnknown(type(exc).__name__) from exc
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
    ) -> None:
        self.proxy = proxy
        self.resolver = resolver
        self.allow_private_addrs = allow_private_addrs
        self.timeout_s = timeout_s
        self.verify = verify

    def destination(self) -> Destination | None:
        return None

    def destination_for(self, args: dict[str, Any]) -> Destination:
        parsed = parse_safe_http_url(str(args.get("url", "")))
        try:
            ip = ipaddress.ip_address(parsed.host)
        except ValueError:
            ip = None
        if ip is not None and not is_public(ip) and str(ip) not in self.allow_private_addrs:
            raise ExecutorFailed("egress_denied:PRIVATE_ADDRESS")
        return Destination(parsed.scheme, parsed.host, parsed.port, "GET")

    async def execute(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        del effect_key
        max_bytes = int(args.get("maxBytes", DEFAULT_MAX_FETCH_BYTES))
        resp = await safe_http_get(
            str(args["url"]),
            resolver=self.resolver,
            allow_private_addrs=self.allow_private_addrs,
            proxy=self.proxy,
            proxy_permit=permit,
            max_bytes=max_bytes,
            timeout_s=self.timeout_s,
            verify=self.verify,
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
