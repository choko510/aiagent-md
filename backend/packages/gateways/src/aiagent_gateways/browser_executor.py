"""ToolGateway executor bridging admitted browser tool intents to BrowserSessionService + PostgresFencePort."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

from aiagent_browser import (
    Authority,
    BrowserError,
    BrowserSessionService,
    BrowserTarget,
    FixtureResponse,
    FormInput,
    PlaywrightBrowserWorker,
)
from aiagent_browser.contracts import WorkerPort
from aiagent_browser.pg_fence import PostgresFencePort
from aiagent_core import control
from aiagent_core.db import Database
from aiagent_core.logging import redact as redact_secrets
from aiagent_domain import uuid7

from .executors import Destination, ExecutorFailed, ExecutorUnknown

T = TypeVar("T")


class _DbAuthorityPort:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def authorize(self, authority: Authority, operation: str, session_id: str | None) -> None:
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


class _AllowListedFixtureGate:
    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        self._hosts = allowed_hosts

    async def permit(self, authority: Authority, url: str) -> None:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if parts.scheme != "fixture" or (parts.hostname or "").lower() not in self._hosts:
            raise BrowserError("EGRESS_DENIED")


class _CatalogFixturePort:
    def __init__(self, catalog: dict[str, FixtureResponse]) -> None:
        self._catalog = catalog

    async def fetch(self, url: str) -> FixtureResponse:
        return self._catalog.get(url, FixtureResponse(404))


class _CoreRedactor:
    def redact(self, text: str) -> str:
        return redact_secrets(text)


class BrowserFixtureExecutor:
    """Offline fixture Browser executor enforcing PostgresFencePort + ApprovalPort + PlaywrightBrowserWorker."""

    def __init__(
        self,
        db: Database,
        *,
        fixtures: dict[str, FixtureResponse] | None = None,
        allowed_hosts: frozenset[str] = frozenset({"alpha.test", "beta.test"}),
        worker: WorkerPort | None = None,
    ) -> None:
        self._db = db
        self.context: dict[str, dict[str, Any]] = {"__active_effect_key__": {}}
        self._worker = worker if worker is not None else PlaywrightBrowserWorker()
        self._fence = PostgresFencePort(db)
        self._service = BrowserSessionService(
            worker=self._worker,
            authority=_DbAuthorityPort(db),
            fence=self._fence,
            fixture_gate=_AllowListedFixtureGate(allowed_hosts),
            fixture=_CatalogFixturePort(dict(fixtures or {})),
            redactor=_CoreRedactor(),
            new_id=uuid7,
            allowed_fixture_hosts=allowed_hosts,
            approval=_DbApprovalPort(db, self.context),
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
                try:
                    fut.result(timeout=10.0)
                except Exception:
                    pass
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    def destination(self) -> Destination | None:
        return None  # Offline fixture-only sandbox; no external network socket exits this executor.

    @classmethod
    def from_fixture_file(cls, db: Database, path: str, *, worker: WorkerPort | None = None) -> BrowserFixtureExecutor:
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
        return cls(db, fixtures=catalog, allowed_hosts=hosts, worker=worker)

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

    async def execute(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        return await self._on_persistent_loop(self._execute_on_loop(effect_key, args, permit))

    async def _execute_on_loop(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        ctx = self.context.get(effect_key)
        if ctx is None or "tool" not in ctx:
            raise ExecutorFailed("no admission context")
        authority = self._authority_from_ctx(ctx)
        scope_key = (authority.tenant_id, authority.owner_user_id, authority.task_id)
        tool_name = str(ctx["tool"])

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
                else:
                    await self._ensure_lease_current(sid, scope_key, authority)
                obs = await self._service.navigate_fixture(sid, authority, str(args["url"]))
                return {
                    "sessionId": obs.session_id,
                    "generation": obs.generation,
                    "observation": obs.text,
                    "truncated": obs.text_truncated,
                }

            sid = str(args["sessionId"])
            await self._ensure_lease_current(sid, scope_key, authority)
            target = BrowserTarget(args["kind"], str(args["selector"]), args.get("roleName"))

            if tool_name == "browser_click":
                out = await self._service.click(sid, authority, target)
                return {
                    "sessionId": out.session_id,
                    "generation": out.generation,
                    "status": out.status,
                    "before": out.before_observation.text,
                    "after": out.after_observation.text,
                }

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
                    out = await self._service.submit(sid, authority, target, form_inputs)
                finally:
                    self.context["__active_effect_key__"].pop(sid, None)
                return {
                    "sessionId": out.session_id,
                    "generation": out.generation,
                    "status": out.status,
                    "before": out.before_observation.text,
                    "after": out.after_observation.text,
                    "receipt": f"browser:{ctx['intent_id']}:{out.generation}"[:120],
                }
        except BrowserError as exc:
            if exc.code == "ACTION_OUTCOME_UNKNOWN":
                raise ExecutorUnknown(exc.code) from exc
            raise ExecutorFailed(exc.code) from exc
        raise ExecutorFailed(f"unsupported browser tool: {tool_name}")

    async def query(self, effect_key: str, permit: str | None = None) -> dict[str, Any] | None:
        return None


def browser_from_env(db: Database | None) -> BrowserFixtureExecutor | None:
    if db is None:
        return None
    fixture_file = os.environ.get("AIAGENT_BROWSER_FIXTURE_FILE")
    if fixture_file:
        return BrowserFixtureExecutor.from_fixture_file(db, fixture_file)
    return None
