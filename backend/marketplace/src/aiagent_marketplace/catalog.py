"""Read-only, untrusted external catalog normalization.

MKT-1 intentionally has NO install/enable/delete mutations and NO MCP execution.
For production, the caller MUST inject a Sentinel-enforced, authenticated
egress transport instead of FixedSourceHTTP. FixedSourceHTTP is for local
development only and will refuse to run without AIAGENT_MARKETPLACE_DEV_MODE=1.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx

BASE_URLS = {
    "mcp": "https://registry.modelcontextprotocol.io",
    "clawhub": "https://clawhub.ai",
}
MAX_RESPONSE_BYTES = 512 * 1024
MAX_QUERY_LENGTH = 200
MAX_CURSOR_LENGTH = 4096
MAX_ID_LENGTH = 512
CURSOR_TTL_SECONDS = 900
_SLUG = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


@dataclass
class CatalogError(Exception):
    code: str
    status: int = 503
    retry_after: int | None = None

    def __str__(self) -> str:
        return self.code


class JSONTransport(Protocol):
    async def get(self, source: str, path: str, params: Mapping[str, Any]) -> dict[str, Any]: ...


class FixedSourceHTTP:
    """Development transport: only two fixed upstream origins, no redirects.

    Production must supply a transport backed by the existing Sentinel and
    Trusted Egress; this class is deliberately disabled otherwise.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        if os.environ.get("AIAGENT_MARKETPLACE_DEV_MODE") != "1":
            raise RuntimeError("direct catalog HTTP disabled: use a Sentinel transport in production")
        self._client = client or httpx.AsyncClient(
            follow_redirects=False, timeout=httpx.Timeout(5.0), trust_env=False
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get(self, source: str, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if source not in BASE_URLS:
            raise CatalogError("INVALID_SOURCE", 400)
        if not path.startswith("/") or path.startswith("//"):
            raise CatalogError("INVALID_PATH", 400)
        if len(path) > 600 or any(c in path for c in "\r\n\\"):
            raise CatalogError("INVALID_PATH", 400)
        # Never follow an upstream redirect, including an OAuth redirect.
        try:
            async with self._client.stream(
                "GET", BASE_URLS[source] + path, params=params, headers={"Accept": "application/json"}
            ) as response:
                if response.status_code == 429:
                    value = response.headers.get("Retry-After", "")
                    retry = min(int(value), 120) if value.isdigit() else None
                    raise CatalogError("RATE_LIMITED", 503, retry)
                if response.status_code == 404:
                    raise CatalogError("ITEM_NOT_FOUND", 404)
                if response.status_code != 200:
                    raise CatalogError("UPSTREAM_UNAVAILABLE", 503)
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                    raise CatalogError("UPSTREAM_RESPONSE_TOO_LARGE", 503)
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise CatalogError("UPSTREAM_RESPONSE_TOO_LARGE", 503)
        except (httpx.RequestError, httpx.TimeoutException) as exc:
            raise CatalogError("UPSTREAM_UNAVAILABLE", 503) from exc
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeError) as exc:
            raise CatalogError("UPSTREAM_INVALID_JSON", 503) from exc
        if not isinstance(payload, dict):
            raise CatalogError("UPSTREAM_INVALID_SCHEMA", 503)
        return payload


class CachedJSONTransport:
    """Bounded, short-lived metadata cache. Cache successes only, never errors."""

    def __init__(self, inner: JSONTransport, *, ttl_seconds: float = 45.0, max_entries: int = 256) -> None:
        self.inner = inner
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}

    async def get(self, source: str, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        key = (source, path, tuple(sorted((str(k), str(v)) for k, v in params.items())))
        now = time.monotonic()
        cached = self._entries.get(key)
        if cached and cached[0] > now:
            return cached[1]
        result = await self.inner.get(source, path, params)
        if len(self._entries) >= self.max_entries:
            # Evict the oldest expiry, including already-expired entries.
            self._entries.pop(min(self._entries, key=lambda k: self._entries[k][0]), None)
        self._entries[key] = (time.monotonic() + self.ttl_seconds, result)
        return result


def _text(value: Any, max_len: int = 320) -> str:
    return value[:max_len] if isinstance(value, str) else ""


def _catalog_id(source: str, identity: str) -> str:
    raw = json.dumps([source, identity], separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _read_id(value: str) -> tuple[str, str]:
    if len(value) > MAX_ID_LENGTH or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise CatalogError("INVALID_ITEM_ID", 400)
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        source, identity = json.loads(raw)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise CatalogError("INVALID_ITEM_ID", 400) from exc
    if source not in BASE_URLS or not isinstance(identity, str) or not (1 <= len(identity) <= 200):
        raise CatalogError("INVALID_ITEM_ID", 400)
    if source == "clawhub":
        parts = identity.split("/")
        if len(parts) != 2 or any(not _SLUG.fullmatch(p) for p in parts):
            raise CatalogError("INVALID_ITEM_ID", 400)
    elif any(c in identity for c in "\r\n"):
        raise CatalogError("INVALID_ITEM_ID", 400)
    return source, identity


def _mcp_item(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("server"), dict):
        return None
    server = entry["server"]
    name = _text(server.get("name"), 200)
    if not name or "/" not in name:
        return None
    metadata = entry.get("_meta") if isinstance(entry.get("_meta"), dict) else {}
    info = metadata.get("io.modelcontextprotocol.registry/official", {})
    info = info if isinstance(info, dict) else {}
    if info.get("status") == "deleted":
        return None
    remotes = server.get("remotes") if isinstance(server.get("remotes"), list) else []
    has_remote = any(
        isinstance(r, dict)
        and r.get("type") == "streamable-http"
        and isinstance(r.get("url"), str)
        and r["url"].startswith("https://")
        for r in remotes
    )
    return {
        "id": _catalog_id("mcp", name),
        "source": "mcp",
        "kind": "MCP",
        "name": _text(server.get("title")) or name.rsplit("/", 1)[-1],
        "summary": _text(server.get("description"), 1200),
        "publisher": name.split("/", 1)[0],
        "version": _text(server.get("version"), 100) or None,
        "url": BASE_URLS["mcp"],
        "identity": name,
        "transport": "streamable-http" if has_remote else "unsupported",
        "catalogStatus": _text(info.get("status")) or "unknown",
        "installable": False,
        "notice": "MKT-1は閲覧専用です。接続・追加は後続実装です。"
        if has_remote else "MKT-1は閲覧専用です。このMCPの実行方式は未対応です。",
    }


def _clawhub_item(entry: Any, *, detail_owner: str | None = None) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    owner = entry.get("owner") if isinstance(entry.get("owner"), dict) else {}
    handle = _text(detail_owner or entry.get("ownerHandle") or owner.get("handle"), 128)
    slug = _text(entry.get("slug"), 128)
    if not _SLUG.fullmatch(handle) or not _SLUG.fullmatch(slug):
        return None
    version = entry.get("latestVersion")
    version = version.get("version") if isinstance(version, dict) else entry.get("version")
    return {
        "id": _catalog_id("clawhub", f"{handle}/{slug}"),
        "source": "clawhub",
        "kind": "SKILL",
        "name": _text(entry.get("displayName"), 160) or slug,
        "summary": _text(entry.get("summary"), 1200),
        "publisher": handle,
        "version": _text(version, 100) or None,
        "url": f"{BASE_URLS['clawhub']}/{quote(handle, safe='')}/skills/{quote(slug, safe='')}",
        "identity": f"{handle}/{slug}",
        "transport": None,
        "catalogStatus": "public-unverified",
        "installable": False,
        "notice": "MKT-1は閲覧専用です。掲載・スキャン結果は安全性の保証ではありません。",
    }


class CatalogService:
    def __init__(self, transport: JSONTransport, *, secret: bytes | None = None) -> None:
        self.transport = transport
        self._secret = secret or secrets.token_bytes(32)

    def _seal(self, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        sig = hmac.new(self._secret, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(body + sig).decode().rstrip("=")

    def _unseal(self, token: str) -> dict[str, Any]:
        if not token or len(token) > MAX_CURSOR_LENGTH:
            raise CatalogError("INVALID_CURSOR", 400)
        try:
            blob = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            body, sig = blob[:-32], blob[-32:]
            if not hmac.compare_digest(hmac.new(self._secret, body, hashlib.sha256).digest(), sig):
                raise ValueError("invalid MAC")
            value = json.loads(body)
            if not isinstance(value, dict) or value.get("exp", 0) < time.time():
                raise ValueError("expired")
            return value
        except (ValueError, TypeError, UnicodeError) as exc:
            raise CatalogError("INVALID_CURSOR", 400) from exc

    async def list_items(
        self, *, q: str = "", kind: str = "all", source: str = "all",
        limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        q = q.strip()
        if len(q) > MAX_QUERY_LENGTH or kind not in ("all", "MCP", "SKILL") or source not in ("all", "mcp", "clawhub") or not 1 <= limit <= 50:
            raise CatalogError("INVALID_FILTER", 422)
        if (kind == "SKILL" and source == "mcp") or (kind == "MCP" and source == "clawhub"):
            raise CatalogError("INVALID_FILTER", 422)
        short_mcp_query = bool(q and len(q) < 3 and kind != "SKILL")
        if short_mcp_query and (source == "mcp" or kind == "MCP"):
            raise CatalogError("MCP_QUERY_TOO_SHORT", 422)
        selected = [
            s for s in ("mcp", "clawhub")
            if (source == "all" or source == s)
            and (kind == "all" or (kind == "MCP") == (s == "mcp"))
            and not (s == "mcp" and short_mcp_query)
        ]
        fingerprint = {"q": q, "kind": kind, "source": source, "limit": limit}
        state: dict[str, Any] = {s: {"cursor": None, "done": False} for s in selected}
        page_number = 0
        if cursor:
            c = self._unseal(cursor)
            page_number = c.get("page", 0)
            if not isinstance(page_number, int) or not 0 <= page_number <= 10000:
                raise CatalogError("INVALID_CURSOR", 400)
            if c.get("filters") != fingerprint or not isinstance(c.get("state"), dict):
                raise CatalogError("CURSOR_FILTER_MISMATCH", 400)
            state = c["state"]
            if set(state) != set(selected):
                raise CatalogError("INVALID_CURSOR", 400)
            for v in state.values():
                if not isinstance(v, dict) or not isinstance(v.get("done"), bool):
                    raise CatalogError("INVALID_CURSOR", 400)
                cur = v.get("cursor")
                if cur is not None and (not isinstance(cur, str) or len(cur) > 1024):
                    raise CatalogError("INVALID_CURSOR", 400)
        entries: list[dict[str, Any]] = []
        warnings: list[dict[str, str]] = (
            [{"source": "mcp", "code": "MCP_QUERY_TOO_SHORT"}] if short_mcp_query else []
        )
        progressed = False
        base_quota, extra = divmod(limit, len(selected))
        for index, s in enumerate(selected):
            quota = base_quota + (1 if extra and index == page_number % len(selected) else 0)
            if state[s]["done"] or quota == 0:
                continue
            try:
                if s == "mcp":
                    params: dict[str, Any] = {"limit": quota, "version": "latest"}
                    if q:
                        params["search"] = q
                    if state[s]["cursor"]:
                        params["cursor"] = state[s]["cursor"]
                    result = await self.transport.get("mcp", "/v0.1/servers", params)
                    raw = result.get("servers")
                    if not isinstance(raw, list):
                        raise CatalogError("UPSTREAM_INVALID_SCHEMA")
                    entries.extend(filter(None, (_mcp_item(x) for x in raw)))
                    meta = result.get("metadata") or {}
                    next_cursor = meta.get("nextCursor") if isinstance(meta, dict) else None
                else:
                    params = {"limit": quota, "nonSuspiciousOnly": "true"}
                    if q:
                        params["q"] = q
                        result = await self.transport.get("clawhub", "/api/v1/search", params)
                        raw = result.get("results")
                    else:
                        params["sort"] = "recommended"
                        if state[s]["cursor"]:
                            params["cursor"] = state[s]["cursor"]
                        result = await self.transport.get("clawhub", "/api/v1/skills", params)
                        raw = result.get("items")
                    if not isinstance(raw, list):
                        raise CatalogError("UPSTREAM_INVALID_SCHEMA")
                    entries.extend(filter(None, (_clawhub_item(x) for x in raw)))
                    next_cursor = None if q else result.get("nextCursor")
                if next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) > 1024):
                    raise CatalogError("UPSTREAM_INVALID_SCHEMA")
                if next_cursor and next_cursor == state[s]["cursor"]:
                    raise CatalogError("UPSTREAM_CURSOR_LOOP")
                state[s] = {"cursor": next_cursor, "done": not bool(next_cursor)}
                progressed = True
            except CatalogError as exc:
                warnings.append({"source": s, "code": exc.code})
        next_token = None
        if any(not v["done"] for v in state.values()):
            # Even an empty filtered page can have a valid upstream nextCursor.
            # Do not issue an unchanged token when all sources failed.
            if progressed:
                next_token = self._seal({
                    "filters": fingerprint, "state": state,
                    "page": page_number + 1, "exp": int(time.time()) + CURSOR_TTL_SECONDS,
                })
        if warnings and not entries and not next_token:
            # Source outages must not masquerade as an empty successful catalog.
            return {"items": [], "nextCursor": None, "warnings": warnings, "partial": True}
        return {"items": entries[:limit], "nextCursor": next_token, "warnings": warnings, "partial": bool(warnings)}

    async def detail(self, item_id: str) -> dict[str, Any]:
        source, identity = _read_id(item_id)
        if source == "mcp":
            path = f"/v0.1/servers/{quote(identity, safe='')}/versions/latest"
            result = await self.transport.get("mcp", path, {})
            item = _mcp_item(result)
        else:
            handle, slug = identity.split("/", 1)
            result = await self.transport.get("clawhub", f"/api/v1/skills/{quote(slug, safe='')}", {})
            skill = result.get("skill")
            owner = result.get("owner") if isinstance(result.get("owner"), dict) else {}
            # Slugs are not unique across publishers; never return a different
            # owner's skill just because its slug matches.
            if not isinstance(skill, dict) or owner.get("handle") != handle:
                raise CatalogError("ITEM_NOT_FOUND", 404)
            item = _clawhub_item({**skill, "latestVersion": result.get("latestVersion")}, detail_owner=handle)
            moderation = result.get("moderation")
            if item and isinstance(moderation, dict) and moderation.get("isMalwareBlocked"):
                raise CatalogError("ITEM_NOT_FOUND", 404)
        if item is None or item["id"] != item_id:
            raise CatalogError("ITEM_NOT_FOUND", 404)
        return item
