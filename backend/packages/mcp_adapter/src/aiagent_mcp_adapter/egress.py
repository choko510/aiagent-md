"""Trusted egress for MCP HTTP. The adapter has no other network path.

`ForwardProxyEgress` speaks the existing Trusted Egress forward-proxy contract: one absolute-form
HTTP request per connection, a Sentinel permit in `Proxy-Authorization`, no CONNECT, no redirects,
no environment proxy, bounded response size and time. The proxy originates TLS to the approved origin;
this process never resolves or connects to the origin itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx2

from .errors import ErrorCode, McpError, classify_http_status

_ALLOWED_METHODS = frozenset({"GET", "POST", "DELETE"})
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "host",
        "accept-encoding",
    }
)
_STATUS = re.compile(r"^HTTP/1\.[01] (\d{3})(?: .*)?$")
_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


@dataclass(frozen=True)
class EgressTarget:
    scheme: str
    host: str
    port: int
    path_and_query: str

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default = 443 if self.scheme == "https" else 80
        netloc = host if self.port == default else f"{host}:{self.port}"
        return f"{self.scheme}://{netloc}{self.path_and_query}"


@dataclass(frozen=True)
class EgressRequest:
    method: str
    target: EgressTarget
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers:
            if key.lower() == lowered:
                return value
        return None


@dataclass(frozen=True)
class EgressResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    truncated: bool = False


class PermitIssuer(Protocol):
    """Admin/Sentinel authorization. Discovery, health and refresh are never an exemption from it."""

    async def issue(self, *, provider_id: str, tenant_id: str, target: EgressTarget, purpose: str) -> str: ...


class McpEgress(Protocol):
    async def send(
        self,
        request: EgressRequest,
        *,
        permit: str,
        timeout_s: float,
        max_bytes: int,
    ) -> EgressResponse: ...


def parse_target(url: str, *, allow_loopback: bool = False) -> EgressTarget:
    from .config import parse_endpoint  # local import keeps config free of egress concerns

    origin = parse_endpoint(url, allow_loopback=allow_loopback)
    parts = urlsplit(url)
    path = parts.path or "/"
    path_and_query = path + (f"?{parts.query}" if parts.query else "")
    return EgressTarget(origin.scheme, origin.host, origin.port, path_and_query)


@dataclass(frozen=True)
class _ProxyAddress:
    host: str
    port: int


class ForwardProxyEgress:
    """Production egress. Construct with the trusted proxy URL and the provider's one approved origin."""

    def __init__(
        self,
        proxy_url: str,
        *,
        permit_issuer: PermitIssuer,
        allowed_origins: frozenset[str],
        allow_loopback: bool = False,
        connect_timeout_s: float = 5.0,
    ) -> None:
        proxy = urlsplit(proxy_url)
        if proxy.scheme != "http" or not proxy.hostname or proxy.username or proxy.password:
            raise McpError(ErrorCode.CONFIG_INVALID, "proxy_must_be_http_trusted")
        if proxy.query or proxy.fragment or (proxy.path not in ("", "/")):
            raise McpError(ErrorCode.CONFIG_INVALID, "proxy_url_must_not_have_path")
        self._proxy = _ProxyAddress(proxy.hostname, proxy.port or 8080)
        self._issuer = permit_issuer
        self._allowed = frozenset(allowed_origins)
        self._allow_loopback = allow_loopback
        self._connect_timeout = connect_timeout_s

    async def send(
        self,
        request: EgressRequest,
        *,
        permit: str,
        timeout_s: float,
        max_bytes: int,
    ) -> EgressResponse:
        if not permit or permit.strip().lower() in {"none", "null", "test", "fake"}:
            raise McpError(ErrorCode.EGRESS_DENIED, "missing_permit")
        if request.method not in _ALLOWED_METHODS:
            raise McpError(ErrorCode.EGRESS_DENIED, "method_not_allowed")
        target = request.target
        if target.origin not in self._allowed:
            raise McpError(ErrorCode.EGRESS_DENIED, "origin_not_allowlisted")
        if target.scheme != "https" and not (self._allow_loopback and target.scheme == "http"):
            raise McpError(ErrorCode.EGRESS_DENIED, "https_required")
        for name, value in request.headers:
            if not _TOKEN.match(name) or "\r" in value or "\n" in value:
                raise McpError(ErrorCode.EGRESS_DENIED, "invalid_header")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._proxy.host, self._proxy.port, limit=65536),
                timeout=self._connect_timeout,
            )
        except (TimeoutError, OSError) as exc:
            raise McpError(ErrorCode.CONNECT_FAILED, "proxy_unreachable") from exc
        sent = False
        try:
            payload = self._build_payload(request, permit)
            writer.write(payload)
            await asyncio.wait_for(writer.drain(), timeout=min(timeout_s, self._connect_timeout))
            sent = True
            return await self._read_response(reader, timeout_s=timeout_s, max_bytes=max_bytes, permit=permit)
        except TimeoutError as exc:
            code = ErrorCode.TRANSPORT_TIMEOUT if sent else ErrorCode.CONNECT_TIMEOUT
            raise McpError(code, "egress_timeout") from exc
        except McpError:
            raise
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            code = ErrorCode.TRANSPORT_RESET if sent else ErrorCode.CONNECT_FAILED
            raise McpError(code, "egress_io_error") from exc
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except (TimeoutError, OSError):
                pass

    def _build_payload(self, request: EgressRequest, permit: str) -> bytes:
        target = request.target
        lines = [f"{request.method} {target.url} HTTP/1.1"]
        default = 443 if target.scheme == "https" else 80
        host = f"[{target.host}]" if ":" in target.host else target.host
        host_header = host if target.port == default else f"{host}:{target.port}"
        seen: set[str] = set()
        for name, value in request.headers:
            lowered = name.lower()
            if lowered in _HOP_BY_HOP or lowered in seen:
                continue
            seen.add(lowered)
            lines.append(f"{name}: {value}")
        if "host" not in seen:
            lines.append(f"Host: {host_header}")
        lines.append("Accept-Encoding: identity")
        lines.append(f"Content-Length: {len(request.body)}")
        lines.append(f"Proxy-Authorization: Aiagent tool_gateway:{permit}")
        lines.append("Connection: close")
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        return head + request.body

    async def _read_response(
        self,
        reader: asyncio.StreamReader,
        *,
        timeout_s: float,
        max_bytes: int,
        permit: str,
    ) -> EgressResponse:
        deadline = asyncio.get_running_loop().time() + timeout_s
        buffered = _Reader(reader, deadline)
        head = await buffered.read_until(b"\r\n\r\n", 32 * 1024)
        lines = head.decode("latin-1").split("\r\n")
        if not lines:
            raise McpError(ErrorCode.INVALID_CONTENT_TYPE, "empty_response")
        match = _STATUS.match(lines[0])
        if not match:
            raise McpError(ErrorCode.INVALID_CONTENT_TYPE, "invalid_status_line")
        status = int(match.group(1))
        headers: list[tuple[str, str]] = []
        for line in lines[1:]:
            if not line or line[0] in " \t" or ":" not in line:
                raise McpError(ErrorCode.INVALID_CONTENT_TYPE, "invalid_header")
            name, _, value = line.partition(":")
            name, value = name.strip(), value.strip()
            if not _TOKEN.match(name) or "\r" in value or "\n" in value:
                raise McpError(ErrorCode.INVALID_CONTENT_TYPE, "invalid_header")
            if name.lower() in ("set-cookie", "proxy-authenticate", "proxy-authorization"):
                continue
            headers.append((name, value))
        lowered = {k.lower(): v for k, v in headers}

        if 300 <= status <= 399:
            raise McpError(ErrorCode.EGRESS_DENIED, "redirect_forbidden")
        if status == 403 and lowered.get("x-aiagent-egress") == "denied":
            proof = lowered.get("x-aiagent-egress-proof", "")
            code = proof.partition(":")[0]
            if _proxy_denial_verified(permit, code, proof):
                raise McpError(ErrorCode.EGRESS_DENIED, f"proxy_denied:{code}")
        if status == 401:
            raise McpError(ErrorCode.HTTP_UNAUTHORIZED, "http_401")
        if status == 403:
            raise McpError(ErrorCode.HTTP_FORBIDDEN, "http_403")
        if status == 404:
            raise McpError(ErrorCode.HTTP_SESSION_LOST, "http_404")
        if status >= 400:
            raise classify_http_status(status)

        if status in (204, 304):
            return EgressResponse(status=status, headers=tuple(headers), body=b"")
        body, truncated = await _read_body(buffered, headers=lowered, max_bytes=max_bytes)
        return EgressResponse(status=status, headers=tuple(headers), body=body, truncated=truncated)


class _Reader:
    """Bounded, deadline-driven HTTP/1.1 reader with an internal buffer (no private StreamReader access)."""

    def __init__(self, reader: asyncio.StreamReader, deadline: float) -> None:
        self._reader = reader
        self._buffer = bytearray()
        self._deadline = deadline
        self._eof = False

    async def _fill(self) -> None:
        left = self._deadline - asyncio.get_running_loop().time()
        if left <= 0:
            raise McpError(ErrorCode.TRANSPORT_TIMEOUT, "egress_timeout")
        try:
            chunk = await asyncio.wait_for(self._reader.read(8192), timeout=left)
        except TimeoutError as exc:
            raise McpError(ErrorCode.TRANSPORT_TIMEOUT, "egress_timeout") from exc
        if not chunk:
            self._eof = True
        else:
            self._buffer.extend(chunk)

    async def read_until(self, token: bytes, limit: int) -> bytes:
        while True:
            idx = self._buffer.find(token)
            if idx >= 0:
                out = bytes(self._buffer[:idx])
                del self._buffer[: idx + len(token)]
                return out
            if len(self._buffer) > limit:
                raise McpError(ErrorCode.INVALID_CONTENT_TYPE, "response_line_too_long")
            if self._eof:
                raise McpError(ErrorCode.TRANSPORT_RESET, "egress_closed")
            await self._fill()

    async def read_some(self, at_most: int) -> bytes:
        if not self._buffer and not self._eof:
            await self._fill()
        if not self._buffer:
            return b""
        take = min(at_most, len(self._buffer))
        out = bytes(self._buffer[:take])
        del self._buffer[:take]
        return out

    async def read_exact(self, size: int, *, max_bytes: int) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        total = 0
        while total < size:
            if total >= max_bytes:
                return b"".join(chunks), True
            chunk = await self.read_some(min(size - total, max_bytes - total))
            if not chunk:
                raise McpError(ErrorCode.TRANSPORT_RESET, "egress_truncated")
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks), False

    async def read_to_eof(self, max_bytes: int) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        total = 0
        while True:
            if total >= max_bytes:
                return b"".join(chunks), True
            chunk = await self.read_some(min(8192, max_bytes - total))
            if not chunk:
                return b"".join(chunks), False
            chunks.append(chunk)
            total += len(chunk)


async def _read_body(
    reader: _Reader,
    *,
    headers: dict[str, str],
    max_bytes: int,
) -> tuple[bytes, bool]:
    transfer = headers.get("transfer-encoding", "").lower()
    if "chunked" in transfer:
        chunks: list[bytes] = []
        total = 0
        while True:
            line = (await reader.read_until(b"\r\n", 1024)).decode("latin-1")
            size_text = line.split(";", 1)[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError as exc:
                raise McpError(ErrorCode.INVALID_CONTENT_TYPE, "invalid_chunk_size") from exc
            if size == 0:
                while True:
                    trailer = await reader.read_until(b"\r\n", 8192)
                    if not trailer:
                        break
                break
            data, truncated = await reader.read_exact(size, max_bytes=max_bytes - total)
            chunks.append(data)
            total += len(data)
            if truncated:
                return b"".join(chunks), True
            crlf, _ = await reader.read_exact(2, max_bytes=2)
            if crlf != b"\r\n":
                raise McpError(ErrorCode.INVALID_CONTENT_TYPE, "invalid_chunk_crlf")
        return b"".join(chunks), False

    length_text = headers.get("content-length")
    if length_text is not None:
        try:
            length = int(length_text)
        except ValueError as exc:
            raise McpError(ErrorCode.INVALID_CONTENT_TYPE, "invalid_content_length") from exc
        if length < 0:
            raise McpError(ErrorCode.INVALID_CONTENT_TYPE, "invalid_content_length")
        return await reader.read_exact(length, max_bytes=max_bytes)
    return await reader.read_to_eof(max_bytes)


def _proxy_denial_verified(permit: str, code: str, proof: str) -> bool:
    if not code or ":" not in proof:
        return False
    supplied = proof.partition(":")[2]
    key = hashlib.sha256(permit.encode("utf-8")).digest()
    expected = hmac.new(key, f"DENIED:{code}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied, expected)


class EgressHttpTransport(httpx2.AsyncBaseTransport):
    """httpx2 transport handed to the MCP SDK; every request is forced through `McpEgress`.

    This is the only SDK integration seam. `follow_redirects` stays false, the environment proxy is
    ignored, and an origin not in the provider's approved set is refused here even if the egress port
    would allow it (defense in depth).
    """

    def __init__(
        self,
        egress: McpEgress,
        *,
        permit_for: Callable[[EgressRequest], Awaitable[str]],
        allowed_origins: frozenset[str],
        max_bytes: int,
        timeout_s: float,
        allow_loopback: bool = False,
        max_request_bytes: int = 1024 * 1024,
    ) -> None:
        super().__init__()
        self._egress = egress
        self._permit_for = permit_for
        self._allowed = frozenset(allowed_origins)
        self._max_bytes = max_bytes
        self._timeout_s = timeout_s
        self._allow_loopback = allow_loopback
        self._max_request_bytes = max_request_bytes

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        target = parse_target(str(request.url), allow_loopback=self._allow_loopback)
        if target.origin not in self._allowed:
            raise McpError(ErrorCode.EGRESS_DENIED, "origin_not_allowlisted")
        body = await request.aread()
        if len(body) > self._max_request_bytes:
            raise McpError(ErrorCode.BYTES_LIMIT_EXCEEDED, "request_body_too_large")
        headers = tuple((name.decode("latin-1"), value.decode("latin-1")) for name, value in request.headers.raw)
        egress_request = EgressRequest(
            method=request.method.upper(),
            target=target,
            headers=headers,
            body=body,
        )
        permit = await self._permit_for(egress_request)
        result = await self._egress.send(
            egress_request,
            permit=permit,
            timeout_s=self._timeout_s,
            max_bytes=self._max_bytes,
        )
        if result.truncated:
            raise McpError(ErrorCode.RESULT_TRUNCATED, "egress_response_truncated")
        response_headers = [
            (name, value)
            for name, value in result.headers
            if name.lower() not in ("content-length", "transfer-encoding", "connection")
        ]
        return httpx2.Response(
            status_code=result.status,
            headers=response_headers,
            content=result.body,
            request=request,
        )
