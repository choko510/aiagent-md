"""Public HTTP API (CHAT_08 subset for STEP 3). Authentication: opaque bearer token -> (tenant, user, workspace) via
`agent.api_credentials` (SHA-256 only). Every request runs with RLS scoped to the authenticated tenant, and owner
predicates in SQL; another owner's resources are indistinguishable from missing ones (404). Request bodies are
validated with the generated strict contracts. The API never performs agent work inside a request.
"""

import asyncio
import hmac
import json
import os
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Literal
from urllib.parse import SplitResult, urlencode, urlsplit, urlunsplit

from aiagent_automation.campaign import (
    CampaignController,
    CampaignPlan,
    DenyUntrustedVerification,
    Rejected,
    StopPolicy,
    SystemClock,
)
from aiagent_automation.pg_store import PostgresCampaignStore, PostgresQueueDispatch, PostgresWatchStore
from aiagent_automation.watch import DailyWatch
from aiagent_core import commands, config, memory_tombstones, memory_writer, notifications
from aiagent_core.commands import CommandError, Scope
from aiagent_core.db import Database
from aiagent_core.egress_http import ForwardProxyError, send_via_forward_proxy
from aiagent_core.logging import redact as redact_secrets
from aiagent_core.migrate import check_compatible
from aiagent_core.step4_gate import capability_snapshot
from aiagent_domain import is_uuid7, uuid7
from aiagent_domain.canonical import sha256_bytes
from aiagent_domain.generated import (
    ApprovalDecisionCommandV1,
    ContextPolicyCommandV1,
    MemoryCreateCommandV1,
    MemoryUpdateCommandV1,
    TaskSteerCommandV1,
    TaskSubmitCommandV1,
)
from aiagent_domain.rpcwire import RpcError
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import ValidationError


def _parse_keyring_secret(raw_json: str) -> tuple[str, bytes]:
    try:
        doc = json.loads(raw_json)
        active = str(doc["active"])
        key_bytes = bytes.fromhex(str(doc["keys"][active]))
    except (ValueError, KeyError, TypeError) as exc:
        raise config.ConfigError("invalid browser storage key ring") from exc
    if not active or len(key_bytes) != 32:
        raise config.ConfigError("invalid browser storage key ring")
    return active, key_bytes


def _load_api_storage_keyring() -> tuple[str, bytes] | None:
    """Resolve active (key_id, AES-256 key) preferring dedicated browser_storage_keys."""
    browser_raw = config.secret("AIAGENT_BROWSER_STORAGE_KEYS", required=False)
    if browser_raw is not None:
        return _parse_keyring_secret(browser_raw.reveal())
    if config.profile() == "production":
        return None
    for env_name in ("AIAGENT_CONTEXT_KEY_RING_JSON", "AIAGENT_CONTEXT_ARTIFACT_KEYS"):
        raw = config.secret(env_name, required=False)
        if raw is not None:
            return _parse_keyring_secret(raw.reveal())
    for env_name in ("AIAGENT_CONTEXT_KEY", "AIAGENT_CONTEXT_ARTIFACT_KEY"):
        single = config.secret(env_name, required=False)
        if single is not None:
            kb = bytes.fromhex(single.reveal())
            if len(kb) != 32:
                raise config.ConfigError("invalid single browser storage key")
            return "v1", kb
    return None


def _encrypt_browser_storage_state(
    keyring: Any,
    state_dict: dict[str, Any],
    *,
    tenant_id: str,
    user_id: str,
    task_id: str,
    session_id: str,
) -> tuple[bytes, bytes, str, str]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    plaintext = json.dumps(state_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    aad = f"browser-storage-v1|{tenant_id}|{user_id}|{task_id}|{session_id}".encode()
    if keyring is not None and hasattr(keyring, "active") and hasattr(keyring, "cipher"):
        key_id = str(keyring.active)
        nonce = os.urandom(12)
        cipher_bytes = keyring.cipher(key_id).encrypt(nonce, plaintext, aad)
        return nonce, cipher_bytes, key_id, sha256(plaintext).hexdigest()
    loaded = _load_api_storage_keyring()
    if loaded is None:
        raise HTTPException(503, detail={"error": "STORAGE_KEY_UNAVAILABLE"})
    key_id, key_bytes = loaded
    nonce = os.urandom(12)
    cipher_bytes = AESGCM(key_bytes).encrypt(nonce, plaintext, aad)
    return nonce, cipher_bytes, key_id, sha256(plaintext).hexdigest()


def _parse_http_origin_url(raw_url: str) -> tuple[SplitResult, str, str, int]:
    """Parse HTTP(S) URL and validate scheme, host, absence of userinfo, and port (1..65535)."""
    u = urlsplit(raw_url)
    scheme = u.scheme.lower()
    host = (u.hostname or "").lower()
    if scheme not in ("http", "https") or not host or u.username is not None or u.password is not None:
        raise ValueError("INVALID_TARGET_URL")
    raw_port = u.port
    port = (443 if scheme == "https" else 80) if raw_port is None else raw_port
    if not (1 <= port <= 65535):
        raise ValueError("INVALID_TARGET_URL")
    return u, scheme, host, port


def _default_cookie_path(origin_url: str) -> str:
    uri_path = urlsplit(origin_url).path
    if not uri_path or not uri_path.startswith("/") or uri_path.count("/") <= 1:
        return "/"
    return uri_path.rsplit("/", 1)[0] or "/"


def _is_ip_literal(host: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


_SAME_SITE_MAP: dict[str, Literal["Strict", "Lax", "None"]] = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
}


@dataclass(frozen=True, slots=True)
class _CookieRecord:
    """RFC 6265 cookie record separating matching metadata from Playwright storageState payload."""

    name: str
    value: str
    match_domain: str
    storage_domain: str
    path: str
    secure: bool
    http_only: bool
    host_only: bool
    expires_at: float | None = None
    same_site: Literal["Strict", "Lax", "None"] | None = None

    def dedup_key(self) -> tuple[str, str, str]:
        return (self.name, self.storage_domain, self.path)

    def to_storage_cookie(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "value": self.value,
            "domain": self.storage_domain,
            "path": self.path,
            "secure": self.secure,
            "httpOnly": self.http_only,
        }
        if self.expires_at is not None:
            payload["expires"] = float(self.expires_at)
        if self.same_site is not None:
            payload["sameSite"] = self.same_site
        return payload


def _parse_set_cookie_records(origin_url: str, set_cookie_headers: list[str]) -> list[_CookieRecord]:
    import time
    from datetime import UTC
    from email.utils import parsedate_to_datetime

    u = urlsplit(origin_url)
    host = (u.hostname or "").lower()
    is_https = u.scheme.lower() == "https"
    default_path = _default_cookie_path(origin_url)
    now_ts = time.time()
    cookies: list[_CookieRecord] = []
    for raw_hdr in set_cookie_headers:
        jar = SimpleCookie()
        try:
            jar.load(raw_hdr)
        except CookieError:
            jar.clear()
        for name, morsel in jar.items():
            if not name:
                continue
            secure = bool(morsel["secure"])
            if secure and not is_https:
                continue
            same_site_raw = str(morsel["samesite"] or "").strip().lower()
            same_site = _SAME_SITE_MAP.get(same_site_raw)
            if same_site == "None" and not secure:
                continue
            domain_attr = str(morsel["domain"] or "").strip().lstrip(".").lower()
            if domain_attr:
                if _is_ip_literal(host):
                    if host != domain_attr:
                        continue
                    match_domain = host
                    host_only = True
                    storage_domain = host
                elif host == domain_attr or host.endswith("." + domain_attr):
                    match_domain = domain_attr
                    host_only = False
                    # Playwright storageState treats a leading dot as a Domain cookie (matching subdomains)
                    # and a bare domain as a host-only cookie.
                    storage_domain = f".{domain_attr}"
                else:
                    continue
            else:
                match_domain = host
                host_only = True
                storage_domain = host
            raw_path = str(morsel["path"] or "").strip()
            path_attr = raw_path if raw_path.startswith("/") else default_path
            expires_at: float | None = None
            max_age_raw = str(morsel["max-age"] or "").strip()
            if max_age_raw and (max_age_raw.isdigit() or (max_age_raw[0] == "-" and max_age_raw[1:].isdigit())):
                delta = int(max_age_raw)
                expires_at = now_ts + delta if delta > 0 else 0.0
            else:
                exp_raw = str(morsel["expires"] or "").strip()
                if exp_raw:
                    try:
                        dt = parsedate_to_datetime(exp_raw)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=UTC)
                        expires_at = dt.timestamp()
                    except (TypeError, ValueError):
                        expires_at = None
            cookies.append(
                _CookieRecord(
                    name=name,
                    value=morsel.value,
                    match_domain=match_domain,
                    storage_domain=storage_domain,
                    path=path_attr,
                    secure=secure,
                    http_only=bool(morsel["httponly"]),
                    host_only=host_only,
                    expires_at=expires_at,
                    same_site=same_site,
                )
            )
    return cookies


def _cookies_from_set_headers(origin_url: str, set_cookie_headers: list[str]) -> list[dict[str, Any]]:
    import time

    now_ts = time.time()
    return [
        rec.to_storage_cookie()
        for rec in _parse_set_cookie_records(origin_url, set_cookie_headers)
        if rec.expires_at is None or rec.expires_at > now_ts
    ]


def _cookie_matches_url(cookie: _CookieRecord, target_url: str, *, now_ts: float | None = None) -> bool:
    import time

    if cookie.expires_at is not None and cookie.expires_at <= (now_ts if now_ts is not None else time.time()):
        return False
    tu = urlsplit(target_url)
    t_scheme = tu.scheme.lower()
    t_host = (tu.hostname or "").lower()
    t_path = tu.path or "/"
    if cookie.secure and t_scheme != "https":
        return False
    c_dom = cookie.match_domain
    if not c_dom or not t_host:
        return False
    if cookie.host_only or _is_ip_literal(t_host):
        if t_host != c_dom:
            return False
    elif t_host != c_dom and not t_host.endswith("." + c_dom):
        return False
    c_path = cookie.path or "/"
    if t_path == c_path:
        return True
    if c_path.endswith("/"):
        return t_path.startswith(c_path)
    return t_path.startswith(c_path) and t_path[len(c_path) : len(c_path) + 1] == "/"


def _matching_cookie_header(cookies: list[_CookieRecord], target_url: str) -> str:
    return "; ".join(f"{c.name}={c.value}" for c in cookies if _cookie_matches_url(c, target_url))


_PASSWORD_INPUT_RE = re.compile(r"<input\b[^>]*\btype=[\"']?password\b", re.IGNORECASE)
_CHALLENGE_MARKERS = (
    "g-recaptcha",
    "h-captcha",
    "cf-turnstile",
    "verify you are human",
    "captcha-challenge",
)
_AUTH_FAILURE_MARKERS = (
    "login failed",
    "invalid password",
    "invalid credentials",
    "authentication failed",
    "sign in failed",
    "incorrect password",
)
_SESSION_COOKIE = "aiagent_session"
_SESSION_TTL_DEFAULT = 12 * 3600


def _same_origin_request(request: Request) -> bool:
    """Reject cross-origin cookie-authenticated mutations by verifying Origin or Referer against Host."""
    host_hdr = (request.headers.get("host") or "").strip().lower()
    origin_raw = (request.headers.get("origin") or "").strip()
    referer_raw = (request.headers.get("referer") or "").strip()
    candidate = origin_raw or referer_raw
    if not candidate:
        return True
    try:
        cu = urlsplit(candidate)
        c_scheme = cu.scheme.lower()
        c_host = (cu.hostname or "").lower()
        if not c_host or c_scheme not in ("http", "https"):
            return False
        c_port = cu.port or (443 if c_scheme == "https" else 80)
    except ValueError:
        return False
    if not host_hdr:
        return False
    try:
        hu = urlsplit(f"{request.url.scheme}://{host_hdr}")
        h_scheme = hu.scheme.lower()
        h_host = (hu.hostname or "").lower()
        h_port = hu.port or (443 if h_scheme == "https" else 80)
    except ValueError:
        return False
    return c_host == h_host and c_port == h_port


def _takeover_authenticated_page(status_code: int, resp_text: str) -> bool:
    """Strict authentication signal: require HTTP 2xx without challenge, password prompt, or auth-failure page."""
    if not (200 <= status_code < 300):
        return False
    low = resp_text.lower()
    if any(marker in low for marker in _CHALLENGE_MARKERS):
        return False
    if _PASSWORD_INPUT_RE.search(resp_text) is not None:
        return False
    return not any(marker in low for marker in _AUTH_FAILURE_MARKERS)


async def _takeover_via_forward_proxy(
    proxy_url: str,
    *,
    method: str,
    target_url: str,
    host_header: str,
    permit: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout_s: float = 15.0,
) -> tuple[int, list[str], str]:
    """Send ONE forward-proxy HTTP/HTTPS request for Human Takeover using Aiagent human_takeover permit."""
    try:
        return await send_via_forward_proxy(
            proxy_url,
            identity="human_takeover",
            permit=permit,
            method=method,
            target_url=target_url,
            host_header=host_header,
            headers=headers,
            body=body,
            timeout_s=timeout_s,
        )
    except ForwardProxyError as exc:
        if exc.code in ("EGRESS_PROXY_TIMEOUT", "PROXY_TIMEOUT"):
            raise TimeoutError(exc.code) from None
        err = "EGRESS_DENIED" if exc.http_status == 403 else exc.code
        raise HTTPException(exc.http_status, detail={"error": err}) from None


def _campaign_http_error(exc: Rejected) -> HTTPException:
    code = str(exc)
    if code == "CAMPAIGN_NOT_FOUND":
        return HTTPException(404, detail={"error": code})
    if code in (
        "STALE_CONTROL_VERSION",
        "CONTROL_COMMAND_CONFLICT",
        "NOT_PAUSABLE",
        "NOT_RESUMABLE",
        "BLOCKED_RESUME",
        "INVALID_APPROVAL_TRANSITION",
        "CAMPAIGN_EXISTS",
    ) or code.startswith("CAMPAIGN_"):
        return HTTPException(409, detail={"error": code})
    return HTTPException(422, detail={"error": code})


def _strict_optional_bool(body: dict[str, Any], key: str, *, default: bool = False) -> bool:
    """Strictly parse optional JSON boolean flags; reject strings ('false'), ints (0/1), null, lists, etc. as 422."""
    if key not in body:
        return default
    val = body[key]
    if type(val) is not bool:
        raise HTTPException(422, detail={"error": f"INVALID_BOOLEAN_{key.upper()}"})
    return val


def create_app(
    db: Database | None = None,
    *,
    storage_key_ring: Any = None,
    notification_key_ring: Any = None,
    sentinel: Any = None,
    egress_proxy: str | None = None,
    allowed_web_hosts: frozenset[str] | None = None,
    takeover_resolver: Callable[[str, int], Awaitable[list[str]]] | None = None,
    takeover_verify_ssl: Any = True,
    takeover_http_fn: Callable[[str, str, dict[str, str], bytes | None], Awaitable[tuple[int, list[str], str]]]
    | None = None,
    notify_resolver: Callable[[str, int], Awaitable[list[str]]] | None = None,
    notify_verify_ssl: Any = True,
    notify_allow_private: frozenset[str] = frozenset(),
    notify_direct_send_fn: notifications.DirectSendFn | None = None,
    dev_github_client: Any = None,
    dev_debug_probe_fn: Any = None,
    tool_gateway: Any = None,
    web_dist_dir: Path | None = None,
) -> FastAPI:
    state: dict[str, Database] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if storage_key_ring is None and config.profile() == "production":
            config.require_production_safety(
                "api",
                {"browser storage key ring configured": _load_api_storage_keyring() is not None},
            )
        state["db"] = db or Database(config.secret("AIAGENT_API_DSN").reveal(), max_size=16, name="api")  # type: ignore[union-attr]
        yield
        if db is None:
            state["db"].close()

    app = FastAPI(title="aiagent-md API", version="0.3.0", lifespan=lifespan)

    def get_db() -> Database:
        return state["db"]

    async def scope(request: Request, authorization: Annotated[str | None, Header()] = None) -> Scope:
        if authorization:
            if not authorization.startswith("Bearer ") or len(authorization) > 400:
                raise HTTPException(401, "unauthenticated")
            h = sha256_bytes(authorization[7:].encode())
            row = await asyncio.to_thread(get_db().one, "SELECT * FROM agent.resolve_api_token(%s)", (h,))
            if row is None:
                raise HTTPException(401, "unauthenticated")
            return Scope(row["tenant_id"], row["user_id"], row["workspace_id"])

        raw_cookie = request.cookies.get(_SESSION_COOKIE)
        if not raw_cookie or len(raw_cookie) > 256:
            raise HTTPException(401, "unauthenticated")
        s_hash = sha256_bytes(raw_cookie.encode("utf-8"))
        row = await asyncio.to_thread(get_db().one, "SELECT * FROM agent.resolve_web_session(%s)", (s_hash,))
        if row is None:
            raise HTTPException(401, "unauthenticated")
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if not _same_origin_request(request):
                raise HTTPException(403, detail={"error": "CSRF_ORIGIN_MISMATCH"})
            csrf_hdr = (request.headers.get("x-csrf-token") or "").strip()
            expected_csrf = str(row["csrf_token"])
            if not csrf_hdr or not hmac.compare_digest(csrf_hdr, expected_csrf):
                raise HTTPException(403, detail={"error": "CSRF_INVALID"})
        return Scope(row["tenant_id"], row["user_id"], row["workspace_id"])

    S = Annotated[Scope, Depends(scope)]

    @app.exception_handler(CommandError)
    async def _cmd(_: Request, e: CommandError) -> JSONResponse:
        return JSONResponse({"error": e.code}, status_code=e.http)

    @app.exception_handler(notifications.NotificationError)
    async def _notify_err(_: Request, e: notifications.NotificationError) -> JSONResponse:
        return JSONResponse({"error": e.code}, status_code=e.http_status)

    @app.exception_handler(ValidationError)
    async def _val(_: Request, e: ValidationError) -> JSONResponse:
        return JSONResponse(
            {"error": "INVALID_REQUEST", "fields": [".".join(map(str, x["loc"])) for x in e.errors()]}, status_code=422
        )

    async def body_of(request: Request, model: Any) -> Any:
        raw = await request.body()
        if len(raw) > 64 * 1024:
            raise HTTPException(413, "too large")
        return model.model_validate_json(raw)

    @app.post("/v1/auth/session", status_code=201)
    async def create_web_session(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        if not _same_origin_request(request):
            raise HTTPException(403, detail={"error": "CSRF_ORIGIN_MISMATCH"})
        raw_body = await request.body()
        if len(raw_body) > 4096:
            raise HTTPException(413, "too large")
        body_token = ""
        ttl_s = _SESSION_TTL_DEFAULT
        if raw_body:
            try:
                parsed = json.loads(raw_body)
            except ValueError:
                raise HTTPException(422, detail={"error": "INVALID_JSON"}) from None
            if not isinstance(parsed, dict):
                raise HTTPException(422, detail={"error": "INVALID_REQUEST"})
            if isinstance(parsed.get("token"), str):
                body_token = parsed["token"].strip()
            if parsed.get("ttlSeconds") is not None:
                try:
                    ttl_s = int(parsed["ttlSeconds"])
                except (TypeError, ValueError):
                    raise HTTPException(422, detail={"error": "INVALID_TTL"}) from None
                if not (60 <= ttl_s <= 604800):
                    raise HTTPException(422, detail={"error": "INVALID_TTL"})
        hdr_token = authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else ""
        token = body_token or hdr_token
        if not token or len(token) > 380:
            raise HTTPException(401, "unauthenticated")
        token_sha = sha256_bytes(token.encode("utf-8"))
        sess_id = uuid7()
        sess_secret = secrets.token_urlsafe(32)
        sess_sha = sha256_bytes(sess_secret.encode("utf-8"))
        csrf_tok = secrets.token_urlsafe(32)

        def create_row() -> dict[str, Any] | None:
            return get_db().one(
                "SELECT * FROM agent.create_web_session(%s,%s,%s,%s,%s)",
                (token_sha, sess_id, sess_sha, csrf_tok, ttl_s),
            )

        row = await asyncio.to_thread(create_row)
        if row is None:
            raise HTTPException(401, "unauthenticated")
        is_secure = request.url.scheme == "https" or config.profile() == "production"
        resp = JSONResponse(
            {
                "schemaVersion": "1.0",
                "authenticated": True,
                "authMode": "session",
                "tenantId": str(row["tenant_id"]),
                "userId": str(row["user_id"]),
                "workspaceId": str(row["workspace_id"]),
                "csrfToken": str(row["csrf_token"]),
                "expiresAt": row["expires_at"].isoformat(),
            },
            status_code=201,
        )
        resp.set_cookie(
            key=_SESSION_COOKIE,
            value=sess_secret,
            httponly=True,
            samesite="lax",
            secure=is_secure,
            path="/",
            max_age=ttl_s,
        )
        return resp

    @app.get("/v1/auth/session")
    async def get_web_session(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        if authorization and authorization.startswith("Bearer ") and len(authorization) <= 400:
            h = sha256_bytes(authorization[7:].encode())
            row = await asyncio.to_thread(get_db().one, "SELECT * FROM agent.resolve_api_token(%s)", (h,))
            if row is None:
                raise HTTPException(401, "unauthenticated")
            return {
                "schemaVersion": "1.0",
                "authenticated": True,
                "authMode": "bearer",
                "tenantId": str(row["tenant_id"]),
                "userId": str(row["user_id"]),
                "workspaceId": str(row["workspace_id"]),
                "csrfToken": None,
            }
        raw_cookie = request.cookies.get(_SESSION_COOKIE)
        if not raw_cookie or len(raw_cookie) > 256:
            raise HTTPException(401, "unauthenticated")
        s_hash = sha256_bytes(raw_cookie.encode("utf-8"))
        row = await asyncio.to_thread(get_db().one, "SELECT * FROM agent.resolve_web_session(%s)", (s_hash,))
        if row is None:
            raise HTTPException(401, "unauthenticated")
        return {
            "schemaVersion": "1.0",
            "authenticated": True,
            "authMode": "session",
            "tenantId": str(row["tenant_id"]),
            "userId": str(row["user_id"]),
            "workspaceId": str(row["workspace_id"]),
            "csrfToken": str(row["csrf_token"]),
            "expiresAt": row["expires_at"].isoformat(),
        }

    @app.delete("/v1/auth/session")
    async def delete_web_session(request: Request, s: S) -> Response:
        del s
        raw_cookie = request.cookies.get(_SESSION_COOKIE)
        if raw_cookie and len(raw_cookie) <= 256:
            s_hash = sha256_bytes(raw_cookie.encode("utf-8"))
            await asyncio.to_thread(get_db().one, "SELECT agent.revoke_web_session(%s)", (s_hash,))
        resp = JSONResponse({"schemaVersion": "1.0", "authenticated": False})
        resp.delete_cookie(key=_SESSION_COOKIE, path="/")
        return resp

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        def check() -> bool:
            with get_db().conn() as c:
                check_compatible(c, writer=True)
            return True

        try:
            await asyncio.to_thread(check)
        except Exception:
            return JSONResponse({"ready": False}, status_code=503)
        return JSONResponse({"ready": True})

    @app.get("/v1/step4/capabilities")
    async def step4_capabilities(_scope: S) -> dict[str, Any]:
        """Authenticated, read-only projection. An entry never grants a device or tool capability."""
        return {"schemaVersion": "1.0", "capabilities": capability_snapshot()}

    @app.post("/v1/tasks", status_code=201)
    async def submit(request: Request, s: S, idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, str]:
        cmd: TaskSubmitCommandV1 = await body_of(request, TaskSubmitCommandV1)
        return await asyncio.to_thread(
            commands.submit_task, get_db(), s, cmd.goal, profile=cmd.profile, idempotency_key=idempotency_key
        )

    @app.get("/v1/tasks/{task_id}")
    async def get_task(task_id: str, s: S) -> dict[str, Any]:
        def q() -> dict[str, Any] | None:
            with get_db().tx(tenant=s.tenant_id) as c:
                t = c.execute(
                    "SELECT t.id, t.status_code, t.goal_revision, t.active_run_id, t.record_version, r.status_code AS run_state,"
                    " r.result_text, r.lease_epoch, r.head_revision, r.attempt_no, r.error_code FROM agent.tasks t"
                    " LEFT JOIN agent.runs r ON r.tenant_id=t.tenant_id AND r.id=t.active_run_id"
                    " WHERE t.tenant_id=%s AND t.id=%s AND t.owner_user_id=%s",
                    (s.tenant_id, task_id, s.user_id),
                ).fetchone()
                if t is None:
                    return None
                approvals = c.execute(
                    "SELECT id, tool_key, safe_summary, request_sha256, decision_code, record_version, goal_revision"
                    " FROM agent.approval_records WHERE tenant_id=%s AND task_id=%s ORDER BY requested_at",
                    (s.tenant_id, task_id),
                ).fetchall()
                effects = c.execute(
                    "SELECT tool_key, status_code FROM agent.tool_intents WHERE tenant_id=%s AND task_id=%s"
                    " ORDER BY created_at",
                    (s.tenant_id, task_id),
                ).fetchall()
            return {
                "schemaVersion": "1.0",
                "id": t["id"],
                "state": t["status_code"],
                "goalRevision": str(t["goal_revision"]),
                "recordVersion": str(t["record_version"]),
                "run": {
                    "id": t["active_run_id"],
                    "state": t["run_state"],
                    "attemptNo": t["attempt_no"],
                    "leaseEpoch": str(t["lease_epoch"]),
                    "headRevision": str(t["head_revision"]),
                    "result": t["result_text"],
                    "errorCode": t["error_code"],
                },
                "approvals": [
                    {
                        "id": a["id"],
                        "tool": a["tool_key"],
                        "summary": a["safe_summary"],
                        "requestSha256": a["request_sha256"],
                        "decision": a["decision_code"],
                        "recordVersion": str(a["record_version"]),
                        "goalRevision": str(a["goal_revision"]),
                    }
                    for a in approvals
                ],
                "effects": [{"tool": e["tool_key"], "status": e["status_code"]} for e in effects],
            }

        out = await asyncio.to_thread(q)
        if out is None:
            raise HTTPException(404, "not found")
        return out

    @app.get("/v1/tasks/{task_id}/stream")
    async def stream_text(task_id: str, s: S) -> dict[str, Any]:
        """Provisional (not committed) public text deltas, already filtered by the Stream Privacy Gate."""

        def q() -> list[str]:
            with get_db().tx(tenant=s.tenant_id) as c:
                rows = c.execute(
                    "SELECT ch.text_delta FROM agent.run_stream_chunks ch JOIN agent.tasks t ON t.tenant_id=ch.tenant_id"
                    " AND t.active_run_id=ch.run_id WHERE t.tenant_id=%s AND t.id=%s AND t.owner_user_id=%s"
                    " ORDER BY ch.created_at, ch.seq",
                    (s.tenant_id, task_id, s.user_id),
                ).fetchall()
            return [r["text_delta"] for r in rows]

        return {"provisional": True, "deltas": await asyncio.to_thread(q)}

    @app.post("/v1/tasks/{task_id}/steer")
    async def steer(task_id: str, request: Request, s: S) -> dict[str, str]:
        cmd: TaskSteerCommandV1 = await body_of(request, TaskSteerCommandV1)
        return await asyncio.to_thread(commands.steer, get_db(), s, task_id, cmd.goal, cmd.expected_goal_revision)

    @app.post("/v1/tasks/{task_id}/cancel")
    async def cancel(task_id: str, s: S) -> dict[str, str]:
        return await asyncio.to_thread(commands.cancel, get_db(), s, task_id)

    @app.post("/v1/tasks/{task_id}/pause")
    async def pause(task_id: str, s: S) -> dict[str, str]:
        return await asyncio.to_thread(commands.pause, get_db(), s, task_id)

    @app.post("/v1/tasks/{task_id}/resume")
    async def resume(task_id: str, s: S) -> dict[str, str]:
        return await asyncio.to_thread(commands.resume, get_db(), s, task_id)

    @app.post("/v1/approvals/{approval_id}/decision")
    async def decide(approval_id: str, request: Request, s: S) -> dict[str, str]:
        cmd: ApprovalDecisionCommandV1 = await body_of(request, ApprovalDecisionCommandV1)
        return await asyncio.to_thread(
            commands.decide_approval,
            get_db(),
            s,
            approval_id,
            cmd.decision,
            cmd.request_sha256,
            cmd.expected_record_version,
        )

    # ------------------------------------------------------------------ Memory (owner-authenticated)
    def memory_ingest_client() -> Any:
        """Internal authenticated RPC to the Model Gateway (the only component holding the key)."""
        endpoint = os.environ.get("AIAGENT_MODEL_GATEWAY_ENDPOINT")
        key = config.secret("AIAGENT_API_MODEL_RPC_KEY", required=False)
        if not endpoint or key is None:
            raise HTTPException(503, "MEMORY_INGEST_UNAVAILABLE")
        from aiagent_core.rpc import RpcClient

        return RpcClient(endpoint, "api", bytes.fromhex(key.reveal()))

    @app.post("/v1/tasks/{task_id}/memory/source", status_code=201)
    async def put_memory_source(task_id: str, request: Request, s: S) -> dict[str, Any]:
        """Store the user's original text as an encrypted artifact (plaintext never persists in the API)."""
        raw = await request.body()
        if not raw or len(raw) > 2_000_000:
            raise HTTPException(413, "source too large")
        try:
            text = raw.decode("utf-8", "strict")
        except UnicodeError:
            raise HTTPException(422, "source must be UTF-8 text") from None
        client = memory_ingest_client()
        body = {
            "tenantId": s.tenant_id,
            "ownerUserId": s.user_id,
            "workspaceId": s.workspace_id,
            "taskId": task_id,
            "sourceRef": f"user:{s.user_id}",
            "locatorRef": "bytes:0",
            "observedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "content": text,
        }
        try:
            # RpcClient.call is async; run it on this loop (it is network I/O, not blocking SQL).
            result = await client.call("memory.artifact.put", body)
        except RpcError as exc:
            raise HTTPException(409 if exc.code == "MEMORY_SCOPE_DENIED" else 422, exc.code) from None
        return {"schemaVersion": "1.0", "artifactId": result["artifactId"]}

    # Memory plaintext never passes through the API: creation accepts encrypted-artifact
    # references only, and every operation is owner-scoped (another owner gets 404).
    def tombstone_log() -> Any:
        try:
            return memory_tombstones.log_from_env()
        except (memory_tombstones.TombstoneError, config.ConfigError):
            if config.profile() == "test":
                return None
            raise HTTPException(503, "MEMORY_TOMBSTONE_UNAVAILABLE") from None

    @app.post("/v1/tasks/{task_id}/memory", status_code=201)
    async def create_memory(
        task_id: str,
        request: Request,
        s: S,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        cmd: MemoryCreateCommandV1 = await body_of(request, MemoryCreateCommandV1)
        log = tombstone_log()
        memory_id, epoch, created = await asyncio.to_thread(
            memory_writer.create_memory_with_status,
            get_db(),
            s,
            task_id=task_id,
            source_ref=cmd.source_artifact_ref,
            projection_ref=cmd.projection_artifact_ref,
            recipient=cmd.recipient,
            observed_at=datetime.fromisoformat(cmd.observed_at.replace("Z", "+00:00")),
            expires_at=datetime.fromisoformat(cmd.expires_at.replace("Z", "+00:00")),
            consent_basis=cmd.consent_basis,
            idempotency_key=idempotency_key or f"memory:{task_id}:{cmd.source_artifact_ref}",
        )
        if created and log is not None:
            await asyncio.to_thread(
                log.append,
                kind="created",
                tenant_id=s.tenant_id,
                owner_id=s.user_id,
                task_id=task_id,
                memory_id=memory_id,
                revision=1,
                recorded_at=datetime.now(UTC).isoformat(),
            )
        return {
            "schemaVersion": "1.0",
            "memoryId": memory_id,
            "revision": "1",
            "sourceEpoch": str(epoch),
        }

    @app.get("/v1/tasks/{task_id}/memory")
    async def list_memory(task_id: str, s: S) -> dict[str, Any]:
        rows = await asyncio.to_thread(memory_writer.list_memories, get_db(), s, task_id=task_id)
        return {"schemaVersion": "1.0", "items": rows}

    @app.get("/v1/tasks/{task_id}/memory/{memory_id}/revisions")
    async def memory_revisions(task_id: str, memory_id: str, s: S) -> dict[str, Any]:
        rows = await asyncio.to_thread(
            memory_writer.memory_revisions, get_db(), s, task_id=task_id, memory_id=memory_id
        )
        return {"schemaVersion": "1.0", "items": rows}

    @app.patch("/v1/tasks/{task_id}/memory/{memory_id}")
    async def update_memory(task_id: str, memory_id: str, request: Request, s: S) -> dict[str, Any]:
        cmd: MemoryUpdateCommandV1 = await body_of(request, MemoryUpdateCommandV1)
        log = tombstone_log()
        revision = await asyncio.to_thread(
            memory_writer.update_memory,
            get_db(),
            s,
            task_id=task_id,
            memory_id=memory_id,
            recipient=cmd.recipient,
            expires_at=(
                datetime.fromisoformat(cmd.expires_at.replace("Z", "+00:00")) if cmd.expires_at is not None else None
            ),
            expected_revision=int(cmd.expected_revision),
            tombstone_log=log,
        )
        return {"schemaVersion": "1.0", "memoryId": memory_id, "revision": str(revision)}

    @app.delete("/v1/tasks/{task_id}/memory/{memory_id}")
    async def revoke_memory(task_id: str, memory_id: str, s: S) -> dict[str, Any]:
        log = tombstone_log()
        epoch = await asyncio.to_thread(
            memory_writer.revoke_memory, get_db(), s, task_id=task_id, memory_id=memory_id, tombstone_log=log
        )
        return {"schemaVersion": "1.0", "memoryId": memory_id, "sourceEpoch": str(epoch)}

    @app.get("/v1/memory/consents")
    async def list_consents(s: S) -> dict[str, Any]:
        rows = await asyncio.to_thread(memory_writer.memory_consents, get_db(), s)
        return {"schemaVersion": "1.0", "items": rows}

    @app.post("/v1/memory/consents/{recipient}", status_code=201)
    async def grant_consent(recipient: str, s: S) -> dict[str, Any]:
        revision = await asyncio.to_thread(memory_writer.grant_memory_consent, get_db(), s, recipient=recipient)
        return {
            "schemaVersion": "1.0",
            "recipient": recipient,
            "granted": True,
            "recordVersion": str(revision),
        }

    @app.get("/v1/memory/consents/{recipient}/revisions")
    async def consent_revisions(recipient: str, s: S) -> dict[str, Any]:
        rows = await asyncio.to_thread(memory_writer.memory_consent_revisions, get_db(), s, recipient=recipient)
        return {"schemaVersion": "1.0", "items": rows}

    @app.delete("/v1/memory/consents/{recipient}")
    async def revoke_consent(recipient: str, s: S) -> dict[str, Any]:
        log = tombstone_log()
        revision = await asyncio.to_thread(
            memory_writer.revoke_memory_consent,
            get_db(),
            s,
            recipient=recipient,
            tombstone_log=log,
        )
        return {
            "schemaVersion": "1.0",
            "recipient": recipient,
            "granted": False,
            "recordVersion": str(revision),
        }

    @app.get("/v1/tasks/{task_id}/context-policy")
    async def get_context_policy(task_id: str, s: S) -> dict[str, Any]:
        policy = await asyncio.to_thread(memory_writer.context_policy, get_db(), s, task_id=task_id)
        return {"schemaVersion": "1.0", "policy": policy}

    @app.put("/v1/tasks/{task_id}/context-policy")
    async def set_context_policy(task_id: str, request: Request, s: S) -> dict[str, Any]:
        body = await body_of(request, ContextPolicyCommandV1)
        version = await asyncio.to_thread(
            memory_writer.set_context_policy,
            get_db(),
            s,
            task_id=task_id,
            recipient=body.recipient,
            model=body.model,
            enabled=body.enabled,
            semantic_verifier_mode=body.semantic_verifier_mode or "HEURISTIC",
            expected_record_version=(
                int(body.expected_record_version) if body.expected_record_version is not None else None
            ),
        )
        return {"schemaVersion": "1.0", "taskId": task_id, "recordVersion": str(version)}

    @app.get("/v1/tasks/{task_id}/events/stream")
    async def stream_task_events(
        task_id: str,
        s: S,
        after_offset: int = 0,
        limit: int = 100,
        follow: bool = False,
        max_polls: int = 20,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        bounded = max(1, min(limit, 500))
        poll_cap = max(1, min(max_polls, 60))
        start_cursor = after_offset
        if last_event_id and last_event_id.strip().isdigit():
            start_cursor = max(start_cursor, int(last_event_id.strip()))

        def load_events(cursor: int) -> tuple[str, list[dict[str, Any]]]:
            with get_db().tx(tenant=s.tenant_id) as c:
                owned = c.execute(
                    "SELECT status_code FROM agent.tasks WHERE tenant_id=%s AND id=%s AND owner_user_id=%s",
                    (s.tenant_id, task_id, s.user_id),
                ).fetchone()
                if owned is None:
                    raise HTTPException(404, "not found")
                rows = c.execute(
                    "SELECT id, event_kind, delivery_offset, payload_json, occurred_at"
                    " FROM agent.domain_events"
                    " WHERE tenant_id=%s AND task_id=%s AND delivery_offset > %s"
                    " ORDER BY delivery_offset ASC LIMIT %s",
                    (s.tenant_id, task_id, cursor, bounded),
                ).fetchall()
                return str(owned["status_code"]), [
                    {
                        "id": str(r["id"]),
                        "kind": str(r["event_kind"]),
                        "offset": int(r["delivery_offset"]),
                        "payload": dict(r["payload_json"]),
                        "occurredAt": r["occurred_at"].isoformat(),
                    }
                    for r in rows
                ]

        initial_state, initial_events = await asyncio.to_thread(load_events, start_cursor)

        async def sse() -> AsyncIterator[str]:
            cursor = start_cursor
            state_code = initial_state
            batch = initial_events
            polls = 0
            while True:
                for ev in batch:
                    cursor = max(cursor, int(ev["offset"]))
                    yield f"id: {ev['offset']}\nevent: {ev['kind']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                polls += 1
                if not follow or polls >= poll_cap or state_code in ("SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"):
                    break
                await asyncio.sleep(0.25)
                state_code, batch = await asyncio.to_thread(load_events, cursor)

        return StreamingResponse(sse(), media_type="text/event-stream")

    @app.get("/v1/tasks")
    async def list_tasks(s: S, limit: int = 50) -> dict[str, Any]:
        bounded = max(1, min(limit, 200))

        def q() -> list[dict[str, Any]]:
            with get_db().tx(tenant=s.tenant_id) as c:
                rows = c.execute(
                    "SELECT t.id, t.status_code, t.profile_code, t.goal_revision, t.active_run_id,"
                    " t.created_at, t.updated_at, r.status_code AS run_state, gr.goal_text"
                    " FROM agent.tasks t"
                    " LEFT JOIN agent.runs r ON r.tenant_id=t.tenant_id AND r.id=t.active_run_id"
                    " LEFT JOIN agent.task_goal_revisions gr"
                    "   ON gr.tenant_id=t.tenant_id AND gr.task_id=t.id AND gr.revision=t.goal_revision"
                    " WHERE t.tenant_id=%s AND t.owner_user_id=%s ORDER BY t.created_at DESC LIMIT %s",
                    (s.tenant_id, s.user_id, bounded),
                ).fetchall()
                return [
                    {
                        "id": str(r["id"]),
                        "state": str(r["status_code"]),
                        "profile": str(r["profile_code"]),
                        "goalRevision": str(r["goal_revision"]),
                        "goalSummary": " ".join(redact_secrets(str(r["goal_text"] or "")).split())[:160],
                        "activeRunId": str(r["active_run_id"]) if r["active_run_id"] else None,
                        "runState": str(r["run_state"]) if r["run_state"] else None,
                        "createdAt": r["created_at"].isoformat(),
                        "updatedAt": r["updated_at"].isoformat(),
                    }
                    for r in rows
                ]

        return {"schemaVersion": "1.0", "items": await asyncio.to_thread(q)}

    @app.get("/v1/browser/sessions")
    async def list_browser_sessions(s: S, limit: int = 50) -> dict[str, Any]:
        bounded = max(1, min(limit, 200))

        def q() -> list[dict[str, Any]]:
            with get_db().tx(tenant=s.tenant_id) as c:
                rows = c.execute(
                    "SELECT id, task_id, run_id, state, mode, generation, goal_revision, lease_epoch,"
                    " current_url, takeover_reason, (storage_state_sha256 IS NOT NULL) AS has_storage_state,"
                    " updated_at"
                    " FROM agent.browser_sessions"
                    " WHERE tenant_id=%s AND owner_user_id=%s ORDER BY created_at DESC LIMIT %s",
                    (s.tenant_id, s.user_id, bounded),
                ).fetchall()
                return [
                    {
                        "id": str(r["id"]),
                        "taskId": str(r["task_id"]),
                        "runId": str(r["run_id"]),
                        "state": str(r["state"]),
                        "mode": str(r["mode"]),
                        "generation": int(r["generation"]),
                        "goalRevision": str(r["goal_revision"]),
                        "leaseEpoch": str(r["lease_epoch"]),
                        "currentUrl": r.get("current_url"),
                        "takeoverReason": r.get("takeover_reason"),
                        "hasStorageState": bool(r.get("has_storage_state")),
                        "updatedAt": r["updated_at"].isoformat(),
                    }
                    for r in rows
                ]

        return {"schemaVersion": "1.0", "items": await asyncio.to_thread(q)}

    @app.get("/v1/browser/sessions/{session_id}/observation")
    async def get_browser_observation(session_id: str, s: S) -> dict[str, Any]:
        if not is_uuid7(session_id):
            raise HTTPException(404)

        def q() -> dict[str, Any] | None:
            with get_db().tx(tenant=s.tenant_id) as c:
                row = c.execute(
                    "SELECT bs.id, bs.task_id, bs.run_id, bs.state, bs.mode, bs.generation,"
                    " bs.current_url, bs.takeover_reason, bs.last_observation_text,"
                    " bs.last_observation_truncated, bs.last_observation_at"
                    " FROM agent.browser_sessions bs"
                    " JOIN agent.tasks t ON t.tenant_id=bs.tenant_id AND t.id=bs.task_id"
                    " WHERE bs.tenant_id=%s AND bs.owner_user_id=%s AND bs.id=%s"
                    " AND bs.closed_at IS NULL AND agent.scope_active(%s,%s,t.workspace_id)",
                    (s.tenant_id, s.user_id, session_id, s.tenant_id, s.user_id),
                ).fetchone()
                if row is None:
                    return None
                return {
                    "schemaVersion": "1.0",
                    "sessionId": str(row["id"]),
                    "taskId": str(row["task_id"]),
                    "runId": str(row["run_id"]),
                    "state": str(row["state"]),
                    "mode": str(row["mode"]),
                    "generation": int(row["generation"]),
                    "currentUrl": row.get("current_url"),
                    "takeoverReason": row.get("takeover_reason"),
                    "observation": row.get("last_observation_text") or "",
                    "truncated": bool(row.get("last_observation_truncated")),
                    "observedAt": (row["last_observation_at"].isoformat() if row.get("last_observation_at") else None),
                    "observeOnly": True,
                }

        out = await asyncio.to_thread(q)
        if out is None:
            raise HTTPException(404)
        return out

    @app.post("/v1/browser/sessions/{session_id}/takeover/start")
    async def browser_takeover_start(session_id: str, request: Request, s: S) -> dict[str, Any]:
        if not is_uuid7(session_id):
            raise HTTPException(404)
        reason = "USER_REQUESTED"
        raw_body = await request.body()
        if raw_body:
            try:
                parsed = json.loads(raw_body)
                if isinstance(parsed, dict) and isinstance(parsed.get("reason"), str) and parsed["reason"].strip():
                    reason = parsed["reason"].strip()[:128]
            except ValueError:
                raise HTTPException(422, detail={"error": "INVALID_JSON"}) from None

        def tx() -> tuple[str, dict[str, Any] | None]:
            with get_db().tx(tenant=s.tenant_id) as c:
                exists = c.execute(
                    "SELECT state FROM agent.browser_sessions WHERE tenant_id=%s AND owner_user_id=%s AND id=%s",
                    (s.tenant_id, s.user_id, session_id),
                ).fetchone()
                if exists is None:
                    return "NOT_FOUND", None
                row = c.execute(
                    "SELECT generation, goal_revision, lease_epoch, state, mode"
                    " FROM agent.browser_session_takeover_start(%s,%s,%s,%s)",
                    (s.tenant_id, session_id, s.user_id, reason),
                ).fetchone()
                if row is None:
                    return "CONFLICT", None
                return "OK", {
                    "schemaVersion": "1.0",
                    "sessionId": session_id,
                    "generation": int(row["generation"]),
                    "goalRevision": str(row["goal_revision"]),
                    "leaseEpoch": str(row["lease_epoch"]),
                    "state": str(row["state"]),
                    "mode": str(row["mode"]),
                }

        status_flag, data = await asyncio.to_thread(tx)
        if status_flag == "NOT_FOUND":
            raise HTTPException(404)
        if status_flag == "CONFLICT" or data is None:
            raise HTTPException(409, detail={"error": "TAKEOVER_NOT_ALLOWED"})
        return data

    @app.post("/v1/browser/sessions/{session_id}/takeover/authenticate")
    async def browser_takeover_authenticate(session_id: str, request: Request, s: S) -> dict[str, Any]:
        """Interactive Human Takeover login action executed strictly in HUMAN/PAUSED mode without LLM visibility."""
        if not is_uuid7(session_id):
            raise HTTPException(404)
        raw_body = await request.body()
        if len(raw_body) > 64 * 1024:
            raise HTTPException(413, detail={"error": "BODY_TOO_LARGE"})
        try:
            parsed = json.loads(raw_body) if raw_body else {}
        except ValueError:
            raise HTTPException(422, detail={"error": "INVALID_JSON"}) from None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("url"), str):
            raise HTTPException(422, detail={"error": "INVALID_TAKEOVER_REQUEST"})
        target_url = str(parsed["url"]).strip()
        try:
            u, scheme, req_host, req_port = _parse_http_origin_url(target_url)
        except ValueError:
            raise HTTPException(422, detail={"error": "INVALID_TARGET_URL"}) from None
        if scheme == "http" and config.profile() == "production":
            raise HTTPException(422, detail={"error": "HTTPS_REQUIRED"})
        method = str(parsed.get("method") or "POST").upper()
        if method not in ("GET", "POST"):
            raise HTTPException(422, detail={"error": "INVALID_METHOD"})
        form_data = parsed.get("formData") or {}
        if not isinstance(form_data, dict):
            raise HTTPException(422, detail={"error": "INVALID_FORM_DATA"})
        encoded_pairs = [(str(k), str(v)) for k, v in form_data.items()]

        def load_session() -> tuple[str, str | None, str | None, str | None]:
            with get_db().tx(tenant=s.tenant_id) as c:
                row = c.execute(
                    "SELECT s.task_id, t.workspace_id, s.state, s.mode, s.current_url FROM agent.browser_sessions s"
                    " JOIN agent.tasks t ON t.tenant_id=s.tenant_id AND t.id=s.task_id"
                    " WHERE s.tenant_id=%s AND s.owner_user_id=%s AND s.id=%s AND s.closed_at IS NULL"
                    " AND agent.scope_active(%s,%s,t.workspace_id)",
                    (s.tenant_id, s.user_id, session_id, s.tenant_id, s.user_id),
                ).fetchone()
                if row is None:
                    return "NOT_FOUND", None, None, None
                if row["mode"] != "HUMAN" or row["state"] not in ("PAUSED", "NEEDS_LOGIN"):
                    return "CONFLICT", None, None, None
                cur = row.get("current_url")
                return "OK", str(row["task_id"]), str(row["workspace_id"]), (str(cur) if cur else None)

        flag, task_id, task_workspace_id, session_current_url = await asyncio.to_thread(load_session)
        if flag == "NOT_FOUND":
            raise HTTPException(404)
        if flag != "OK" or task_id is None or task_workspace_id is None:
            raise HTTPException(409, detail={"error": "TAKEOVER_NOT_ACTIVE"})

        req_url = target_url
        req_body: bytes | None = None
        req_headers: dict[str, str] = {}
        if method == "GET" and encoded_pairs:
            qs = urlencode(encoded_pairs)
            sep_q = f"{u.query}&{qs}" if u.query else qs
            req_url = urlunsplit((scheme, u.netloc, u.path or "/", sep_q, ""))
        elif method == "POST":
            req_body = urlencode(encoded_pairs).encode("utf-8")
            req_headers["Content-Type"] = "application/x-www-form-urlencoded"

        req_u = u
        req_scheme = scheme

        verify_url_raw = parsed.get("verifyUrl")
        if verify_url_raw is not None and not isinstance(verify_url_raw, str):
            raise HTTPException(422, detail={"error": "INVALID_TARGET_URL"})
        explicit_verify = isinstance(verify_url_raw, str) and bool(verify_url_raw.strip())
        verify_target: str | None = None
        if explicit_verify:
            candidate = str(verify_url_raw).strip()
            try:
                _, v_scheme, v_host, v_port = _parse_http_origin_url(candidate)
            except ValueError:
                raise HTTPException(422, detail={"error": "INVALID_TARGET_URL"}) from None
            if req_scheme == "https" and v_scheme != "https":
                raise HTTPException(422, detail={"error": "VERIFY_URL_DOWNGRADE"})
            if (v_scheme, v_host, v_port) != (req_scheme, req_host, req_port):
                raise HTTPException(422, detail={"error": "VERIFY_URL_ORIGIN_MISMATCH"})
            verify_target = candidate
        elif isinstance(session_current_url, str) and session_current_url.startswith(("http://", "https://")):
            try:
                vu, v_scheme, v_host, v_port = _parse_http_origin_url(session_current_url)
            except ValueError:
                vu = None
                v_scheme = v_host = ""
                v_port = 0
            if (
                vu is not None
                and (v_scheme, v_host, v_port) == (req_scheme, req_host, req_port)
                and (not (req_scheme == "https" and v_scheme != "https"))
                and (vu.path or "/") != (req_u.path or "/")
            ):
                verify_target = session_current_url

        expected_marker_raw = parsed.get("verifyContains")
        expected_marker = (
            expected_marker_raw.strip()
            if isinstance(expected_marker_raw, str) and 0 < len(expected_marker_raw.strip()) <= 256
            else None
        )

        async def _execute_takeover_http(
            url_str: str, req_meth: str, hdrs: dict[str, str], body_bytes: bytes | None
        ) -> tuple[int, list[str], str]:
            if takeover_http_fn is not None:
                return await takeover_http_fn(url_str, req_meth, hdrs, body_bytes)
            import ipaddress
            import socket

            import httpx

            try:
                pu, p_scheme, p_host, target_port = _parse_http_origin_url(url_str)
            except ValueError:
                raise HTTPException(422, detail={"error": "INVALID_TARGET_URL"}) from None
            if p_scheme == "http" and config.profile() == "production":
                raise HTTPException(422, detail={"error": "HTTPS_REQUIRED"})
            hosts_cfg = (
                allowed_web_hosts
                if allowed_web_hosts is not None
                else frozenset(
                    h.strip().lower()
                    for h in os.environ.get("AIAGENT_BROWSER_ALLOWED_WEB_HOSTS", "").split(",")
                    if h.strip()
                )
            )
            if (hosts_cfg or config.profile() == "production") and "*" not in hosts_cfg and p_host not in hosts_cfg:
                raise HTTPException(403, detail={"error": "EGRESS_DENIED"})

            allow_priv = (
                {x.strip() for x in os.environ.get("AIAGENT_WEB_FETCH_ALLOW_PRIVATE", "").split(",") if x.strip()}
                if config.profile() != "production"
                else set()
            )
            try:
                lit_ip = ipaddress.ip_address(p_host)
            except ValueError:
                lit_ip = None
            if lit_ip is not None and not lit_ip.is_global and str(lit_ip) not in allow_priv:
                raise HTTPException(422, detail={"error": "EGRESS_DENIED"})

            proxy_url = egress_proxy if egress_proxy is not None else os.environ.get("AIAGENT_EGRESS_PROXY")
            sentinel_endpoint = os.environ.get("AIAGENT_SENTINEL_ENDPOINT")
            sentinel_rpc_key = config.secret("AIAGENT_API_SENTINEL_RPC_KEY", required=False)
            has_sentinel_rpc = bool(sentinel_endpoint and sentinel_rpc_key is not None)
            if config.profile() == "production" and (not proxy_url or (sentinel is None and not has_sentinel_rpc)):
                raise HTTPException(503, detail={"error": "EGRESS_PROXY_REQUIRED"})

            default_p = 443 if p_scheme == "https" else 80
            host_net = f"[{p_host}]" if ":" in p_host else p_host
            host_hdr = host_net if target_port == default_p else f"{host_net}:{target_port}"

            if proxy_url:
                target_path = (pu.path or "/") + (f"?{pu.query}" if pu.query else "")
                body_sha = sha256(body_bytes or b"").hexdigest()
                if sentinel is not None:
                    if not hasattr(sentinel, "authorize_human_takeover"):
                        raise HTTPException(503, detail={"error": "SENTINEL_REQUIRED"})
                    try:
                        ht_permit = await asyncio.to_thread(
                            sentinel.authorize_human_takeover,
                            s.tenant_id,
                            s.user_id,
                            task_workspace_id,
                            session_id,
                            SimpleNamespace(scheme=p_scheme, host=p_host, port=target_port, method=req_meth),
                            target_path=target_path,
                            body_sha256=body_sha,
                        )
                    except RpcError as exc:
                        raise HTTPException(403, detail={"error": f"sentinel:{exc.code}"}) from None
                elif has_sentinel_rpc:
                    from aiagent_core.rpc import RpcClient

                    assert sentinel_endpoint is not None and sentinel_rpc_key is not None
                    rpc_cli = RpcClient(sentinel_endpoint, "api", bytes.fromhex(sentinel_rpc_key.reveal()))
                    try:
                        rpc_res = await rpc_cli.call(
                            "sentinel.authorize_human_takeover",
                            {
                                "tenantId": s.tenant_id,
                                "userId": s.user_id,
                                "workspaceId": task_workspace_id,
                                "sessionId": session_id,
                                "scheme": p_scheme,
                                "host": p_host,
                                "port": target_port,
                                "method": req_meth,
                                "targetPath": target_path,
                                "bodySha256": body_sha,
                            },
                        )
                    except RpcError as exc:
                        status_err = 503 if exc.code in ("SENTINEL_UNAVAILABLE", "CONNECTION_FAILED") else 403
                        raise HTTPException(status_err, detail={"error": f"sentinel:{exc.code}"}) from None
                    except Exception:
                        raise HTTPException(503, detail={"error": "SENTINEL_UNAVAILABLE"}) from None
                    ht_permit = str(rpc_res["permit"])
                else:
                    raise HTTPException(503, detail={"error": "SENTINEL_REQUIRED"})
                return await _takeover_via_forward_proxy(
                    proxy_url,
                    method=req_meth,
                    target_url=url_str,
                    host_header=host_hdr,
                    permit=ht_permit,
                    headers=hdrs,
                    body=body_bytes,
                )

            if takeover_resolver is not None:
                raw_addrs = await takeover_resolver(p_host, target_port)
            else:
                try:
                    infos = await asyncio.to_thread(socket.getaddrinfo, p_host, target_port, type=socket.SOCK_STREAM)
                    raw_addrs = [str(sa[0]).split("%", 1)[0] for *_, sa in infos]
                except OSError:
                    raise HTTPException(422, detail={"error": "EGRESS_DENIED"}) from None
            if not raw_addrs:
                raise HTTPException(422, detail={"error": "EGRESS_DENIED"})
            for addr in raw_addrs:
                ip_obj = ipaddress.ip_address(addr.split("%", 1)[0])
                if not ip_obj.is_global and str(ip_obj) not in allow_priv:
                    raise HTTPException(422, detail={"error": "EGRESS_DENIED"})
            vetted_ip = raw_addrs[0]
            ip_host = f"[{vetted_ip}]" if ":" in vetted_ip else vetted_ip
            pq = (pu.path or "/") + (f"?{pu.query}" if pu.query else "")
            pinned_url = f"{p_scheme}://{ip_host}:{target_port}{pq}"
            pinned_hdrs = {**hdrs, "Host": host_hdr}
            ext = {"sni_hostname": p_host} if p_scheme == "https" else {}
            async with httpx.AsyncClient(
                proxy=None, timeout=15.0, trust_env=False, verify=takeover_verify_ssl, follow_redirects=False
            ) as cli:
                resp = await cli.request(req_meth, pinned_url, headers=pinned_hdrs, content=body_bytes, extensions=ext)
                return resp.status_code, list(resp.headers.get_list("set-cookie")), resp.text

        status_code, set_cookies, resp_text = await _execute_takeover_http(req_url, method, req_headers, req_body)
        cookie_records = _parse_set_cookie_records(req_url, set_cookies)

        if not _takeover_authenticated_page(status_code, resp_text):
            auth_status = "FAILED"
            authenticated = False
        elif verify_target is not None:
            cookie_hdr = _matching_cookie_header(cookie_records, verify_target)
            if not cookie_hdr:
                auth_status = "UNVERIFIED"
                authenticated = False
            else:
                # Baseline unauthenticated check proves the verify URL actually restricts unauthenticated access
                # (a public HTTP 200 page must not produce a false VERIFIED signal).
                u_status, _, u_text = await _execute_takeover_http(verify_target, "GET", {}, None)
                v_status, v_cookies_raw, v_text = await _execute_takeover_http(
                    verify_target, "GET", {"Cookie": cookie_hdr}, None
                )
                if v_cookies_raw:
                    merged = {c.dedup_key(): c for c in cookie_records}
                    for nc in _parse_set_cookie_records(verify_target, v_cookies_raw):
                        merged[nc.dedup_key()] = nc
                    cookie_records = list(merged.values())
                unauth_open = _takeover_authenticated_page(u_status, u_text)
                auth_open = _takeover_authenticated_page(v_status, v_text)
                if not auth_open or (expected_marker is not None and expected_marker not in v_text):
                    auth_status = "FAILED"
                    authenticated = False
                elif not unauth_open or (expected_marker is not None and expected_marker not in u_text):
                    auth_status = "VERIFIED"
                    authenticated = True
                else:
                    auth_status = "UNVERIFIED"
                    authenticated = False
        else:
            auth_status = "UNVERIFIED"
            authenticated = False

        import time as _time

        now_ts = _time.time()
        cookies = [
            rec.to_storage_cookie() for rec in cookie_records if rec.expires_at is None or rec.expires_at > now_ts
        ]
        new_storage = {"cookies": cookies, "origins": []}
        s_nonce, s_cipher, s_key_id, s_sha = _encrypt_browser_storage_state(
            storage_key_ring,
            new_storage,
            tenant_id=s.tenant_id,
            user_id=s.user_id,
            task_id=task_id,
            session_id=session_id,
        )
        redacted_obs = redact_secrets(resp_text)[:4096]
        obs_trunc = len(resp_text) > 4096

        def save_storage() -> bool:
            with get_db().tx(tenant=s.tenant_id) as c:
                row = c.execute(
                    "SELECT agent.browser_session_takeover_set_storage(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS ok",
                    (
                        s.tenant_id,
                        session_id,
                        s.user_id,
                        s_nonce,
                        s_cipher,
                        s_key_id,
                        s_sha,
                        redacted_obs,
                        obs_trunc,
                        verify_target if authenticated else None,
                    ),
                ).fetchone()
                return bool(row and row["ok"])

        if not await asyncio.to_thread(save_storage):
            raise HTTPException(409, detail={"error": "TAKEOVER_STATE_CONFLICT"})
        return {
            "schemaVersion": "1.0",
            "sessionId": session_id,
            "authenticated": authenticated,
            "authStatus": auth_status,
            "status": status_code,
            "cookieCount": len(cookies),
            "observation": redacted_obs,
        }

    @app.post("/v1/browser/sessions/{session_id}/takeover/complete")
    async def browser_takeover_complete(session_id: str, request: Request, s: S) -> dict[str, Any]:
        if not is_uuid7(session_id):
            raise HTTPException(404)
        supplied_storage: dict[str, Any] | None = None
        raw_body = await request.body()
        if raw_body:
            if len(raw_body) > 64 * 1024:
                raise HTTPException(413, detail={"error": "BODY_TOO_LARGE"})
            try:
                parsed = json.loads(raw_body)
            except ValueError:
                raise HTTPException(422, detail={"error": "INVALID_JSON"}) from None
            if not isinstance(parsed, dict):
                raise HTTPException(422, detail={"error": "INVALID_JSON"})
            if "storageState" in parsed and parsed["storageState"] is not None:
                st = parsed["storageState"]
                if not isinstance(st, dict) or not isinstance(st.get("cookies", []), list):
                    raise HTTPException(422, detail={"error": "INVALID_STORAGE_STATE"})
                supplied_storage = {"cookies": list(st.get("cookies", [])), "origins": list(st.get("origins", []))}

        def tx() -> tuple[str, dict[str, Any] | None]:
            with get_db().tx(tenant=s.tenant_id) as c:
                exists = c.execute(
                    "SELECT state, task_id FROM agent.browser_sessions WHERE tenant_id=%s AND owner_user_id=%s AND id=%s",
                    (s.tenant_id, s.user_id, session_id),
                ).fetchone()
                if exists is None:
                    return "NOT_FOUND", None
                if supplied_storage is not None:
                    s_nonce, s_cipher, s_key_id, s_sha = _encrypt_browser_storage_state(
                        storage_key_ring,
                        supplied_storage,
                        tenant_id=s.tenant_id,
                        user_id=s.user_id,
                        task_id=str(exists["task_id"]),
                        session_id=session_id,
                    )
                    stored = c.execute(
                        "SELECT agent.browser_session_takeover_set_storage(%s,%s,%s,%s,%s,%s,%s,NULL,false,NULL) AS ok",
                        (s.tenant_id, session_id, s.user_id, s_nonce, s_cipher, s_key_id, s_sha),
                    ).fetchone()
                    if not (stored and stored["ok"]):
                        return "CONFLICT", None
                row = c.execute(
                    "SELECT generation, goal_revision, lease_epoch, state, mode"
                    " FROM agent.browser_session_takeover_complete(%s,%s,%s)",
                    (s.tenant_id, session_id, s.user_id),
                ).fetchone()
                if row is None:
                    return "CONFLICT", None
                return "OK", {
                    "schemaVersion": "1.0",
                    "sessionId": session_id,
                    "generation": int(row["generation"]),
                    "goalRevision": str(row["goal_revision"]),
                    "leaseEpoch": str(row["lease_epoch"]),
                    "state": str(row["state"]),
                    "mode": str(row["mode"]),
                }

        status_flag, data = await asyncio.to_thread(tx)
        if status_flag == "NOT_FOUND":
            raise HTTPException(404)
        if status_flag == "CONFLICT" or data is None:
            raise HTTPException(409, detail={"error": "TAKEOVER_COMPLETE_CONFLICT"})
        return data

    @app.post("/v1/browser/sessions/{session_id}/takeover/abort")
    async def browser_takeover_abort(session_id: str, s: S) -> dict[str, Any]:
        if not is_uuid7(session_id):
            raise HTTPException(404)

        def tx() -> tuple[str, bool]:
            with get_db().tx(tenant=s.tenant_id) as c:
                exists = c.execute(
                    "SELECT id FROM agent.browser_sessions WHERE tenant_id=%s AND owner_user_id=%s AND id=%s",
                    (s.tenant_id, s.user_id, session_id),
                ).fetchone()
                if exists is None:
                    return "NOT_FOUND", False
                res = c.execute(
                    "SELECT agent.browser_session_takeover_abort(%s,%s,%s) AS ok",
                    (s.tenant_id, session_id, s.user_id),
                ).fetchone()
                return ("OK", True) if (res and res["ok"]) else ("CONFLICT", False)

        status_flag, _ = await asyncio.to_thread(tx)
        if status_flag == "NOT_FOUND":
            raise HTTPException(404)
        if status_flag == "CONFLICT":
            raise HTTPException(409, detail={"error": "SESSION_ALREADY_CLOSED"})
        return {"schemaVersion": "1.0", "sessionId": session_id, "state": "CLOSED"}

    @app.get("/v1/campaigns")
    async def list_campaigns(s: S, limit: int = 50) -> dict[str, Any]:
        bounded = max(1, min(limit, 200))

        def q() -> list[dict[str, Any]]:
            with get_db().tx(tenant=s.tenant_id) as c:
                rows = c.execute(
                    "SELECT id, state, control_version, goal_revision, updated_at"
                    " FROM agent.automation_campaigns"
                    " WHERE tenant_id=%s AND owner_user_id=%s ORDER BY created_at DESC LIMIT %s",
                    (s.tenant_id, s.user_id, bounded),
                ).fetchall()
                return [
                    {
                        "id": str(r["id"]),
                        "state": str(r["state"]),
                        "controlVersion": int(r["control_version"]),
                        "goalRevision": str(r["goal_revision"]),
                        "updatedAt": r["updated_at"].isoformat(),
                    }
                    for r in rows
                ]

        return {"schemaVersion": "1.0", "items": await asyncio.to_thread(q)}

    @app.get("/v1/watches")
    async def list_watches(s: S, limit: int = 50) -> dict[str, Any]:
        bounded = max(1, min(limit, 200))

        def q() -> list[dict[str, Any]]:
            with get_db().tx(tenant=s.tenant_id) as c:
                rows = c.execute(
                    "SELECT watch_id, timezone_id, local_hour, local_minute, max_occurrences, revision, active, created_at"
                    " FROM agent.automation_watches"
                    " WHERE tenant_id=%s AND owner_user_id=%s ORDER BY created_at DESC LIMIT %s",
                    (s.tenant_id, s.user_id, bounded),
                ).fetchall()
                return [
                    {
                        "watchId": str(r["watch_id"]),
                        "timezoneId": str(r["timezone_id"]),
                        "localHour": int(r.get("local_hour", 9)),
                        "localMinute": int(r.get("local_minute", 0)),
                        "maxOccurrences": int(r["max_occurrences"]),
                        "revision": int(r["revision"]),
                        "active": bool(r["active"]),
                        "createdAt": r["created_at"].isoformat(),
                    }
                    for r in rows
                ]

        return {"schemaVersion": "1.0", "items": await asyncio.to_thread(q)}

    @app.get("/v1/campaigns/{campaign_id}")
    async def get_campaign(campaign_id: str, s: S) -> dict[str, Any]:
        def q() -> dict[str, Any] | None:
            with get_db().tx(tenant=s.tenant_id) as c:
                row = c.execute(
                    "SELECT state, control_version, goal_revision, campaign_json"
                    " FROM agent.automation_campaigns"
                    " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s",
                    (s.tenant_id, campaign_id, s.user_id),
                ).fetchone()
                if row is None:
                    return None
                return {
                    "schemaVersion": "1.0",
                    "campaignId": campaign_id,
                    "state": str(row["state"]),
                    "controlVersion": int(row["control_version"]),
                    "goalRevision": str(row["goal_revision"]),
                    "campaign": dict(row["campaign_json"]),
                }

        out = await asyncio.to_thread(q)
        if out is None:
            raise HTTPException(404, "not found")
        return out

    @app.get("/v1/campaigns/{campaign_id}/events/stream")
    async def stream_campaign_events(campaign_id: str, s: S, after_seq: int = 0) -> StreamingResponse:
        def load() -> list[dict[str, Any]]:
            with get_db().tx(tenant=s.tenant_id) as c:
                row = c.execute(
                    "SELECT campaign_json FROM agent.automation_campaigns"
                    " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s",
                    (s.tenant_id, campaign_id, s.user_id),
                ).fetchone()
                if row is None:
                    raise HTTPException(404, "not found")
                raw = dict(row["campaign_json"])
                return [ev for ev in raw.get("events", []) if int(ev.get("seq", 0)) > after_seq]

        events = await asyncio.to_thread(load)

        async def sse() -> AsyncIterator[str]:
            for ev in events:
                yield f"id: {ev['seq']}\nevent: campaign.{str(ev.get('reason', 'event')).lower()}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream")

    # ------------------------------------------------------------------ Automation Write APIs (Campaign / Watch)
    def _campaign_controller(
        s: Scope, *, profile: str = "default"
    ) -> tuple[PostgresCampaignStore, PostgresQueueDispatch, CampaignController]:
        store = PostgresCampaignStore(
            get_db(), tenant_id=s.tenant_id, owner_user_id=s.user_id, workspace_id=s.workspace_id
        )
        queue = PostgresQueueDispatch(get_db(), owner_user_id=s.user_id, workspace_id=s.workspace_id, profile=profile)
        ctrl = CampaignController(store, SystemClock(), queue, DenyUntrustedVerification())
        return store, queue, ctrl

    @app.post("/v1/campaigns", status_code=201)
    async def create_campaign(request: Request, s: S) -> dict[str, Any]:
        from datetime import timedelta

        raw = await request.body()
        if len(raw) > 32 * 1024:
            raise HTTPException(413, "too large")
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise HTTPException(422, detail={"error": "INVALID_JSON"}) from None
        if not isinstance(body, dict) or not isinstance(body.get("goalText"), str) or not body["goalText"].strip():
            raise HTTPException(422, detail={"error": "GOAL_TEXT_REQUIRED"})
        goal_text = body["goalText"].strip()[:2000]
        campaign_id = str(body.get("campaignId") or uuid7()).strip()
        if not campaign_id or len(campaign_id) > 128:
            raise HTTPException(422, detail={"error": "INVALID_CAMPAIGN_ID"})
        profile_val = str(body.get("profile") or "default").strip().lower()
        if profile_val not in ("default", "browser", "sandbox", "anthropic"):
            raise HTTPException(422, detail={"error": "INVALID_PROFILE"})
        try:
            max_cycles = int(body.get("maxCycles", 5))
            deadline_hours = int(body.get("deadlineHours", 24))
            max_cost = int(body.get("maxCostMicroUsd", 1_000_000))
            max_tokens = int(body.get("maxTokens", 50_000))
            max_tools = int(body.get("maxToolCalls", 50))
            cost_per = int(body.get("costPerCycle", 100_000))
            tokens_per = int(body.get("tokensPerCycle", 4096))
            tools_per = int(body.get("toolsPerCycle", 8))
        except (TypeError, ValueError):
            raise HTTPException(422, detail={"error": "INVALID_PLAN_NUMBERS"}) from None
        if not (1 <= max_cycles <= 100 and 1 <= deadline_hours <= 720):
            raise HTTPException(422, detail={"error": "INVALID_PLAN_BOUNDS"})
        first_item = str(body.get("firstWorkItemId") or "item-1").strip()[:120]
        if not first_item:
            raise HTTPException(422, detail={"error": "INVALID_WORK_ITEM"})
        deadline_utc = (datetime.now(UTC) + timedelta(hours=deadline_hours)).isoformat().replace("+00:00", "Z")

        def op() -> dict[str, Any]:
            store, queue, ctrl = _campaign_controller(s, profile=profile_val)
            try:
                plan = CampaignPlan(
                    campaign_id=campaign_id,
                    tenant_id=s.tenant_id,
                    goal_revision="1",
                    policy=StopPolicy(
                        max_cycles=max_cycles,
                        deadline_utc=deadline_utc,
                        max_cost_micro_usd=max_cost,
                        max_tokens=max_tokens,
                        max_tool_calls=max_tools,
                    ),
                    cost_per_cycle=cost_per,
                    tokens_per_cycle=tokens_per,
                    tools_per_cycle=tools_per,
                    goal_text=goal_text,
                )
                ctrl.create(plan, first_item)
                dispatched = ctrl.dispatch_pending(campaign_id)
                camp_obj = store.read(campaign_id)
                return {
                    "schemaVersion": "1.0",
                    "campaignId": campaign_id,
                    "state": str(camp_obj.state),
                    "controlVersion": camp_obj.control_version,
                    "goalRevision": str(camp_obj.plan.goal_revision),
                    "dispatched": dispatched,
                    "createdRuns": queue.created_runs,
                }
            except Rejected as exc:
                raise _campaign_http_error(exc) from None
            except ValueError as exc:
                raise HTTPException(422, detail={"error": str(exc)}) from None

        return await asyncio.to_thread(op)

    @app.post("/v1/campaigns/{campaign_id}/{action}")
    async def control_campaign(campaign_id: str, action: str, request: Request, s: S) -> dict[str, Any]:
        action_norm = action.strip().lower()
        if action_norm not in ("pause", "resume", "stop", "revoke", "steer"):
            raise HTTPException(404)
        raw = await request.body()
        if len(raw) > 16 * 1024:
            raise HTTPException(413, "too large")
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise HTTPException(422, detail={"error": "INVALID_JSON"}) from None
        if not isinstance(body, dict) or body.get("expectedControlVersion") is None:
            raise HTTPException(422, detail={"error": "EXPECTED_CONTROL_VERSION_REQUIRED"})
        try:
            expected_cv = int(body["expectedControlVersion"])
        except (TypeError, ValueError):
            raise HTTPException(422, detail={"error": "INVALID_CONTROL_VERSION"}) from None

        def op() -> dict[str, Any]:
            store, _, ctrl = _campaign_controller(s)
            try:
                if action_norm == "steer":
                    new_rev = str(body.get("newGoalRevision") or "").strip()
                    new_text = body.get("newGoalText")
                    if new_text is not None and not isinstance(new_text, str):
                        raise HTTPException(422, detail={"error": "INVALID_GOAL_TEXT"})
                    cv = ctrl.steer(
                        campaign_id,
                        new_goal_revision=new_rev,
                        expected_control_version=expected_cv,
                        new_goal_text=new_text.strip()[:2000] if isinstance(new_text, str) else None,
                    )
                    dispatched = ctrl.dispatch_pending(campaign_id)
                else:
                    cmd_id = str(body["commandId"]).strip() if isinstance(body.get("commandId"), str) else None
                    cv = ctrl.control(campaign_id, action_norm.upper(), expected_cv, cmd_id)
                    dispatched = ctrl.dispatch_pending(campaign_id) if action_norm == "resume" else 0
                camp_obj = store.read(campaign_id)
                return {
                    "schemaVersion": "1.0",
                    "campaignId": campaign_id,
                    "state": str(camp_obj.state),
                    "controlVersion": cv,
                    "goalRevision": str(camp_obj.plan.goal_revision),
                    "dispatched": dispatched,
                }
            except Rejected as exc:
                raise _campaign_http_error(exc) from None

        return await asyncio.to_thread(op)

    @app.post("/v1/watches", status_code=201)
    async def create_watch(request: Request, s: S) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > 16 * 1024:
            raise HTTPException(413, "too large")
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise HTTPException(422, detail={"error": "INVALID_JSON"}) from None
        if not isinstance(body, dict):
            raise HTTPException(422, detail={"error": "INVALID_REQUEST"})
        watch_id = str(body.get("watchId") or uuid7()).strip()
        tz_id = str(body.get("timezoneId") or "UTC").strip()
        start_utc = str(body.get("startUtc") or datetime.now(UTC).isoformat().replace("+00:00", "Z")).strip()
        try:
            max_occ = int(body.get("maxOccurrences", 5))
            revision = int(body.get("revision", 1))
            local_hour = int(body.get("localHour", 9))
            local_minute = int(body.get("localMinute", 0))
        except (TypeError, ValueError):
            raise HTTPException(422, detail={"error": "INVALID_WATCH_NUMBERS"}) from None

        def op() -> dict[str, Any]:
            try:
                watch = DailyWatch(
                    watch_id=watch_id,
                    tenant_id=s.tenant_id,
                    revision=revision,
                    timezone_id=tz_id,
                    local_hour=local_hour,
                    local_minute=local_minute,
                    start_utc=start_utc,
                    max_occurrences=max_occ,
                )
                PostgresWatchStore(get_db(), owner_user_id=s.user_id, workspace_id=s.workspace_id).register(watch)
                with get_db().tx(tenant=s.tenant_id) as c:
                    c.execute(
                        "UPDATE agent.automation_watches SET local_hour=%s, local_minute=%s, active=true"
                        " WHERE tenant_id=%s AND watch_id=%s AND owner_user_id=%s",
                        (watch.local_hour, watch.local_minute, s.tenant_id, watch.watch_id, s.user_id),
                    )
            except Rejected as exc:
                raise HTTPException(409, detail={"error": str(exc)}) from None
            except Exception as exc:
                raise HTTPException(422, detail={"error": f"INVALID_WATCH:{type(exc).__name__}"}) from None
            return {
                "schemaVersion": "1.0",
                "watchId": watch.watch_id,
                "timezoneId": watch.timezone_id,
                "localHour": watch.local_hour,
                "localMinute": watch.local_minute,
                "startUtc": watch.start_utc,
                "maxOccurrences": watch.max_occurrences,
                "revision": watch.revision,
                "active": True,
            }

        return await asyncio.to_thread(op)

    @app.delete("/v1/watches/{watch_id}")
    async def deactivate_watch(watch_id: str, s: S) -> dict[str, Any]:
        def op() -> dict[str, Any] | None:
            with get_db().tx(tenant=s.tenant_id) as c:
                row = c.execute(
                    "UPDATE agent.automation_watches SET active=false"
                    " WHERE tenant_id=%s AND watch_id=%s AND owner_user_id=%s RETURNING watch_id",
                    (s.tenant_id, watch_id, s.user_id),
                ).fetchone()
                return {"schemaVersion": "1.0", "watchId": str(row["watch_id"]), "active": False} if row else None

        out = await asyncio.to_thread(op)
        if out is None:
            raise HTTPException(404)
        return out

    @app.post("/v1/watches/{watch_id}/trigger")
    async def trigger_watch(watch_id: str, request: Request, s: S) -> dict[str, Any]:
        raw = await request.body()
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise HTTPException(422, detail={"error": "INVALID_JSON"}) from None
        if not isinstance(body, dict):
            raise HTTPException(422, detail={"error": "INVALID_REQUEST"})
        slot_iso = body.get("slotUtc")
        eval_now = notifications.utcnow()
        if "slotUtc" in body:
            if not isinstance(slot_iso, str) or not slot_iso.strip():
                raise HTTPException(422, detail={"error": "INVALID_SLOT_UTC"})
            try:
                slot_dt = datetime.fromisoformat(slot_iso.strip().replace("Z", "+00:00"))
                if slot_dt.tzinfo is None:
                    slot_dt = slot_dt.replace(tzinfo=UTC)
            except ValueError:
                raise HTTPException(422, detail={"error": "INVALID_SLOT_UTC"}) from None
        else:
            slot_dt = eval_now

        raw_ev = body.get("evidenceUrls")
        if raw_ev is not None and not isinstance(raw_ev, list):
            raise HTTPException(422, detail={"error": "INVALID_EVIDENCE_URLS"})
        evidence_urls = [str(u) for u in (raw_ev or []) if isinstance(u, str)]
        title = str(body.get("title") or f"Watch {watch_id} triggered").strip()
        summary = str(body.get("summaryText") or f"Watch observation slot {slot_dt.isoformat()}").strip()
        priority = str(body.get("priority") or "standard")
        dry_run = _strict_optional_bool(body, "dryRun", default=False)
        dispatch_now = _strict_optional_bool(body, "dispatch", default=False)
        all_consented = _strict_optional_bool(body, "allConsentedBindings", default=False)

        explicit_bindings: list[str] = []
        if "bindingId" in body:
            if not isinstance(body.get("bindingId"), str) or not is_uuid7(str(body["bindingId"]).strip()):
                raise HTTPException(422, detail={"error": "INVALID_BINDING_ID"})
            explicit_bindings.append(str(body["bindingId"]).strip())
        if "bindingIds" in body:
            raw_bids = body["bindingIds"]
            if not isinstance(raw_bids, list):
                raise HTTPException(422, detail={"error": "INVALID_BINDING_IDS"})
            for item in raw_bids:
                if not isinstance(item, str) or not is_uuid7(item.strip()):
                    raise HTTPException(422, detail={"error": "INVALID_BINDING_ID"})
                bid_s = item.strip()
                if bid_s not in explicit_bindings:
                    explicit_bindings.append(bid_s)
        if not explicit_bindings and not all_consented:
            raise HTTPException(422, detail={"error": "BINDING_SELECTION_REQUIRED"})

        if dry_run:

            def dry_op() -> dict[str, Any]:
                with get_db().tx(tenant=s.tenant_id) as c:
                    wrow = c.execute(
                        "SELECT active FROM agent.automation_watches"
                        " WHERE tenant_id=%s AND watch_id=%s AND owner_user_id=%s",
                        (s.tenant_id, watch_id, s.user_id),
                    ).fetchone()
                if wrow is None:
                    raise HTTPException(404)
                if not wrow["active"]:
                    raise HTTPException(409, detail={"error": "WATCH_INACTIVE"})
                preview = notifications.preview_notification_dry_run(
                    get_db(),
                    tenant_id=s.tenant_id,
                    user_id=s.user_id,
                    workspace_id=s.workspace_id,
                    binding_ids=explicit_bindings or None,
                    all_consented_bindings=all_consented,
                    priority=priority,
                    now_utc=eval_now,
                )
                return {
                    "schemaVersion": "1.0",
                    "watchId": watch_id,
                    "dryRun": True,
                    "claimed": False,
                    "preview": preview,
                    "queuedNotifications": [],
                }

            return await asyncio.to_thread(dry_op)

        def op() -> dict[str, Any]:
            wstore = PostgresWatchStore(get_db(), owner_user_id=s.user_id, workspace_id=s.workspace_id)
            with get_db().tx(tenant=s.tenant_id) as c:
                wrow = c.execute(
                    "SELECT watch_id, timezone_id, local_hour, local_minute, start_utc, max_occurrences, revision, active"
                    " FROM agent.automation_watches"
                    " WHERE tenant_id=%s AND watch_id=%s AND owner_user_id=%s FOR UPDATE",
                    (s.tenant_id, watch_id, s.user_id),
                ).fetchone()
                if wrow is None:
                    raise HTTPException(404)
                if not wrow["active"]:
                    raise HTTPException(409, detail={"error": "WATCH_INACTIVE"})
                watch = DailyWatch(
                    watch_id=str(wrow["watch_id"]),
                    tenant_id=s.tenant_id,
                    revision=int(wrow["revision"]),
                    timezone_id=str(wrow.get("timezone_id") or "UTC"),
                    local_hour=int(wrow.get("local_hour", 9)),
                    local_minute=int(wrow.get("local_minute", 0)),
                    start_utc=wrow["start_utc"].isoformat().replace("+00:00", "Z"),
                    max_occurrences=int(wrow["max_occurrences"]),
                )
                claimed = wstore.claim_bounded_tx(c, watch, slot_dt)
                if not claimed:
                    return {
                        "schemaVersion": "1.0",
                        "watchId": watch_id,
                        "dryRun": False,
                        "claimed": False,
                        "queuedNotifications": [],
                    }
                if os.environ.get("AIAGENT_WATCH_FAULT") == "fail_after_claim":
                    raise RuntimeError("simulated crash after watch claim inside tx")
                target_bindings = list(explicit_bindings)
                if not target_bindings and all_consented:
                    brows = c.execute(
                        "SELECT id FROM agent.notification_bindings"
                        " WHERE tenant_id=%s AND owner_user_id=%s AND workspace_id=%s"
                        " AND consent_granted=true AND opted_out_at IS NULL AND revoked_at IS NULL",
                        (s.tenant_id, s.user_id, s.workspace_id),
                    ).fetchall()
                    target_bindings = [str(b["id"]) for b in brows]
                if not target_bindings:
                    raise notifications.NotificationError("NO_ACTIVE_BINDINGS", http_status=422)
                queued: list[dict[str, Any]] = []
                dedupe = f"watch:{s.tenant_id}:{watch_id}:r{watch.revision}:{slot_dt.isoformat()}"
                for bid in target_bindings:
                    intent = notifications.enqueue_notification_tx(
                        c,
                        tenant_id=s.tenant_id,
                        user_id=s.user_id,
                        workspace_id=s.workspace_id,
                        binding_id=bid,
                        semantic_event_key=f"watch:{watch_id}:r{watch.revision}:{slot_dt.isoformat()}",
                        source_kind="watch",
                        source_ref=watch_id,
                        title=title,
                        summary_text=summary,
                        evidence_urls=evidence_urls,
                        observed_at=slot_dt,
                        now_utc=eval_now,
                        priority=priority,
                    )
                    queued.append(intent)
                c.execute(
                    "UPDATE agent.automation_watch_slots SET status='SENT'"
                    " WHERE tenant_id=%s AND dedupe_key=%s AND status='PENDING'",
                    (s.tenant_id, dedupe),
                )
                return {
                    "schemaVersion": "1.0",
                    "watchId": watch_id,
                    "dryRun": False,
                    "claimed": True,
                    "queuedNotifications": queued,
                }

        res = await asyncio.to_thread(op)
        if res["claimed"] and dispatch_now:
            intent_ids = [str(q["id"]) for q in res["queuedNotifications"]]
            res["dispatch"] = await _notify_worker().run_once(
                tenant_id=s.tenant_id,
                owner_user_id=s.user_id,
                workspace_id=s.workspace_id,
                intent_ids=intent_ids,
                batch=max(1, len(intent_ids)),
            )
        return res

    # ------------------------------------------------------------------ Notification Bindings & Outbox APIs
    def _notify_worker() -> notifications.NotificationDeliveryWorker:
        return notifications.NotificationDeliveryWorker(
            get_db(),
            notification_key_ring,
            sentinel=sentinel,
            egress_proxy=egress_proxy,
            direct_send_fn=notify_direct_send_fn,
            allow_private_addrs=notify_allow_private,
            resolver=notify_resolver,
            verify_ssl=notify_verify_ssl,
        )

    @app.get("/v1/notifications/bindings")
    async def list_notification_bindings(s: S, limit: int = 50) -> dict[str, Any]:
        items = await asyncio.to_thread(
            notifications.list_bindings, get_db(), tenant_id=s.tenant_id, user_id=s.user_id, limit=limit
        )
        return {"schemaVersion": "1.0", "items": items}

    @app.post("/v1/notifications/bindings", status_code=201)
    async def create_notification_binding(request: Request, s: S) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > 16 * 1024:
            raise HTTPException(413, "too large")
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise HTTPException(422, detail={"error": "INVALID_JSON"}) from None
        if not isinstance(body, dict) or not isinstance(body.get("destination"), dict):
            raise HTTPException(422, detail={"error": "INVALID_BINDING_REQUEST"})
        dest_raw = {str(k): str(v) for k, v in body["destination"].items()}
        consent_granted = _strict_optional_bool(body, "consentGranted", default=False)
        try:
            raw_qs = body.get("quietStartHour")
            raw_qe = body.get("quietEndHour")
            if isinstance(raw_qs, bool) or isinstance(raw_qe, bool):
                raise ValueError("bool is not valid hour")
            quiet_start = int(raw_qs) if raw_qs is not None else None
            quiet_end = int(raw_qe) if raw_qe is not None else None
        except (TypeError, ValueError):
            raise HTTPException(422, detail={"error": "INVALID_QUIET_HOURS"}) from None
        created = await asyncio.to_thread(
            notifications.create_binding,
            get_db(),
            notification_key_ring,
            tenant_id=s.tenant_id,
            user_id=s.user_id,
            workspace_id=s.workspace_id,
            channel=str(body.get("channel") or ""),
            label=str(body.get("label") or ""),
            destination=dest_raw,
            consent_granted=consent_granted,
            quiet_start_hour=quiet_start,
            quiet_end_hour=quiet_end,
            timezone_id=str(body.get("timezoneId") or "UTC"),
        )
        return {"schemaVersion": "1.0", "binding": created}

    @app.post("/v1/notifications/bindings/{binding_id}/consent")
    async def update_notification_consent(binding_id: str, request: Request, s: S) -> dict[str, Any]:
        if not is_uuid7(binding_id):
            raise HTTPException(404)
        raw = await request.body()
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise HTTPException(422, detail={"error": "INVALID_JSON"}) from None
        if not isinstance(body, dict) or not isinstance(body.get("granted"), bool):
            raise HTTPException(422, detail={"error": "GRANTED_BOOLEAN_REQUIRED"})
        updated = await asyncio.to_thread(
            notifications.set_binding_consent,
            get_db(),
            tenant_id=s.tenant_id,
            user_id=s.user_id,
            binding_id=binding_id,
            granted=bool(body["granted"]),
        )
        if updated is None:
            raise HTTPException(404)
        return {"schemaVersion": "1.0", "binding": updated}

    @app.post("/v1/notifications/bindings/{binding_id}/opt-out")
    async def opt_out_notification_binding(binding_id: str, s: S) -> dict[str, Any]:
        if not is_uuid7(binding_id):
            raise HTTPException(404)
        updated = await asyncio.to_thread(
            notifications.opt_out_binding,
            get_db(),
            tenant_id=s.tenant_id,
            user_id=s.user_id,
            binding_id=binding_id,
            revoke=False,
        )
        if updated is None:
            raise HTTPException(404)
        return {"schemaVersion": "1.0", "binding": updated}

    @app.delete("/v1/notifications/bindings/{binding_id}")
    async def revoke_notification_binding(binding_id: str, s: S) -> dict[str, Any]:
        if not is_uuid7(binding_id):
            raise HTTPException(404)
        updated = await asyncio.to_thread(
            notifications.opt_out_binding,
            get_db(),
            tenant_id=s.tenant_id,
            user_id=s.user_id,
            binding_id=binding_id,
            revoke=True,
        )
        if updated is None:
            raise HTTPException(404)
        return {"schemaVersion": "1.0", "binding": updated}

    @app.get("/v1/notifications")
    async def list_user_notifications(s: S, limit: int = 50) -> dict[str, Any]:
        items = await asyncio.to_thread(
            notifications.list_notifications, get_db(), tenant_id=s.tenant_id, user_id=s.user_id, limit=limit
        )
        return {"schemaVersion": "1.0", "items": items}

    @app.post("/v1/notifications", status_code=201)
    async def create_user_notification(request: Request, s: S) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > 32 * 1024:
            raise HTTPException(413, "too large")
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise HTTPException(422, detail={"error": "INVALID_JSON"}) from None
        if not isinstance(body, dict) or not is_uuid7(str(body.get("bindingId") or "")):
            raise HTTPException(422, detail={"error": "INVALID_BINDING_ID"})
        raw_ev = body.get("evidenceUrls")
        if raw_ev is not None and not isinstance(raw_ev, list):
            raise HTTPException(422, detail={"error": "INVALID_EVIDENCE_URLS"})
        obs_raw = body.get("observedAt")
        observed_dt: datetime | None = None
        if obs_raw is not None:
            if not isinstance(obs_raw, str) or not obs_raw.strip():
                raise HTTPException(422, detail={"error": "INVALID_OBSERVED_AT"})
            try:
                observed_dt = datetime.fromisoformat(obs_raw.strip().replace("Z", "+00:00"))
                if observed_dt.tzinfo is None:
                    observed_dt = observed_dt.replace(tzinfo=UTC)
            except ValueError:
                raise HTTPException(422, detail={"error": "INVALID_OBSERVED_AT"}) from None
        dry_run = _strict_optional_bool(body, "dryRun", default=False)
        dispatch_now = _strict_optional_bool(body, "dispatch", default=False)
        binding_id = str(body["bindingId"]).strip()
        priority = str(body.get("priority") or "standard")
        eval_now = notifications.utcnow()

        if dry_run:
            title_val = str(body.get("title") or "").strip()
            summary_val = str(body.get("summaryText") or "").strip()
            if not title_val or not summary_val:
                raise HTTPException(422, detail={"error": "INVALID_NOTIFICATION_CONTENT"})
            preview = await asyncio.to_thread(
                notifications.preview_notification_dry_run,
                get_db(),
                tenant_id=s.tenant_id,
                user_id=s.user_id,
                workspace_id=s.workspace_id,
                binding_ids=[binding_id],
                priority=priority,
                now_utc=eval_now,
            )
            return {
                "schemaVersion": "1.0",
                "dryRun": True,
                "preview": preview,
                "notification": None,
                "dispatch": None,
            }

        def tx_op() -> dict[str, Any]:
            with get_db().tx(tenant=s.tenant_id) as c:
                return notifications.enqueue_notification_tx(
                    c,
                    tenant_id=s.tenant_id,
                    user_id=s.user_id,
                    workspace_id=s.workspace_id,
                    binding_id=binding_id,
                    semantic_event_key=str(body.get("semanticEventKey") or f"manual:{uuid7()}"),
                    source_kind=str(body.get("sourceKind") or "system"),
                    source_ref=str(body.get("sourceRef") or "manual"),
                    title=str(body.get("title") or ""),
                    summary_text=str(body.get("summaryText") or ""),
                    evidence_urls=[str(u) for u in (raw_ev or []) if isinstance(u, str)],
                    observed_at=observed_dt,
                    now_utc=eval_now,
                    priority=priority,
                    delivery_purpose=str(body.get("deliveryPurpose") or "alert"),
                )

        created = await asyncio.to_thread(tx_op)
        dispatch_res = (
            await _notify_worker().run_once(
                tenant_id=s.tenant_id,
                owner_user_id=s.user_id,
                workspace_id=s.workspace_id,
                intent_ids=[str(created["id"])],
                batch=1,
            )
            if dispatch_now
            else None
        )
        return {"schemaVersion": "1.0", "dryRun": False, "notification": created, "dispatch": dispatch_res}

    @app.post("/v1/notifications/dispatch")
    async def dispatch_user_notifications(request: Request, s: S, limit: int = 20) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > 8 * 1024:
            raise HTTPException(413, "too large")
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise HTTPException(422, detail={"error": "INVALID_JSON"}) from None
        if not isinstance(body, dict):
            raise HTTPException(422, detail={"error": "INVALID_REQUEST"})
        dry_run = _strict_optional_bool(body, "dryRun", default=False)
        explicit_intent_ids: list[str] | None = None
        if "intentIds" in body:
            raw_ids = body["intentIds"]
            if not isinstance(raw_ids, list):
                raise HTTPException(422, detail={"error": "INVALID_INTENT_IDS"})
            seen_ids: set[str] = set()
            parsed_ids: list[str] = []
            for raw_id in raw_ids:
                if not isinstance(raw_id, str) or not is_uuid7(raw_id.strip()):
                    raise HTTPException(422, detail={"error": "INVALID_INTENT_IDS"})
                clean_id = raw_id.strip()
                if clean_id not in seen_ids:
                    seen_ids.add(clean_id)
                    parsed_ids.append(clean_id)
            explicit_intent_ids = parsed_ids
        bounded = max(1, min(limit, 50))
        if dry_run:
            preview = await asyncio.to_thread(
                notifications.preview_pending_dispatch,
                get_db(),
                tenant_id=s.tenant_id,
                user_id=s.user_id,
                workspace_id=s.workspace_id,
                limit=bounded,
            )
            return {"schemaVersion": "1.0", "dryRun": True, "preview": preview, "counts": None}
        eff_batch = max(1, len(explicit_intent_ids)) if explicit_intent_ids is not None else bounded
        counts = await _notify_worker().run_once(
            tenant_id=s.tenant_id,
            owner_user_id=s.user_id,
            workspace_id=s.workspace_id,
            intent_ids=explicit_intent_ids,
            batch=eff_batch,
        )
        return {"schemaVersion": "1.0", "dryRun": False, "counts": counts}

    @app.post("/v1/notifications/{notification_id}/read")
    async def mark_user_notification_read(notification_id: str, s: S) -> dict[str, Any]:
        if not is_uuid7(notification_id):
            raise HTTPException(404)
        updated = await asyncio.to_thread(
            notifications.mark_notification_read,
            get_db(),
            tenant_id=s.tenant_id,
            user_id=s.user_id,
            notification_id=notification_id,
        )
        return {"schemaVersion": "1.0", "notification": updated}

    from .development_routes import register_development_routes

    register_development_routes(
        app,
        get_db=get_db,
        scope_dep=scope,
        sentinel=sentinel,
        egress_proxy=egress_proxy,
        dev_github_client=dev_github_client,
        dev_debug_probe_fn=dev_debug_probe_fn,
        tool_gateway=tool_gateway,
    )

    # Optional same-origin built Web UI serving (for self-hosted console & real browser E2E)
    resolved_dist = web_dist_dir or (
        Path(os.environ["AIAGENT_WEB_DIST_DIR"]) if os.environ.get("AIAGENT_WEB_DIST_DIR") else None
    )
    if resolved_dist is not None:
        dist_root = resolved_dist.resolve()
        if (dist_root / "index.html").is_file():

            @app.get("/", include_in_schema=False)
            async def web_index() -> FileResponse:
                return FileResponse(dist_root / "index.html")

            @app.get("/assets/{asset_path:path}", include_in_schema=False)
            async def web_assets(asset_path: str) -> FileResponse:
                candidate = (dist_root / "assets" / asset_path).resolve()
                assets_dir = (dist_root / "assets").resolve()
                if not candidate.is_relative_to(assets_dir) or not candidate.is_file():
                    raise HTTPException(404)
                return FileResponse(candidate)

    return app


def main() -> None:
    import uvicorn
    from aiagent_core.logging import setup

    setup("api")
    config.require_isolation("api")
    uvicorn.run(
        create_app(),
        host=os.environ.get("AIAGENT_API_HOST", "127.0.0.1"),
        port=config.env_int("AIAGENT_API_PORT", 8080),
        log_config=None,
        timeout_graceful_shutdown=20,
    )


if __name__ == "__main__":
    main()
