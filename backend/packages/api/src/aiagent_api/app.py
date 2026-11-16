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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hashlib import sha256
from http.cookies import CookieError, SimpleCookie
from types import SimpleNamespace
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from aiagent_core import commands, config, memory_tombstones, memory_writer
from aiagent_core.commands import CommandError, Scope
from aiagent_core.db import Database
from aiagent_core.logging import redact as redact_secrets
from aiagent_core.migrate import check_compatible
from aiagent_core.step4_gate import capability_snapshot
from aiagent_domain import is_uuid7
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
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
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


def _parse_set_cookie_records(origin_url: str, set_cookie_headers: list[str]) -> list[dict[str, Any]]:
    import time
    from datetime import UTC
    from email.utils import parsedate_to_datetime

    u = urlsplit(origin_url)
    host = (u.hostname or "").lower()
    is_https = u.scheme.lower() == "https"
    default_path = _default_cookie_path(origin_url)
    now_ts = time.time()
    cookies: list[dict[str, Any]] = []
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
            domain_attr = str(morsel["domain"] or "").strip().lstrip(".").lower()
            if domain_attr:
                if _is_ip_literal(host):
                    if host != domain_attr:
                        continue
                elif not (host == domain_attr or host.endswith("." + domain_attr)):
                    continue
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
                {
                    "name": name,
                    "value": morsel.value,
                    "domain": domain_attr or host,
                    "path": path_attr,
                    "secure": secure,
                    "httpOnly": bool(morsel["httponly"]),
                    "_hostOnly": not bool(domain_attr),
                    "_expiresAt": expires_at,
                }
            )
    return cookies


def _cookies_from_set_headers(origin_url: str, set_cookie_headers: list[str]) -> list[dict[str, Any]]:
    return [
        {k: v for k, v in rec.items() if not k.startswith("_")}
        for rec in _parse_set_cookie_records(origin_url, set_cookie_headers)
    ]


def _cookie_matches_url(cookie: dict[str, Any], target_url: str, *, now_ts: float | None = None) -> bool:
    import time

    exp = cookie.get("_expiresAt")
    if exp is not None and float(exp) <= (now_ts if now_ts is not None else time.time()):
        return False
    tu = urlsplit(target_url)
    t_scheme = tu.scheme.lower()
    t_host = (tu.hostname or "").lower()
    t_path = tu.path or "/"
    if bool(cookie.get("secure")) and t_scheme != "https":
        return False
    c_dom = str(cookie.get("domain") or "").lower()
    if not c_dom or not t_host:
        return False
    if bool(cookie.get("_hostOnly")) or _is_ip_literal(t_host):
        if t_host != c_dom:
            return False
    elif t_host != c_dom and not t_host.endswith("." + c_dom):
        return False
    c_path = str(cookie.get("path") or "/")
    if t_path == c_path:
        return True
    if c_path.endswith("/"):
        return t_path.startswith(c_path)
    return t_path.startswith(c_path) and t_path[len(c_path) : len(c_path) + 1] == "/"


def _matching_cookie_header(cookies: list[dict[str, Any]], target_url: str) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies if _cookie_matches_url(c, target_url))


_CHUNK_HEX_RE = re.compile(rb"^[0-9a-fA-F]{1,16}$")
_MAX_CHUNK_LINE_BYTES = 512
_MAX_CHUNKS = 4096
_CHUNK_READ_BLOCK = 8192
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


def _is_authenticated_proxy_denial(status_code: int, hmap: dict[str, str], permit: str) -> bool:
    if status_code != 403 or hmap.get("x-aiagent-egress") != "denied" or not permit:
        return False
    proof = hmap.get("x-aiagent-egress-proof", "")
    code_part, sep, mac_part = proof.partition(":")
    if not sep or not code_part or not mac_part:
        return False
    key = sha256(permit.encode("utf-8")).digest()
    expected = hmac.new(key, f"DENIED:{code_part}".encode(), sha256).hexdigest()
    return hmac.compare_digest(mac_part, expected)


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
    import contextlib

    pu = urlsplit(proxy_url)
    if pu.scheme != "http" or not pu.hostname:
        raise HTTPException(502, detail={"error": "INVALID_EGRESS_PROXY"})
    lines = [
        f"{method} {target_url} HTTP/1.1",
        f"Host: {host_header}",
        f"Proxy-Authorization: Aiagent human_takeover:{permit}",
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
            raise TimeoutError("takeover proxy timeout")
        return rem

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(pu.hostname, pu.port or 80), timeout=rem_timeout()
        )
    except OSError:
        raise HTTPException(502, detail={"error": "EGRESS_PROXY_UNREACHABLE"}) from None

    try:
        writer.write(raw)
        await asyncio.wait_for(writer.drain(), timeout=rem_timeout())
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=rem_timeout())
        head_lines = head[:-4].decode("latin-1").split("\r\n")
        parts = head_lines[0].split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise HTTPException(502, detail={"error": "INVALID_PROXY_RESPONSE"})
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
        if _is_authenticated_proxy_denial(status_code, hmap, permit):
            raise HTTPException(403, detail={"error": "EGRESS_DENIED"})
        chunks: list[bytes] = []
        total = 0
        cap = 524_288
        if "chunked" in hmap.get("transfer-encoding", "").lower():
            chunk_count = 0
            saw_eof = False
            while True:
                chunk_count += 1
                if chunk_count > _MAX_CHUNKS:
                    raise HTTPException(502, detail={"error": "INVALID_PROXY_RESPONSE"})
                try:
                    sz_line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=rem_timeout())
                except asyncio.LimitOverrunError:
                    raise HTTPException(502, detail={"error": "INVALID_PROXY_RESPONSE"}) from None
                except asyncio.IncompleteReadError:
                    raise HTTPException(502, detail={"error": "INCOMPLETE_PROXY_RESPONSE"}) from None
                if len(sz_line) > _MAX_CHUNK_LINE_BYTES:
                    raise HTTPException(502, detail={"error": "INVALID_PROXY_RESPONSE"})
                sz_hex = sz_line.strip().split(b";", 1)[0].strip()
                if not _CHUNK_HEX_RE.fullmatch(sz_hex):
                    raise HTTPException(502, detail={"error": "INVALID_PROXY_RESPONSE"})
                clen = int(sz_hex, 16)
                if clen == 0:
                    while True:
                        try:
                            end_line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=rem_timeout())
                        except asyncio.LimitOverrunError:
                            raise HTTPException(502, detail={"error": "INVALID_PROXY_RESPONSE"}) from None
                        except asyncio.IncompleteReadError:
                            raise HTTPException(502, detail={"error": "INCOMPLETE_PROXY_RESPONSE"}) from None
                        if len(end_line) > _MAX_CHUNK_LINE_BYTES or not end_line.endswith(b"\r\n"):
                            raise HTTPException(502, detail={"error": "INVALID_PROXY_RESPONSE"})
                        if end_line == b"\r\n":
                            break
                    saw_eof = True
                    break
                if clen > cap - total:
                    raise HTTPException(502, detail={"error": "RESPONSE_TOO_LARGE"})
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
                    raise HTTPException(502, detail={"error": "INCOMPLETE_PROXY_RESPONSE"}) from None
                if crlf != b"\r\n":
                    raise HTTPException(502, detail={"error": "INVALID_PROXY_RESPONSE"})
            if not saw_eof:
                raise HTTPException(502, detail={"error": "INCOMPLETE_PROXY_RESPONSE"})
        elif hmap.get("content-length", "").strip():
            cl_str = hmap["content-length"].strip()
            if not cl_str.isdigit():
                raise HTTPException(502, detail={"error": "INVALID_PROXY_RESPONSE"})
            expected = int(cl_str)
            if expected > cap:
                raise HTTPException(502, detail={"error": "RESPONSE_TOO_LARGE"})
            while total < expected:
                blk = await asyncio.wait_for(
                    reader.read(min(_CHUNK_READ_BLOCK, expected - total)), timeout=rem_timeout()
                )
                if not blk:
                    raise HTTPException(502, detail={"error": "INCOMPLETE_PROXY_RESPONSE"})
                chunks.append(blk)
                total += len(blk)
        else:
            while True:
                blk = await asyncio.wait_for(reader.read(_CHUNK_READ_BLOCK), timeout=rem_timeout())
                if not blk:
                    break
                if total + len(blk) > cap:
                    raise HTTPException(502, detail={"error": "RESPONSE_TOO_LARGE"})
                chunks.append(blk)
                total += len(blk)
        return status_code, set_cookies, b"".join(chunks).decode("utf-8", "replace")
    except (HTTPException, TimeoutError):
        raise
    except (OSError, asyncio.IncompleteReadError, ValueError):
        raise HTTPException(502, detail={"error": "INVALID_PROXY_RESPONSE"}) from None
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def create_app(
    db: Database | None = None,
    *,
    storage_key_ring: Any = None,
    sentinel: Any = None,
    egress_proxy: str | None = None,
    allowed_web_hosts: frozenset[str] | None = None,
    takeover_resolver: Callable[[str, int], Awaitable[list[str]]] | None = None,
    takeover_verify_ssl: Any = True,
    takeover_http_fn: Callable[[str, str, dict[str, str], bytes | None], Awaitable[tuple[int, list[str], str]]]
    | None = None,
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

    async def scope(authorization: Annotated[str | None, Header()] = None) -> Scope:
        if not authorization or not authorization.startswith("Bearer ") or len(authorization) > 400:
            raise HTTPException(401, "unauthenticated")
        h = sha256_bytes(authorization[7:].encode())
        row = await asyncio.to_thread(get_db().one, "SELECT * FROM agent.resolve_api_token(%s)", (h,))
        if row is None:
            raise HTTPException(401, "unauthenticated")
        return Scope(row["tenant_id"], row["user_id"], row["workspace_id"])

    S = Annotated[Scope, Depends(scope)]

    @app.exception_handler(CommandError)
    async def _cmd(_: Request, e: CommandError) -> JSONResponse:
        return JSONResponse({"error": e.code}, status_code=e.http)

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
    ) -> StreamingResponse:
        bounded = max(1, min(limit, 500))
        poll_cap = max(1, min(max_polls, 60))

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

        initial_state, initial_events = await asyncio.to_thread(load_events, after_offset)

        async def sse() -> AsyncIterator[str]:
            cursor = after_offset
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
                    " t.created_at, t.updated_at, r.status_code AS run_state"
                    " FROM agent.tasks t LEFT JOIN agent.runs r ON r.tenant_id=t.tenant_id AND r.id=t.active_run_id"
                    " WHERE t.tenant_id=%s AND t.owner_user_id=%s ORDER BY t.created_at DESC LIMIT %s",
                    (s.tenant_id, s.user_id, bounded),
                ).fetchall()
                return [
                    {
                        "id": str(r["id"]),
                        "state": str(r["status_code"]),
                        "profile": str(r["profile_code"]),
                        "goalRevision": str(r["goal_revision"]),
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
        u = urlsplit(target_url)
        scheme = u.scheme.lower()
        if scheme not in ("http", "https") or not u.hostname or u.username is not None or u.password is not None:
            raise HTTPException(422, detail={"error": "INVALID_TARGET_URL"})
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

        async def _execute_takeover_http(
            url_str: str, req_meth: str, hdrs: dict[str, str], body_bytes: bytes | None
        ) -> tuple[int, list[str], str]:
            if takeover_http_fn is not None:
                return await takeover_http_fn(url_str, req_meth, hdrs, body_bytes)
            import ipaddress
            import socket

            import httpx

            pu = urlsplit(url_str)
            p_scheme = pu.scheme.lower()
            if (
                p_scheme not in ("http", "https")
                or not pu.hostname
                or pu.username is not None
                or pu.password is not None
            ):
                raise HTTPException(422, detail={"error": "INVALID_TARGET_URL"})
            if p_scheme == "http" and config.profile() == "production":
                raise HTTPException(422, detail={"error": "HTTPS_REQUIRED"})
            p_host = (pu.hostname or "").lower()
            target_port = pu.port or (443 if p_scheme == "https" else 80)
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
        req_u = urlsplit(req_url)
        req_scheme = req_u.scheme.lower()
        req_host = (req_u.hostname or "").lower()
        req_port = req_u.port or (443 if req_scheme == "https" else 80)

        verify_url_raw = parsed.get("verifyUrl")
        explicit_verify = isinstance(verify_url_raw, str) and bool(verify_url_raw.strip())
        verify_target: str | None = None
        if explicit_verify:
            candidate = str(verify_url_raw).strip()
            vu = urlsplit(candidate)
            v_scheme = vu.scheme.lower()
            if (
                v_scheme not in ("http", "https")
                or not vu.hostname
                or vu.username is not None
                or vu.password is not None
            ):
                raise HTTPException(422, detail={"error": "INVALID_TARGET_URL"})
            if req_scheme == "https" and v_scheme != "https":
                raise HTTPException(422, detail={"error": "VERIFY_URL_DOWNGRADE"})
            v_host = vu.hostname.lower()
            v_port = vu.port or (443 if v_scheme == "https" else 80)
            if (v_scheme, v_host, v_port) != (req_scheme, req_host, req_port):
                raise HTTPException(422, detail={"error": "VERIFY_URL_ORIGIN_MISMATCH"})
            verify_target = candidate
        elif isinstance(session_current_url, str) and session_current_url.startswith(("http://", "https://")):
            vu = urlsplit(session_current_url)
            v_scheme = vu.scheme.lower()
            v_host = (vu.hostname or "").lower()
            v_port = vu.port or (443 if v_scheme == "https" else 80)
            if (
                (v_scheme, v_host, v_port) == (req_scheme, req_host, req_port)
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
                    merged = {(c["name"], c["domain"], c["path"]): c for c in cookie_records}
                    for nc in _parse_set_cookie_records(verify_target, v_cookies_raw):
                        merged[(nc["name"], nc["domain"], nc["path"])] = nc
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
            {k: v for k, v in rec.items() if not k.startswith("_")}
            for rec in cookie_records
            if rec.get("_expiresAt") is None or float(rec["_expiresAt"]) > now_ts
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
                    "SELECT watch_id, timezone_id, max_occurrences, revision, active, created_at"
                    " FROM agent.automation_watches"
                    " WHERE tenant_id=%s AND owner_user_id=%s ORDER BY created_at DESC LIMIT %s",
                    (s.tenant_id, s.user_id, bounded),
                ).fetchall()
                return [
                    {
                        "watchId": str(r["watch_id"]),
                        "timezoneId": str(r["timezone_id"]),
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
