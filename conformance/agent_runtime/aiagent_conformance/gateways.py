"""Fake Model Gateway / Fake Tool Gateway shared by both candidates.

Both gateways are *outside* the framework: the framework only sees neutral messages in and
neutral messages out. The model "provider credential" lives only here and must never appear
in framework state (checked by the conformance suite).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from psycopg.types.json import Jsonb

from .control import Cancelled, Claim, Control, FencedOut, StaleGoal, canonical, fault, race, sha
from .fixtures import POLICIES, is_summarization_request

FAKE_PROVIDER_SECRET = "sk-fake-provider-SECRET-7f3a"  # must never leave the gateway

READ_TOOLS = {"search"}
WRITE_TOOLS = {"send_message"}  # external, irreversible, approval required
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "search": {
        "description": "Read-only web search (fake).",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "send_message": {
        "description": "Send an external message. Irreversible; needs user approval.",
        "parameters": {
            "type": "object",
            "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
            "required": ["to", "body"],
            "additionalProperties": False,
        },
    },
}


@dataclass
class SealedModelRequest:
    """PoC shape of SealedModelRequestSnapshot (Model Router 02-09). Built by the adapter."""

    claim: Claim
    stream: str
    messages: list[dict[str, Any]]
    tools: list[str]
    policy: str
    model_id: str = "fake-gateway"

    def identity(self) -> str:
        """Snapshot identity used to reuse a committed answer. Everything that can change the answer or its
        admissibility is part of it: tenant, Goal revision, policy, model, tool surface and the *whole* context
        (order and content). The fence is deliberately excluded so a re-claimed worker can replay."""
        ctx = [
            {"k": m.get("kind"), "c": m.get("content"), "t": m.get("tool_call_id"),
             "tc": [[tc.get("id"), tc.get("name"), tc.get("args")] for tc in m.get("tool_calls") or []]}
            for m in self.messages
        ]
        return sha({
            "tenant": self.claim.tenant_id, "run": self.claim.root_run_id, "goal": self.claim.goal_revision,
            "stream": self.stream, "policy": self.policy, "model": self.model_id,
            "tools": sorted(self.tools), "context": sha(ctx),
        })


class PolicyDenied(Exception):
    pass


class ModelGateway:
    """Independent model gateway. Owns admission (tenant/cancel/fence/goal revision/model policy), budget
    counting and the provider credential. Admission runs for every request, including answers replayed
    from HEAD."""

    def __init__(self, ctl: Control):
        self.ctl = ctl

    def admit(self, req: SealedModelRequest, *, cached: bool) -> None:
        race(f"model_before_admission@{req.stream}", self.ctl.url)
        run = self.ctl.run(req.claim.root_run_id)
        reason = None
        if run is None or run["tenant_id"] != req.claim.tenant_id:
            reason = "TENANT"
        elif run["status"] == "CANCELLED":
            reason = "CANCELLED"
        elif run["fence"] != req.claim.fence:
            reason = "FENCED"
        elif run["goal_revision"] != req.claim.goal_revision:
            reason = "STALE_GOAL"
        elif req.model_id not in run["allowed_models"]:
            reason = "MODEL_POLICY"
        self.ctl.db.execute(
            "INSERT INTO aiagent.model_calls(run_id, stream, goal_revision, tenant_id, model_id, admitted, cached, reason, tools)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (req.claim.root_run_id, req.stream, req.claim.goal_revision, req.claim.tenant_id, req.model_id,
             reason is None, cached, reason, sorted(req.tools)),
        )
        if reason == "FENCED":
            raise FencedOut(req.claim.run_id)
        if reason == "STALE_GOAL":
            raise StaleGoal(req.claim.run_id)
        if reason == "CANCELLED":
            raise Cancelled(req.claim.run_id)
        if reason is not None:
            raise PolicyDenied(reason)

    def complete(self, req: SealedModelRequest) -> dict[str, Any]:
        self.admit(req, cached=False)
        _ = sha(FAKE_PROVIDER_SECRET)  # "uses" the credential; it is never returned
        if is_summarization_request(req.messages):
            n = len(req.messages)
            return {"kind": "assistant", "content": f"SUMMARY of {n} earlier messages: user wants widget price.", "tool_calls": []}
        out = POLICIES[req.policy](req.stream, req.messages, req.tools)
        # Real providers mint fresh tool_call ids per call: we do too, so only durable replay keeps them stable.
        for tc in out.get("tool_calls", []):
            tc["id"] = "call_" + uuid.uuid4().hex[:16]
        return {"kind": "assistant", "content": out.get("content", ""), "tool_calls": out.get("tool_calls", [])}


def sealed_model_call(ctl: Control, gw: ModelGateway, req: SealedModelRequest) -> dict[str, Any]:
    """The only model entry point used by adapters.

    1. If HEAD already holds the answer to this exact snapshot (crash after commit), replay it - but only
       after a fresh admission with the *current* Goal / fence / tenant / cancel / model policy.
    2. Otherwise call the independent Model Gateway, then CAS-append the answer to HEAD.
    """
    if is_summarization_request(req.messages):
        # Compaction output is a derived view: it never lands in the main transcript (HEAD stays raw).
        req.stream = "summarize"
    key = req.identity()
    hit = ctl.find_assistant(req.claim.root_run_id, req.stream, key)
    if hit is not None:
        gw.admit(req, cached=True)
        return hit
    resp = gw.complete(req)
    fault("after_model_response")  # provider answered, HEAD not yet committed
    ctl.append_head(req.claim, "assistant", resp, stream=req.stream, request_key=key)
    stored = ctl.find_assistant(req.claim.root_run_id, req.stream, key)
    assert stored is not None
    return stored


@dataclass
class ToolOutcome:
    status: str  # DONE | CONFIRMED | PENDING_APPROVAL | DENIED | EXPIRED | STALE_GOAL | REVOKED | CANCELLED | UNKNOWN
    result: dict[str, Any] = field(default_factory=dict)
    approval_id: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in ("DONE", "CONFIRMED", "DENIED", "EXPIRED", "STALE_GOAL", "REVOKED", "CANCELLED")

    def as_tool_content(self) -> str:
        return canonical({"status": self.status, **self.result})


class ToolGateway:
    """Fake CHAT_03 Tool Execution Gateway: schema → goal/fence → approval → ledger → executor."""

    def __init__(self, ctl: Control, approval_ttl_s: int = 3600):
        self.ctl = ctl
        self.ttl = approval_ttl_s
        self.naive = False  # negative control: framework-only durability, no ledger

    def submit(self, claim: Claim, intent_key: str, tool: str, args: dict[str, Any]) -> ToolOutcome:
        if tool not in TOOL_SCHEMAS:
            return ToolOutcome("DENIED", {"error": "unknown tool"})
        schema = TOOL_SCHEMAS[tool]["parameters"]
        if set(args) - set(schema["properties"]) or not set(schema["required"]) <= set(args):
            return ToolOutcome("DENIED", {"error": "schema"})
        if self.naive:
            return self._naive(intent_key, tool, args)
        db = self.ctl.db
        args_hash = sha([tool, args])
        run = self.ctl.run(claim.root_run_id)
        if run["fence"] != claim.fence:
            raise FencedOut(claim.run_id)
        if run["tenant_id"] != claim.tenant_id:
            return ToolOutcome("DENIED", {"error": "tenant"})
        row = db.execute("SELECT * FROM aiagent.tool_ledger WHERE intent_key=%s", (intent_key,)).fetchone()
        if row is not None and (row["args_hash"] != args_hash or row["root_run_id"] != claim.root_run_id):
            return ToolOutcome("DENIED", {"error": "intent_key reused with different args or run"})
        if row is None:
            if run["status"] == "CANCELLED":
                return ToolOutcome("CANCELLED")
            if run["goal_revision"] != claim.goal_revision:
                return ToolOutcome("STALE_GOAL")
            if tool in READ_TOOLS:
                res = {"result": f"widget price is 42 (query={args['query']})"}
                db.execute(
                    "INSERT INTO aiagent.tool_ledger(intent_key, run_id, root_run_id, tool, args_hash, goal_revision, tenant_id, status, result)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,'DONE',%s) ON CONFLICT DO NOTHING",
                    (intent_key, claim.run_id, claim.root_run_id, tool, args_hash, claim.goal_revision, claim.tenant_id, Jsonb(res)),
                )
                return ToolOutcome("DONE", res)
            approval_id = "apr_" + intent_key
            with db.transaction():
                db.execute(
                    "INSERT INTO aiagent.tool_ledger(intent_key, run_id, root_run_id, tool, args_hash, goal_revision, tenant_id, status)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,'PENDING_APPROVAL')",
                    (intent_key, claim.run_id, claim.root_run_id, tool, args_hash, claim.goal_revision, claim.tenant_id),
                )
                db.execute(
                    "INSERT INTO aiagent.approvals VALUES (%s,%s,%s,%s,%s,'PENDING',NULL, now() + make_interval(secs=>%s))",
                    (approval_id, claim.root_run_id, intent_key, args_hash, claim.goal_revision, self.ttl),
                )
            return ToolOutcome("PENDING_APPROVAL", approval_id=approval_id)

        db.execute("UPDATE aiagent.tool_ledger SET attempts = attempts + 1 WHERE intent_key=%s", (intent_key,))
        st = row["status"]
        if st == "SENDING":  # crashed between external send and confirm: never blind-retry
            db.execute("UPDATE aiagent.tool_ledger SET status='UNKNOWN' WHERE intent_key=%s AND status='SENDING'", (intent_key,))
            return ToolOutcome("UNKNOWN", {"note": "MAY_HAVE_SENT; reconcile before retry"})
        if st != "PENDING_APPROVAL":
            return ToolOutcome(st, row["result"] or {})
        return self._admit_and_execute(claim, intent_key, args, args_hash)

    def _admit_and_execute(self, claim: Claim, intent_key: str, args: dict[str, Any], args_hash: str) -> ToolOutcome:
        """Final admission and the transition to SENDING are ONE transaction holding row locks on the run,
        the approval and the ledger row. Steer / Cancel / Revoke update those rows, so they serialize with it:
        committed before -> the send is refused; committed after -> the send was already admitted (the
        SENDING commit is the linearization point) and stays MAY_HAVE_SENT/CONFIRMED."""
        db = self.ctl.db
        db.execute("SELECT pg_advisory_lock(hashtext(%s))", (claim.root_run_id,))  # per-run write serialization
        try:
            race("gateway_before_final_admission", self.ctl.url)
            fenced = False
            with db.transaction():
                run = db.execute(
                    "SELECT status, fence, goal_revision FROM aiagent.runs WHERE run_id=%s FOR SHARE", (claim.root_run_id,)
                ).fetchone()
                apr = db.execute(
                    "SELECT status, args_hash, goal_revision, approval_id, expires_at < now() AS expired"
                    " FROM aiagent.approvals WHERE intent_key=%s FOR SHARE", (intent_key,)
                ).fetchone()
                led = db.execute(
                    "SELECT status, result FROM aiagent.tool_ledger WHERE intent_key=%s FOR UPDATE", (intent_key,)
                ).fetchone()
                if led["status"] != "PENDING_APPROVAL":
                    return ToolOutcome(led["status"], led["result"] or {})
                final = None
                if run["fence"] != claim.fence:
                    fenced = True
                elif run["status"] == "CANCELLED":
                    final = "CANCELLED"
                elif apr["status"] == "REVOKED":
                    final = "REVOKED"
                elif apr["status"] == "REJECTED":
                    final = "DENIED"
                elif apr["expired"]:
                    final = "EXPIRED"
                elif apr["status"] == "PENDING":
                    return ToolOutcome("PENDING_APPROVAL", approval_id=apr["approval_id"])
                elif apr["args_hash"] != args_hash:
                    final = "DENIED"
                elif not (run["goal_revision"] == apr["goal_revision"] == claim.goal_revision):
                    final = "STALE_GOAL"
                if final:
                    db.execute("UPDATE aiagent.tool_ledger SET status=%s WHERE intent_key=%s", (final, intent_key))
                    return ToolOutcome(final)
                if not fenced:
                    db.execute("UPDATE aiagent.tool_ledger SET status='SENDING' WHERE intent_key=%s", (intent_key,))
            if fenced:
                raise FencedOut(claim.run_id)
            race("gateway_after_final_admission", self.ctl.url)
            db.execute("INSERT INTO aiagent.external_sends(intent_key, payload) VALUES (%s,%s)", (intent_key, Jsonb(args)))
            fault("gateway_after_send")  # external effect happened, ledger still SENDING
            res = {"receipt": "rcpt_" + intent_key[-8:]}
            db.execute("UPDATE aiagent.tool_ledger SET status='CONFIRMED', result=%s WHERE intent_key=%s", (Jsonb(res), intent_key))
            return ToolOutcome("CONFIRMED", res)
        finally:
            db.execute("SELECT pg_advisory_unlock(hashtext(%s))", (claim.root_run_id,))

    def _naive(self, intent_key: str, tool: str, args: dict[str, Any]) -> ToolOutcome:
        if tool in WRITE_TOOLS:
            self.ctl.db.execute(
                "INSERT INTO aiagent.external_sends(intent_key, payload) VALUES (%s,%s)", (intent_key, Jsonb(args))
            )
            return ToolOutcome("CONFIRMED", {"receipt": "naive"})
        return ToolOutcome("DONE", {"result": "widget price is 42"})


class AwaitingApproval(Exception):
    def __init__(self, approval_id: str, tool_call_id: str):
        super().__init__(approval_id)
        self.approval_id = approval_id
        self.tool_call_id = tool_call_id


class UnknownEffect(Exception):
    pass


_inflight: dict[str, int] = {}


async def gateway_tool_stub(
    ctl: Control, tgw: ToolGateway, claim: Claim, tool_call_id: str, tool: str, args: dict[str, Any], stream: str = "main"
) -> str:
    """Body of every framework tool function. Never executes anything itself.

    Returns the tool content for the model, or raises AwaitingApproval / UnknownEffect so the
    adapter can pause the framework at its interrupt point.
    """
    # LOOP-004: only a proposal already committed to Checkpoint HEAD may reach the gateway, and a
    # replayed framework call must match it exactly (checked before any cached result is returned).
    prop = ctl.find_proposal(claim.root_run_id, tool_call_id)
    if prop is None or prop["name"] != tool or prop["args"] != args:
        from .port import DerivedStateMismatch

        raise DerivedStateMismatch(
            f"tool call {tool_call_id} ({tool}) not in HEAD or differs: call_digest={sha([tool, args])}"
            f" head_digest={sha([prop.get('name'), prop.get('args')]) if prop else None}")
    hit = ctl.find_tool(claim.root_run_id, tool_call_id)
    if hit is not None:
        return hit["content"]
    is_write = tool in WRITE_TOOLS
    if is_write:
        _inflight[claim.root_run_id] = _inflight.get(claim.root_run_id, 0) + 1
        ctl.db.execute(
            "INSERT INTO aiagent.tool_concurrency VALUES (%s,%s) ON CONFLICT (run_id) DO UPDATE"
            " SET max_inflight_writes = GREATEST(aiagent.tool_concurrency.max_inflight_writes, EXCLUDED.max_inflight_writes)",
            (claim.root_run_id, _inflight[claim.root_run_id]),
        )
    try:
        await asyncio.sleep(0.05)  # widen the window so framework-side parallelism is observable
        out = await asyncio.to_thread(tgw.submit, claim, tool_call_id, tool, args)
    finally:
        if is_write:
            _inflight[claim.root_run_id] -= 1
    fault(f"after_tool_ack:{tool}") if out.status in ("CONFIRMED", "DONE") else None
    if out.status == "PENDING_APPROVAL":
        assert out.approval_id
        raise AwaitingApproval(out.approval_id, tool_call_id)
    if out.status == "UNKNOWN":
        raise UnknownEffect(tool_call_id)
    if out.status == "CANCELLED":
        raise Cancelled(claim.run_id)
    content = out.as_tool_content()
    ctl.append_head(
        claim,
        "tool",
        {"kind": "tool", "tool_call_id": tool_call_id, "tool_name": tool, "content": content},
        stream=stream,
        tool_call_id=tool_call_id,
    )
    return content


def parse_tool_content(content: str) -> dict[str, Any]:
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return {"raw": content}


async def settle_pending(ctl: Control, tgw: ToolGateway, claim: Claim) -> None:
    """Before (re)building framework state from HEAD, resolve every proposed-but-unanswered tool call
    of the main stream through the gateway (same intent keys -> idempotent). Raises AwaitingApproval /
    UnknownEffect exactly like the in-framework stub."""
    answered = {e["payload"]["tool_call_id"] for e in ctl.head(claim.root_run_id) if e["kind"] == "tool"}
    for e in ctl.head(claim.root_run_id, "main"):
        if e["kind"] != "assistant":
            continue
        for tc in e["payload"].get("tool_calls", []):
            if tc["id"] not in answered and tc["name"] in TOOL_SCHEMAS:
                await gateway_tool_stub(ctl, tgw, claim, tc["id"], tc["name"], tc["args"])
