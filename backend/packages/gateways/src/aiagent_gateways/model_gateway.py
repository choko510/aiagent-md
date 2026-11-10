"""Model Gateway (independent process). Owns: admission, model policy, provider capability, recipient consent, tool
schema fixation, budget reservation, usage/cost, retry/fallback, streaming privacy gate, cancellation, ModelAttempt
records and the response checkpoint (assistant entries of Checkpoint HEAD). Holds the provider credentials; the
runtime never sees them, nor raw provider responses.

Sealed request identity = tenant, run, goal revision, stream, model, tool surface, system prompt and the whole
context. If HEAD already holds the answer to that identity (worker died after the commit), the answer is replayed -
but only after a FRESH admission (fence/goal/cancel/policy re-checked), and the replay is recorded.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiagent_core import control
from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_domain import sha256_hex, uuid7
from aiagent_domain.rpcwire import RpcError
from aiagent_domain.tools import INTERNAL_TOOLS, PROFILE_TOOLS, TOOLS

from .privacy import StreamPrivacyGate
from .providers import ProviderError

log = logging.getLogger("aiagent.model_gateway")
STREAMS_RE = ("main", "summarize")
MAX_CONTEXT_MESSAGES = 2000


def fault(point: str) -> None:
    if point in os.environ.get("AIAGENT_FAULT", "").split(","):
        os._exit(137)


@dataclass(frozen=True)
class Route:
    provider: str
    provider_model: str


@dataclass
class SealedModelRequest:
    claim: Claim
    stream: str
    system: str
    messages: list[dict[str, Any]]
    tools: list[str]
    model: str

    @classmethod
    def from_wire(cls, body: dict[str, Any]) -> SealedModelRequest:
        try:
            claim = Claim.from_wire(body["claim"])
            stream, model = str(body["stream"]), str(body["model"])
            messages, tools, system = body["messages"], body["tools"], str(body.get("system", ""))
        except (KeyError, ValueError, TypeError) as e:
            raise RpcError("BAD_REQUEST", type(e).__name__) from None
        if not (stream in STREAMS_RE or (stream.startswith("subagent:") and len(stream) <= 74)):
            raise RpcError("BAD_REQUEST", "stream")
        if not isinstance(messages, list) or len(messages) > MAX_CONTEXT_MESSAGES:
            raise RpcError("BAD_REQUEST", "messages")
        clean: list[dict[str, Any]] = []
        for m in messages:
            if not isinstance(m, dict) or m.get("kind") not in ("user", "assistant", "tool"):
                raise RpcError("BAD_REQUEST", "message kind")
            clean.append(
                {
                    "kind": m["kind"],
                    "content": str(m.get("content") or ""),
                    "tool_calls": [
                        {"id": str(t["id"]), "name": str(t["name"]), "args": dict(t["args"])}
                        for t in m.get("tool_calls") or []
                    ],
                    **(
                        {"tool_call_id": str(m["tool_call_id"]), "tool_name": str(m.get("tool_name") or "")}
                        if m["kind"] == "tool"
                        else {}
                    ),
                }
            )
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise RpcError("BAD_REQUEST", "tools")
        return cls(claim, stream, system, clean, sorted(set(tools)), model)

    def identity(self) -> str:
        ctx = [
            {
                "k": m["kind"],
                "c": m["content"],
                "t": m.get("tool_call_id"),
                "tc": [[t["id"], t["name"], t["args"]] for t in m["tool_calls"]],
            }
            for m in self.messages
        ]
        return sha256_hex(
            {
                "tenant": self.claim.tenant_id,
                "run": self.claim.run_id,
                "goal": self.claim.goal_revision,
                "stream": self.stream,
                "model": self.model,
                "tools": self.tools,
                "system": sha256_hex(self.system),
                "context": sha256_hex(ctx),
            }
        )


def _rpc(e: Exception) -> RpcError:
    if isinstance(e, control.ControlError):
        return RpcError(e.code, "")
    return RpcError("INTERNAL", type(e).__name__)


class ModelGateway:
    def __init__(
        self,
        db: Database,
        providers: dict[str, Any],
        routes: dict[str, list[Route]],
        *,
        consent: set[tuple[str, str]] | None = None,
        secrets: list[str] | None = None,
        max_retries: int = 2,
        cancel_poll_s: float = 0.5,
    ) -> None:
        self.db, self.providers, self.routes = db, providers, routes
        self.consent = consent or set()
        self.secrets = secrets or []
        self.max_retries, self.cancel_poll_s = max_retries, cancel_poll_s

    def recover_orphans(self, older_than_s: float) -> int:
        """Attempts left RESERVED/STREAMING by a crashed gateway: the provider may have been called (and billed), so
        the reservation is converted to UNKNOWN-quality usage (conservative) and the attempt marked FAILED."""
        with self.db.tx() as c:
            rows = c.execute(
                "UPDATE agent.model_attempts SET status_code='FAILED', usage_quality='UNKNOWN', reason_code='ORPHANED',"
                " finished_at=now() WHERE status_code IN ('RESERVED','STREAMING')"
                " AND created_at < now() - make_interval(secs => %s) RETURNING tenant_id, run_id, tokens_reserved",
                (older_than_s,),
            ).fetchall()
            for r in rows:
                c.execute(
                    "UPDATE agent.runs SET tokens_reserved = greatest(0, tokens_reserved - %s),"
                    " tokens_used = tokens_used + %s WHERE tenant_id=%s AND id=%s",
                    (r["tokens_reserved"], r["tokens_reserved"], r["tenant_id"], r["run_id"]),
                )
        return len(rows)

    # -------------------------------------------------------------------- admission
    def _admit(self, req: SealedModelRequest, *, cached: bool, est_tokens: int) -> str:
        attempt_id = uuid7()
        reason: str | None = None
        exc: Exception | None = None
        with self.db.tx() as c:
            try:
                run = control.check_current(c, req.claim, for_update=True)
                task = c.execute(
                    "SELECT profile_code FROM agent.tasks WHERE tenant_id=%s AND id=%s",
                    (req.claim.tenant_id, req.claim.task_id),
                ).fetchone()
                assert task is not None
                surface = set(PROFILE_TOOLS.get(task["profile_code"], ())) | INTERNAL_TOOLS
                if req.model not in run["allowed_models"] or req.model not in self.routes:
                    reason = "MODEL_POLICY"
                elif not set(req.tools) <= surface:
                    reason = "TOOL_SURFACE"
                elif req.stream.startswith("subagent:") and "task" in req.tools:
                    reason = "TOOL_SURFACE"  # subagents never inherit delegation
                else:
                    route = self.routes[req.model][0]
                    cap = self.providers[route.provider].capability(route.provider_model)
                    if req.tools and not cap.tools:
                        reason = "CAPABILITY"
                    elif sum(len(m["content"]) for m in req.messages) > cap.max_context_chars:
                        reason = "CONTEXT_TOO_LARGE"
                    elif (
                        cap.external_recipient
                        and (req.claim.tenant_id, cap.provider) not in self.consent
                        and ("*", cap.provider) not in self.consent
                    ):
                        reason = "RECIPIENT_CONSENT"
                    elif not cached and (
                        run["model_calls"] + 1 > run["max_model_calls"]
                        or run["tokens_used"] + run["tokens_reserved"] + est_tokens > run["token_budget"]
                    ):
                        reason = "BUDGET_EXHAUSTED"
                if reason is None and not cached:
                    c.execute(
                        "UPDATE agent.runs SET tokens_reserved = tokens_reserved + %s, model_calls = model_calls + 1,"
                        " updated_at=now() WHERE tenant_id=%s AND id=%s",
                        (est_tokens, req.claim.tenant_id, req.claim.run_id),
                    )
            except control.ControlError as e:
                reason, exc = e.code, e
            if isinstance(exc, control.TenantMismatch):
                raise _rpc(exc)  # nothing to record against: the run is not in this tenant
            c.execute(
                "INSERT INTO agent.model_attempts(tenant_id, id, task_id, run_id, stream, request_identity, model_key,"
                " run_lease_epoch, goal_revision, admitted, cached, reason_code, status_code, tool_surface,"
                " tokens_reserved) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    req.claim.tenant_id,
                    attempt_id,
                    req.claim.task_id,
                    req.claim.run_id,
                    req.stream,
                    req.identity(),
                    req.model,
                    req.claim.lease_epoch,
                    req.claim.goal_revision,
                    reason is None,
                    cached,
                    reason,
                    "DENIED" if reason else ("REPLAYED" if cached else "RESERVED"),
                    req.tools,
                    0 if (reason or cached) else est_tokens,
                ),
            )
        if exc is not None:
            raise _rpc(exc)
        if reason is not None:
            raise RpcError(reason)
        return attempt_id

    def _release(
        self, req: SealedModelRequest, attempt_id: str, est: int, status: str, usage: dict[str, int], quality: str
    ) -> None:
        with self.db.tx() as c:
            c.execute(
                "UPDATE agent.runs SET tokens_reserved = greatest(0, tokens_reserved - %s),"
                " tokens_used = tokens_used + %s, updated_at=now() WHERE tenant_id=%s AND id=%s",
                (
                    est,
                    usage.get("input_tokens", 0) + usage.get("output_tokens", 0) if quality != "UNKNOWN" else est,
                    req.claim.tenant_id,
                    req.claim.run_id,
                ),
            )
            c.execute(
                "UPDATE agent.model_attempts SET status_code=%s, input_tokens=%s, output_tokens=%s,"
                " usage_quality=%s, finished_at=now() WHERE tenant_id=%s AND id=%s",
                (
                    status,
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                    quality,
                    req.claim.tenant_id,
                    attempt_id,
                ),
            )

    # -------------------------------------------------------------------- main entry
    async def complete(
        self, req: SealedModelRequest, on_delta: Callable[[str], Awaitable[None]] | None = None
    ) -> dict[str, Any]:
        key = req.identity()
        with self.db.conn() as c:
            hit = control.find_assistant(c, req.claim.tenant_id, req.claim.run_id, req.stream, key)
        if hit is not None:
            await asyncio.to_thread(self._admit, req, cached=True, est_tokens=0)
            return {"message": hit, "replayed": True}
        est = sum(len(m["content"]) for m in req.messages) // 4 + 1024
        attempt_id = await asyncio.to_thread(self._admit, req, cached=False, est_tokens=est)
        try:
            gate, provider_attempts = await self._stream_with_retry(req, attempt_id, on_delta)
        except RpcError as e:
            await asyncio.to_thread(
                self._release, req, attempt_id, est, "CANCELLED" if e.code == "CANCELLED" else "FAILED", {}, "UNKNOWN"
            )
            raise
        result = gate.finish()
        usage, quality = result.usage, "MEASURED" if result.usage else "ESTIMATED"
        if not usage:
            usage = {"input_tokens": est - 1024, "output_tokens": len(result.text) // 4 + 1}
        message = {"kind": "assistant", "content": result.text, "tool_calls": result.tool_calls}
        fault("model_after_response")  # provider answered, HEAD not yet committed
        try:
            await asyncio.to_thread(self._commit, req, key, attempt_id, est, message, usage, quality, provider_attempts)
        except control.ControlError as e:
            await asyncio.to_thread(self._release, req, attempt_id, est, "FAILED", usage, quality)
            raise _rpc(e) from None
        with self.db.conn() as c:
            stored = control.find_assistant(c, req.claim.tenant_id, req.claim.run_id, req.stream, key)
        assert stored is not None
        return {"message": stored, "replayed": False}

    def _commit(
        self,
        req: SealedModelRequest,
        key: str,
        attempt_id: str,
        est: int,
        message: dict[str, Any],
        usage: dict[str, int],
        quality: str,
        provider_attempts: int,
    ) -> None:
        with self.db.tx() as c:
            control.append_head(
                c, req.claim, "assistant", message, writer="model_gateway", stream=req.stream, request_key=key
            )
            used = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            c.execute(
                "UPDATE agent.runs SET tokens_reserved = greatest(0, tokens_reserved - %s),"
                " tokens_used = tokens_used + %s, updated_at=now() WHERE tenant_id=%s AND id=%s",
                (est, used, req.claim.tenant_id, req.claim.run_id),
            )
            c.execute(
                "UPDATE agent.model_attempts SET status_code='COMPLETED', input_tokens=%s, output_tokens=%s,"
                " usage_quality=%s, provider_attempts=%s, provider_key=%s, finished_at=now()"
                " WHERE tenant_id=%s AND id=%s",
                (
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                    quality,
                    provider_attempts,
                    self.routes[req.model][0].provider,
                    req.claim.tenant_id,
                    attempt_id,
                ),
            )
            c.execute(
                "INSERT INTO agent.cost_usage_records(tenant_id, id, task_id, run_id, usage_key, metering_kind_code,"
                " usage_kind, provider_key, logical_call_id, input_units, output_units) VALUES"
                " (%s,%s,%s,%s,%s,'model_tokens',%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    req.claim.tenant_id,
                    uuid7(),
                    req.claim.task_id,
                    req.claim.run_id,
                    f"model:{attempt_id}",
                    quality,
                    self.routes[req.model][0].provider,
                    key,
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                ),
            )

    async def _cancelled(self, req: SealedModelRequest) -> str | None:
        def check() -> str | None:
            with self.db.conn() as c:
                try:
                    control.check_current(c, req.claim)
                except control.ControlError as e:
                    return e.code
            return None

        return await asyncio.to_thread(check)

    async def _stream_with_retry(
        self, req: SealedModelRequest, attempt_id: str, on_delta: Callable[[str], Awaitable[None]] | None
    ) -> tuple[StreamPrivacyGate, int]:
        allowed = {k: TOOLS[k] for k in req.tools if k in TOOLS}
        tool_schemas = [TOOLS[k].provider_schema() for k in req.tools if k in TOOLS]
        if "task" in req.tools:
            tool_schemas.append(
                {
                    "name": "task",
                    "description": "Delegate a sub-task to a sub-agent.",
                    "parameters": {
                        "type": "object",
                        "properties": {"description": {"type": "string"}, "subagent_type": {"type": "string"}},
                        "required": ["description", "subagent_type"],
                        "additionalProperties": False,
                    },
                }
            )
        attempts = 0
        last: ProviderError | None = None
        for route in self.routes[req.model]:  # fallback chain (only before any public output was emitted)
            for n in range(self.max_retries + 1):
                attempts += 1
                gate = StreamPrivacyGate(allowed, self.secrets)
                emitted = False
                seq = 0
                last_check = time.monotonic()
                try:
                    provider = self.providers[route.provider]
                    async for ev in provider.stream(
                        route.provider_model, req.system, req.messages, tool_schemas, stream=req.stream
                    ):
                        if time.monotonic() - last_check > self.cancel_poll_s:
                            last_check = time.monotonic()
                            code = await self._cancelled(req)
                            if code is not None:
                                raise RpcError(code)
                        public = gate.feed(ev)
                        if public:
                            emitted = True
                            await asyncio.to_thread(self._chunk, req, attempt_id, seq, public)
                            seq += 1
                            if on_delta is not None:
                                await on_delta(public)
                    code = await self._cancelled(req)
                    if code is not None:
                        raise RpcError(code)
                    return gate, attempts
                except ProviderError as e:
                    last = e
                    if not e.retryable or emitted:
                        raise RpcError(f"PROVIDER_{e.code}") from None
                    await asyncio.sleep(min(2.0, 0.05 * 2**n))
        raise RpcError(f"PROVIDER_{last.code if last else 'UNAVAILABLE'}")

    def _chunk(self, req: SealedModelRequest, attempt_id: str, seq: int, text: str) -> None:
        with self.db.conn() as c:
            c.execute(
                "INSERT INTO agent.run_stream_chunks(tenant_id, model_attempt_id, run_id, seq, text_delta)"
                " VALUES (%s,%s,%s,%s,%s)",
                (req.claim.tenant_id, attempt_id, req.claim.run_id, seq, text),
            )
