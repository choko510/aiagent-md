"""ToolGateway executor bridging admitted browser tool intents to BrowserSessionService + PostgresFencePort.

Supports both offline `fixture://` sessions and Sentinel/SSRF-vetted `https://` sessions with
AES-GCM encrypted `storageState` persistence and owner-driven Human Takeover resumption.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import json
import os
import ssl
import threading
from collections.abc import Coroutine
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlencode, urlsplit, urlunsplit

from aiagent_browser import (
    Authority,
    BrowserError,
    BrowserSessionService,
    BrowserTarget,
    FixtureResponse,
    FormInput,
    PlaywrightBrowserWorker,
    State,
)
from aiagent_browser.contracts import WorkerPort
from aiagent_browser.pg_fence import PostgresFencePort
from aiagent_browser.service import compute_form_binding
from aiagent_core import config, control
from aiagent_core.db import Database
from aiagent_core.logging import redact as redact_secrets
from aiagent_domain import uuid7

from .context_keys import KeyRing
from .executors import Destination, ExecutorFailed, ExecutorUnknown
from .web_fetch import HARD_MAX_FETCH_BYTES, PermitIssuer, Resolver, parse_safe_http_url, safe_http_get

T = TypeVar("T")


@dataclass
class _PermitBundle:
    session_key: str
    hop0_permit: str | None
    hop0_dest: Destination | None
    permit_issuer: PermitIssuer | None
    hop0_used: bool = False


_PERMIT_CTX: contextvars.ContextVar[_PermitBundle | None] = contextvars.ContextVar("browser_permit_ctx", default=None)


class _DbAuthorityPort:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def authorize(self, authority: Authority, operation: str, session_id: str | None) -> None:
        del operation, session_id
        with self._db.tx() as c:
            row = c.execute(
                "SELECT t.owner_user_id, t.workspace_id, t.goal_revision, t.active_run_id,"
                " r.lease_epoch, r.lease_owner_ref, (r.lease_expires_at > clock_timestamp()) AS lease_alive,"
                " r.restore_incarnation, r.cancel_requested, r.pause_requested, r.status_code,"
                " sc.restore_incarnation AS current_incarnation"
                " FROM agent.tasks t"
                " JOIN agent.runs r ON r.tenant_id = t.tenant_id AND r.id = %s"
                " JOIN agent.schema_control sc ON sc.singleton"
                " WHERE t.tenant_id = %s AND t.id = %s AND r.task_id = t.id",
                (authority.run_id, authority.tenant_id, authority.task_id),
            ).fetchone()
            if (
                row is None
                or str(row["owner_user_id"]) != authority.owner_user_id
                or int(row["goal_revision"]) != int(authority.goal_revision)
                or str(row["active_run_id"]) != authority.run_id
                or int(row["lease_epoch"]) != int(authority.lease_epoch)
                or not row["lease_owner_ref"]
                or not row["lease_alive"]
                or (authority.worker_ref is not None and str(row["lease_owner_ref"]) != authority.worker_ref)
                or int(row["restore_incarnation"]) != int(row["current_incarnation"])
                or row["cancel_requested"]
                or row["pause_requested"]
                or row["status_code"]
                in (
                    "CANCELLED",
                    "CANCELLING",
                    "PAUSED",
                    "FAILED",
                    "SUCCEEDED",
                    "PARTIAL",
                    "EXPIRED",
                    "BUDGET_EXHAUSTED",
                )
                or not control.scope_active(c, authority.tenant_id, authority.owner_user_id, str(row["workspace_id"]))
            ):
                raise BrowserError("AUTH_DENIED")


class _DbApprovalPort:
    """Verifies explicit owner approval committed in agent.approval_records."""

    def __init__(self, db: Database, intents_by_session_ctx: dict[str, dict[str, Any]]) -> None:
        self._db = db
        self._ctx = intents_by_session_ctx

    async def approve(self, authority: Authority, session_id: str, target: BrowserTarget) -> None:
        del target
        effect_key = self._ctx.get("__active_effect_key__", {}).get(session_id)
        if not effect_key or effect_key not in self._ctx:
            raise BrowserError("APPROVAL_REQUIRED")
        meta = self._ctx[effect_key]
        with self._db.tx() as c:
            row = c.execute(
                "SELECT a.decision_code, a.goal_revision, i.status_code"
                " FROM agent.tool_intents i"
                " JOIN agent.approval_records a ON a.tenant_id = i.tenant_id AND a.tool_intent_id = i.id"
                " WHERE i.tenant_id = %s AND i.id = %s AND i.run_id = %s",
                (authority.tenant_id, meta["intent_id"], authority.run_id),
            ).fetchone()
            if (
                row is None
                or row["decision_code"] != "APPROVED"
                or int(row["goal_revision"]) != int(authority.goal_revision)
                or row["status_code"] != "SENDING"
            ):
                raise BrowserError("APPROVAL_REQUIRED")


class _BrowserPolicyGate:
    def __init__(
        self,
        allowed_fixture_hosts: frozenset[str],
        allowed_web_hosts: frozenset[str] = frozenset(),
        *,
        allow_https: bool = False,
        allow_http_web: bool = False,
    ) -> None:
        self._fixture_hosts = allowed_fixture_hosts
        self._web_hosts = allowed_web_hosts
        self._allow_https = allow_https
        self._allow_http_web = allow_http_web

    async def permit(self, authority: Authority, url: str) -> None:
        del authority
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        if scheme == "fixture":
            if host not in self._fixture_hosts:
                raise BrowserError("EGRESS_DENIED")
            return
        if scheme == "https" and self._allow_https and ("*" in self._web_hosts or host in self._web_hosts):
            return
        if scheme == "http" and self._allow_http_web and ("*" in self._web_hosts or host in self._web_hosts):
            return
        raise BrowserError("EGRESS_DENIED")


class _HybridCatalogAndWebPort:
    """Serves `fixture://` from static catalog and `http(s)://` via Sentinel/EgressProxy + session Cookie jar."""

    def __init__(
        self,
        catalog: dict[str, FixtureResponse],
        worker: WorkerPort,
        *,
        proxy: str | None = None,
        resolver: Resolver | None = None,
        allow_private_addrs: frozenset[str] = frozenset(),
        verify: bool | ssl.SSLContext = True,
        require_proxy: bool = False,
    ) -> None:
        self._catalog = catalog
        self._worker = worker
        self._proxy = proxy
        self._resolver = resolver
        self._allow_private_addrs = allow_private_addrs
        self._verify = verify
        self._require_proxy = require_proxy

    async def _permit_for(self, url: str, method: str) -> str | None:
        parsed = parse_safe_http_url(url)
        needed = Destination(parsed.scheme, parsed.host, parsed.port, method.upper())
        bundle = _PERMIT_CTX.get()
        if bundle is None:
            return None
        if not bundle.hop0_used and bundle.hop0_permit is not None and bundle.hop0_dest == needed:
            bundle.hop0_used = True
            return bundle.hop0_permit
        if bundle.permit_issuer is not None:
            return await bundle.permit_issuer(needed)
        if self._proxy is not None:
            raise BrowserError("EGRESS_DENIED")
        return None

    async def _do_web_request(
        self,
        session_id: str | None,
        url: str,
        *,
        method: str = "GET",
        request_body: bytes | None = None,
        content_type: str | None = None,
    ) -> FixtureResponse:
        permit = await self._permit_for(url, method)
        extra_headers: dict[str, str] = {}
        if content_type:
            extra_headers["Content-Type"] = content_type
        if session_id is not None:
            cookies_fn = getattr(self._worker, "cookies_for_url", None)
            if callable(cookies_fn):
                cookie_hdr = await cookies_fn(session_id, url)
                if cookie_hdr:
                    extra_headers["Cookie"] = cookie_hdr
        try:
            resp = await safe_http_get(
                url,
                method=method,
                request_body=request_body,
                resolver=self._resolver,
                allow_private_addrs=self._allow_private_addrs,
                proxy=self._proxy,
                proxy_permit=permit,
                extra_headers=extra_headers or None,
                max_bytes=min(524_288, HARD_MAX_FETCH_BYTES),
                verify=self._verify,
                max_redirects=0,
                follow_redirects=False,
                raise_on_http_error=False,
                require_proxy=self._require_proxy,
            )
            if session_id is not None and resp.set_cookies:
                apply_fn = getattr(self._worker, "apply_set_cookies", None)
                if callable(apply_fn):
                    await apply_fn(session_id, url, resp.set_cookies)
            return FixtureResponse(
                resp.status,
                resp.body.decode("utf-8", "replace"),
                redirect_to=resp.location,
                truncated=resp.truncated,
            )
        except ExecutorFailed as exc:
            if "egress_denied" in str(exc) or "sentinel:" in str(exc) or "egress_proxy_required" in str(exc):
                raise BrowserError("EGRESS_DENIED") from exc
            raise BrowserError("FIXTURE_RESPONSE_DENIED") from exc
        except ExecutorUnknown as exc:
            raise BrowserError("ACTION_OUTCOME_UNKNOWN") from exc

    async def fetch(self, url: str) -> FixtureResponse:
        if url.startswith("fixture://"):
            return self._catalog.get(url, FixtureResponse(404))
        return await self._do_web_request(None, url)

    async def fetch_session(self, session_id: str, url: str) -> FixtureResponse:
        if url.startswith("fixture://"):
            return self._catalog.get(url, FixtureResponse(404))
        return await self._do_web_request(session_id, url)

    async def submit_form(
        self, session_id: str, url: str, method: str, fields: list[tuple[str, str]]
    ) -> FixtureResponse:
        m = method.upper()
        if m == "GET":
            encoded = urlencode(fields)
            parts = urlsplit(url)
            q = f"{parts.query}&{encoded}" if (parts.query and encoded) else (encoded or parts.query)
            target_url = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", q, ""))
            return await self._do_web_request(session_id, target_url, method="GET")
        body = urlencode(fields).encode("utf-8")
        return await self._do_web_request(
            session_id,
            url,
            method="POST",
            request_body=body,
            content_type="application/x-www-form-urlencoded",
        )


class _CoreRedactor:
    def redact(self, text: str) -> str:
        return redact_secrets(text)


def _storage_aad(authority: Authority, session_id: str) -> bytes:
    return (
        f"browser-storage-v1|{authority.tenant_id}|{authority.owner_user_id}|{authority.task_id}|{session_id}".encode()
    )


class BrowserSessionExecutor:
    """Browser executor enforcing PostgresFencePort + ApprovalPort + PlaywrightBrowserWorker + encrypted storageState."""

    def __init__(
        self,
        db: Database,
        *,
        fixtures: dict[str, FixtureResponse] | None = None,
        allowed_hosts: frozenset[str] = frozenset({"alpha.test", "beta.test"}),
        allowed_web_hosts: frozenset[str] = frozenset(),
        allow_https: bool = False,
        allow_http_web: bool = False,
        allow_private_addrs: frozenset[str] = frozenset(),
        proxy: str | None = None,
        resolver: Resolver | None = None,
        verify_ssl: bool | ssl.SSLContext = True,
        storage_key_ring: KeyRing | None = None,
        worker: WorkerPort | None = None,
        require_proxy: bool = False,
    ) -> None:
        self._db = db
        self.proxy = proxy
        self.context: dict[str, dict[str, Any]] = {"__active_effect_key__": {}}
        self._storage_key_ring = storage_key_ring
        self._worker = worker if worker is not None else PlaywrightBrowserWorker()
        self._fence = PostgresFencePort(db)
        web_port = _HybridCatalogAndWebPort(
            dict(fixtures or {}),
            self._worker,
            proxy=proxy,
            resolver=resolver,
            allow_private_addrs=allow_private_addrs,
            verify=verify_ssl,
            require_proxy=require_proxy,
        )
        self._service = BrowserSessionService(
            worker=self._worker,
            authority=_DbAuthorityPort(db),
            fence=self._fence,
            fixture_gate=_BrowserPolicyGate(
                allowed_hosts,
                allowed_web_hosts,
                allow_https=allow_https,
                allow_http_web=allow_http_web,
            ),
            fixture=web_port,
            redactor=_CoreRedactor(),
            new_id=uuid7,
            allowed_fixture_hosts=allowed_hosts,
            approval=_DbApprovalPort(db, self.context),
            allowed_web_hosts=allowed_web_hosts,
            allow_https=allow_https,
            allow_http_web=allow_http_web,
            web_submitter=web_port,
        )
        # Task-scoped session tracking keyed by (tenant_id, owner_user_id, task_id).
        self._task_sessions: dict[tuple[str, str, str], tuple[str, Authority]] = {}
        self._bg_lock = threading.Lock()
        self._bg_loop: asyncio.AbstractEventLoop | None = None
        self._bg_thread: threading.Thread | None = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._bg_lock:
            if self._bg_loop is not None and not self._bg_loop.is_closed():
                return self._bg_loop
            ready: concurrent.futures.Future[asyncio.AbstractEventLoop] = concurrent.futures.Future()

            def runner() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                ready.set_result(loop)
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            t = threading.Thread(target=runner, name="browser-executor-loop", daemon=True)
            t.start()
            self._bg_thread = t
            self._bg_loop = ready.result(timeout=10.0)
            return self._bg_loop

    async def _on_persistent_loop(self, coro: Coroutine[Any, Any, T]) -> T:
        loop = self._ensure_loop()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            return await coro
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return await asyncio.wrap_future(fut)

    def close(self) -> None:
        with self._bg_lock:
            loop = self._bg_loop
            thread = self._bg_thread
            self._bg_loop = None
            self._bg_thread = None
        if loop is not None and not loop.is_closed():
            if hasattr(self._worker, "shutdown"):
                fut = asyncio.run_coroutine_threadsafe(self._worker.shutdown(), loop)
                with contextlib.suppress(Exception):
                    fut.result(timeout=10.0)
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    def destination(self) -> Destination | None:
        return None

    def destination_for(self, args: dict[str, Any]) -> Destination | None:
        return self.destination_for_tool("browser_navigate", args)

    @staticmethod
    def _authority_from_claim(claim: control.Claim) -> Authority:
        return Authority(
            tenant_id=str(claim.tenant_id),
            owner_user_id=str(claim.owner_user_id),
            task_id=str(claim.task_id),
            run_id=str(claim.run_id),
            goal_revision=str(claim.goal_revision),
            lease_epoch=str(claim.lease_epoch),
            worker_ref=str(claim.worker_ref) if claim.worker_ref is not None else None,
        )

    async def _preview_submit_binding(self, args: dict[str, Any], claim: control.Claim) -> dict[str, Any] | None:
        sid = args.get("sessionId")
        if not isinstance(sid, str):
            return None
        authority = self._authority_from_claim(claim)
        scope_key = (authority.tenant_id, authority.owner_user_id, authority.task_id)
        try:
            # Verify caller tenant/owner/task ownership BEFORE any lease lookup, DB touch, or DOM access.
            entry = self._service._entry(sid, authority)
            await self._ensure_lease_current(sid, scope_key, authority)
            await self._sync_takeover_resume_if_needed(sid, authority)
            entry = await self._service._operation(sid, authority, "submit")
            async with entry.lock:
                await self._service._operation(sid, authority, "submit")
                if entry.current_url is None or not entry.current_url.startswith(("http://", "https://")):
                    return None
                self._service._validate_url(entry.current_url)
                extract_fn = getattr(self._worker, "extract_form_submission", None)
                if not callable(extract_fn):
                    raise ExecutorFailed("CONTROLLED_EGRESS_REQUIRED")
                target = BrowserTarget(args["kind"], str(args["selector"]), args.get("roleName"))
                raw_inputs = args.get("inputs") or []
                form_inputs = tuple(
                    FormInput(
                        BrowserTarget(item["kind"], str(item["selector"]), item.get("roleName")), str(item["value"])
                    )
                    for item in raw_inputs
                )
                try:
                    extracted = await extract_fn(
                        sid, target, form_inputs, current_url=entry.current_url, apply_inputs=False
                    )
                except TypeError:
                    extracted = await extract_fn(sid, target, form_inputs, current_url=entry.current_url)
            if not isinstance(extracted, dict) or not extracted.get("hasForm"):
                raise ExecutorFailed("CONTROLLED_EGRESS_REQUIRED")
            action_url = self._service._validate_url(str(extracted["action"]))
            form_method = str(extracted.get("method") or "GET").upper()
            fields = list(extracted.get("fields") or ())
            return compute_form_binding(action_url, form_method, fields)
        except BrowserError as exc:
            raise ExecutorFailed(exc.code) from exc

    def bind_request(
        self, tool_name: str, args: dict[str, Any], *, claim: control.Claim | None = None
    ) -> dict[str, Any] | None:
        if tool_name != "browser_submit":
            return None
        if claim is None:
            raise ExecutorFailed("AUTHORITY_REQUIRED")
        try:
            loop = self._ensure_loop()
            fut = asyncio.run_coroutine_threadsafe(self._preview_submit_binding(args, claim), loop)
        except RuntimeError as exc:
            raise ExecutorFailed("BROWSER_UNAVAILABLE") from exc
        try:
            return fut.result(timeout=15.0)
        except (TimeoutError, concurrent.futures.TimeoutError):
            fut.cancel()
            raise ExecutorFailed("PREVIEW_TIMEOUT") from None
        except concurrent.futures.CancelledError:
            raise ExecutorFailed("PREVIEW_CANCELLED") from None
        except ExecutorFailed:
            raise
        except BrowserError as exc:
            raise ExecutorFailed(exc.code) from exc
        except RuntimeError as exc:
            raise ExecutorFailed("BROWSER_UNAVAILABLE") from exc

    def destination_for_tool(
        self, tool_name: str, args: dict[str, Any], *, bound_form: dict[str, Any] | None = None
    ) -> Destination | None:
        if tool_name == "browser_submit" and bound_form is not None and isinstance(bound_form.get("action"), str):
            parsed = parse_safe_http_url(str(bound_form["action"]))
            return Destination(parsed.scheme, parsed.host, parsed.port, str(bound_form.get("method") or "POST").upper())
        url = args.get("url")
        method = "POST" if tool_name == "browser_submit" else "GET"
        if not isinstance(url, str):
            sid = args.get("sessionId")
            if isinstance(sid, str):
                entry = self._service._entries.get(sid)
                if entry is not None:
                    url = entry.current_url
        if not isinstance(url, str) or url.startswith("fixture://"):
            return None
        parsed = parse_safe_http_url(url)
        return Destination(parsed.scheme, parsed.host, parsed.port, method)

    @classmethod
    def from_fixture_file(
        cls,
        db: Database,
        path: str,
        *,
        worker: WorkerPort | None = None,
        storage_key_ring: KeyRing | None = None,
    ) -> BrowserSessionExecutor:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        hosts = frozenset(str(h).strip().lower() for h in raw.get("allowedHosts", ["alpha.test", "beta.test"]))
        catalog: dict[str, FixtureResponse] = {}
        for url, spec in raw.get("fixtures", {}).items():
            if isinstance(spec, str):
                catalog[url] = FixtureResponse(200, spec)
            else:
                catalog[url] = FixtureResponse(
                    int(spec.get("status", 200)),
                    str(spec.get("html", "")),
                    spec.get("redirectTo"),
                )
        web_hosts = frozenset(str(h).strip().lower() for h in raw.get("allowedWebHosts", []))
        return cls(
            db,
            fixtures=catalog,
            allowed_hosts=hosts,
            allowed_web_hosts=web_hosts,
            allow_https=bool(raw.get("allowHttps", bool(web_hosts))),
            allow_http_web=bool(raw.get("allowHttpWeb", False)) and config.profile() != "production",
            storage_key_ring=storage_key_ring,
            worker=worker,
        )

    @staticmethod
    def _authority_from_ctx(ctx: dict[str, Any]) -> Authority:
        worker_ref = ctx.get("worker_ref")
        return Authority(
            tenant_id=str(ctx["tenant_id"]),
            owner_user_id=str(ctx["owner_user_id"]),
            task_id=str(ctx["task_id"]),
            run_id=str(ctx["run_id"]),
            goal_revision=str(ctx["goal_revision"]),
            lease_epoch=str(ctx["lease_epoch"]),
            worker_ref=str(worker_ref) if worker_ref is not None else None,
        )

    async def _ensure_lease_current(
        self, session_id: str, scope_key: tuple[str, str, str], new_auth: Authority
    ) -> None:
        prev = self._task_sessions.get(scope_key)
        if prev is not None and prev[0] == session_id and prev[1] != new_auth:
            await self._fence.advance(session_id, prev[1], new_auth, "AGENT")
            self._task_sessions[scope_key] = (session_id, new_auth)

    async def _sync_takeover_resume_if_needed(self, session_id: str, authority: Authority) -> None:
        status_row = await self._fence.read_status(session_id, authority, mode="AGENT")
        svc_state, svc_gen, _, _ = self._service.session_details(session_id, authority)
        db_gen = int(status_row["generation"])
        if status_row["state"] == "ACTIVE" and db_gen > svc_gen and svc_state in (State.PAUSED, State.NEEDS_LOGIN):
            await self._service.apply_owner_takeover_resume(session_id, authority, db_generation=db_gen)
            if self._storage_key_ring is not None and hasattr(self._worker, "restore_storage_state"):
                enc = await self._fence.fetch_storage_state(session_id, authority, mode="AGENT")
                if enc is not None:
                    raw_plain = self._decrypt_storage(enc, authority, session_id)
                    await self._worker.restore_storage_state(session_id, json.loads(raw_plain))  # type: ignore[attr-defined]

    def _encrypt_storage(
        self, state_dict: dict[str, Any], authority: Authority, session_id: str
    ) -> tuple[bytes, bytes, str, str]:
        assert self._storage_key_ring is not None
        plaintext = json.dumps(state_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        key_id = self._storage_key_ring.active
        nonce = os.urandom(12)
        ciphertext = self._storage_key_ring.cipher(key_id).encrypt(
            nonce, plaintext, _storage_aad(authority, session_id)
        )
        return nonce, ciphertext, key_id, sha256(plaintext).hexdigest()

    def _decrypt_storage(self, enc: dict[str, Any], authority: Authority, session_id: str) -> bytes:
        assert self._storage_key_ring is not None
        plain = self._storage_key_ring.cipher(enc["key_id"]).decrypt(
            enc["nonce"], enc["ciphertext"], _storage_aad(authority, session_id)
        )
        if sha256(plain).hexdigest() != enc["sha256"]:
            raise BrowserError("STORAGE_STATE_TAMPERED")
        return plain

    async def _persist_session_state(
        self,
        session_id: str,
        authority: Authority,
        obs_text: str,
        obs_truncated: bool,
    ) -> None:
        state, _, cur_url, challenge = self._service.session_details(session_id, authority)
        if state is State.RECOVERING or state is State.CLOSED:
            return
        update_storage = False
        s_nonce = s_cipher = s_key_id = s_sha = None
        if self._storage_key_ring is not None and hasattr(self._worker, "export_storage_state"):
            try:
                exported = await self._worker.export_storage_state(session_id)  # type: ignore[attr-defined]
                s_nonce, s_cipher, s_key_id, s_sha = self._encrypt_storage(exported, authority, session_id)
                update_storage = True
            except BrowserError:
                pass
        await self._fence.record_state(
            session_id,
            authority,
            mode="AGENT",
            state=state.value,
            current_url=cur_url,
            takeover_reason=challenge,
            obs_text=obs_text,
            obs_truncated=obs_truncated,
            update_storage=update_storage,
            storage_nonce=s_nonce,
            storage_ciphertext=s_cipher,
            storage_key_id=s_key_id,
            storage_sha256=s_sha,
        )

    async def execute(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        return await self._on_persistent_loop(self._execute_on_loop(effect_key, args, permit))

    async def _execute_on_loop(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        ctx = self.context.get(effect_key)
        if ctx is None or "tool" not in ctx:
            raise ExecutorFailed("no admission context")
        authority = self._authority_from_ctx(ctx)
        scope_key = (authority.tenant_id, authority.owner_user_id, authority.task_id)
        tool_name = str(ctx["tool"])
        bound_form = ctx.get("bound_form") if isinstance(ctx.get("bound_form"), dict) else None
        hop0_dest = self.destination_for_tool(tool_name, args, bound_form=bound_form)
        token_ctx = _PERMIT_CTX.set(
            _PermitBundle(
                session_key=effect_key,
                hop0_permit=permit,
                hop0_dest=hop0_dest,
                permit_issuer=ctx.get("permit_issuer"),
            )
        )

        try:
            if tool_name == "browser_navigate":
                sid = args.get("sessionId")
                if not isinstance(sid, str):
                    existing = self._task_sessions.get(scope_key)
                    if existing is None:
                        view = await self._service.create(authority)
                        sid = view.session_id
                        self._task_sessions[scope_key] = (sid, authority)
                    else:
                        sid = existing[0]
                        await self._ensure_lease_current(sid, scope_key, authority)
                        await self._sync_takeover_resume_if_needed(sid, authority)
                else:
                    await self._ensure_lease_current(sid, scope_key, authority)
                    await self._sync_takeover_resume_if_needed(sid, authority)
                obs = await self._service.navigate(sid, authority, str(args["url"]))
                await self._persist_session_state(sid, authority, obs.text, obs.text_truncated)
                state, _, _, challenge = self._service.session_details(sid, authority)
                out_nav: dict[str, Any] = {
                    "sessionId": obs.session_id,
                    "generation": obs.generation,
                    "state": state.value,
                    "observation": obs.text,
                    "truncated": obs.text_truncated,
                }
                if challenge is not None:
                    out_nav["takeoverReason"] = challenge
                return out_nav

            sid = str(args["sessionId"])
            await self._ensure_lease_current(sid, scope_key, authority)
            await self._sync_takeover_resume_if_needed(sid, authority)
            target = BrowserTarget(args["kind"], str(args["selector"]), args.get("roleName"))

            if tool_name == "browser_click":
                out = await self._service.click(sid, authority, target)
                await self._persist_session_state(
                    sid, authority, out.after_observation.text, out.after_observation.text_truncated
                )
                click_res: dict[str, Any] = {
                    "sessionId": out.session_id,
                    "generation": out.generation,
                    "status": out.status,
                    "before": out.before_observation.text,
                    "after": out.after_observation.text,
                }
                if out.status in ("NEEDS_LOGIN", "CAPTCHA_CHALLENGE"):
                    click_res["takeoverReason"] = out.status
                return click_res

            if tool_name == "browser_submit":
                raw_inputs = args.get("inputs") or []
                form_inputs = tuple(
                    FormInput(
                        BrowserTarget(item["kind"], str(item["selector"]), item.get("roleName")), str(item["value"])
                    )
                    for item in raw_inputs
                )
                self.context["__active_effect_key__"][sid] = effect_key
                try:
                    out = await self._service.submit(
                        sid,
                        authority,
                        target,
                        form_inputs,
                        expected_bound_form=bound_form,
                    )
                except BrowserError as exc:
                    if exc.code == "ACTION_OUTCOME_UNKNOWN":
                        with contextlib.suppress(Exception):
                            await self._persist_session_state(sid, authority, "", False)
                    raise
                finally:
                    self.context["__active_effect_key__"].pop(sid, None)
                try:
                    await self._persist_session_state(
                        sid, authority, out.after_observation.text, out.after_observation.text_truncated
                    )
                except Exception as exc:
                    raise ExecutorUnknown("POST_SEND_PERSIST_UNKNOWN") from exc
                sub_res: dict[str, Any] = {
                    "sessionId": out.session_id,
                    "generation": out.generation,
                    "status": out.status,
                    "before": out.before_observation.text,
                    "after": out.after_observation.text,
                    "receipt": f"browser:{ctx['intent_id']}:{out.generation}"[:120],
                }
                if out.status in ("NEEDS_LOGIN", "CAPTCHA_CHALLENGE"):
                    sub_res["takeoverReason"] = out.status
                return sub_res
        except BrowserError as exc:
            if exc.code == "ACTION_OUTCOME_UNKNOWN":
                raise ExecutorUnknown(exc.code) from exc
            raise ExecutorFailed(exc.code) from exc
        finally:
            _PERMIT_CTX.reset(token_ctx)
        raise ExecutorFailed(f"unsupported browser tool: {tool_name}")

    async def query(self, effect_key: str, permit: str | None = None) -> dict[str, Any] | None:
        del effect_key, permit
        return None


BrowserFixtureExecutor = BrowserSessionExecutor


def browser_from_env(db: Database | None) -> BrowserSessionExecutor | None:
    if db is None:
        return None
    from .servers import load_key_ring

    ring: KeyRing | None = None
    with contextlib.suppress(Exception):
        ring = load_key_ring()

    fixture_file = os.environ.get("AIAGENT_BROWSER_FIXTURE_FILE")
    if fixture_file:
        return BrowserSessionExecutor.from_fixture_file(db, fixture_file, storage_key_ring=ring)

    web_hosts_raw = os.environ.get("AIAGENT_BROWSER_ALLOWED_WEB_HOSTS")
    if web_hosts_raw:
        hosts = frozenset(x.strip().lower() for x in web_hosts_raw.split(",") if x.strip())
        if hosts:
            allow_priv: frozenset[str] = frozenset()
            if config.profile() != "production" and os.environ.get("AIAGENT_WEB_FETCH_ALLOW_PRIVATE"):
                allow_priv = frozenset(
                    x.strip() for x in os.environ["AIAGENT_WEB_FETCH_ALLOW_PRIVATE"].split(",") if x.strip()
                )
            return BrowserSessionExecutor(
                db,
                allowed_web_hosts=hosts,
                allow_https=True,
                allow_http_web=(
                    os.environ.get("AIAGENT_BROWSER_ALLOW_HTTP_WEB", "0") == "1" and config.profile() != "production"
                ),
                allow_private_addrs=allow_priv,
                proxy=os.environ.get("AIAGENT_EGRESS_PROXY"),
                storage_key_ring=ring,
                require_proxy=(config.profile() == "production"),
            )
    return None
