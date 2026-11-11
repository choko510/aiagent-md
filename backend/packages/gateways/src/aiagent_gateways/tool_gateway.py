"""Tool Execution Gateway (independent process; Intent / Approval / Effect Ledger owner).

Pipeline for every proposal (an untrusted input):
  HEAD proposal check (LOOP-004) -> registry schema -> profile surface -> tenant/owner/fence/goal/cancel admission ->
  budget -> Tool Intent -> [Approval] -> FINAL ADMISSION + SENDING (one TX, row locks) -> Sentinel permit ->
  executor -> receipt -> ledger + event -> HEAD tool result.

* Final admission and the transition to SENDING are one transaction holding the run (FOR SHARE), approval (FOR SHARE)
  and intent (FOR UPDATE) rows. Steer/Cancel/Revoke update those rows, so they serialize with it: committed before ->
  the send is refused; committed after -> the send was admitted (the SENDING commit is the linearization point).
* SENDING found on a later submit (gateway died between send and confirm) becomes UNKNOWN for non-read tools and is
  never retried automatically; only reconciliation (receipt query) or a human resolves it.
* Read-only tools (risk_class=read) are idempotent and may be re-executed after a crash.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import PurePosixPath
from typing import Any

from aiagent_core import control
from aiagent_core.control import Claim
from aiagent_core.db import Database, Row
from aiagent_core.events import append_event, enqueue_run_job
from aiagent_domain import canonical_json, sha256_hex, uuid7
from aiagent_domain.rpcwire import RpcError
from aiagent_domain.tools import INTERNAL_TOOLS, PROFILE_TOOLS, TOOLS
from jsonschema import Draft202012Validator
from psycopg.types.json import Jsonb

from .executors import Executor, ExecutorFailed, ExecutorUnknown, valid_receipt

log = logging.getLogger("aiagent.tool_gateway")
SERVICE = {"type": "service", "id": "tool_gateway"}
TERMINAL_INTENT = (
    "DONE",
    "CONFIRMED",
    "DENIED",
    "EXPIRED",
    "STALE_GOAL",
    "REVOKED",
    "CANCELLED",
    "BUDGET_EXHAUSTED",
    "FAILED",
)
MAX_CONTENT = 64 * 1024


def fault(point: str) -> None:
    if point in os.environ.get("AIAGENT_FAULT", "").split(","):
        os._exit(137)


def outcome(status: str, result: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {"status": status, "result": result or {}, **extra}


def content_of(o: dict[str, Any]) -> str:
    return canonical_json({"status": o["status"], **o["result"]})


class ToolGateway:
    def __init__(
        self,
        db: Database,
        executors: dict[str, Executor],
        *,
        sentinel: Any = None,
        approval_ttl_s: int = 3600,
        max_tool_calls: int = 32,
    ) -> None:
        self.db, self.executors, self.sentinel = db, executors, sentinel
        self.ttl, self.max_tool_calls = approval_ttl_s, max_tool_calls

    # ------------------------------------------------------------------ helpers
    def _record_head(self, claim: Claim, tool_call_id: str, tool: str, o: dict[str, Any], stream: str) -> None:
        with self.db.tx() as c:
            control.append_head(
                c,
                claim,
                "tool",
                {"kind": "tool", "tool_call_id": tool_call_id, "tool_name": tool, "content": content_of(o)},
                writer="tool_gateway",
                stream=stream,
                tool_call_id=tool_call_id,
                require_current_goal=False,
            )

    def _event(self, c: Any, claim: Claim, kind: str, intent_id: str, payload: dict[str, Any]) -> None:
        append_event(
            c,
            tenant_id=claim.tenant_id,
            aggregate_type="tool",
            aggregate_id=intent_id,
            kind=kind,
            payload=payload,
            actor=SERVICE,
            owner_user_id=claim.owner_user_id,
            task_id=claim.task_id,
            run_id=claim.run_id,
        )

    def _deny(self, claim: Claim, tool_call_id: str, tool: str, reason: str, stream: str) -> dict[str, Any]:
        o = outcome("DENIED", {"error": reason})
        self._record_head(claim, tool_call_id, tool, o, stream)
        return o

    # ------------------------------------------------------------------ submit
    async def submit(self, claim: Claim, tool_call_id: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        with self.db.conn() as c:
            prop = control.find_proposal(c, claim.tenant_id, claim.run_id, tool_call_id)
            hit = control.find_tool(c, claim.tenant_id, claim.run_id, tool_call_id)
        if prop is None or prop[0].get("name") != tool or prop[0].get("args") != args:
            raise RpcError("NOT_IN_HEAD", "tool call is not a committed proposal or differs from it")
        stream = prop[1]
        if hit is not None:
            import json

            data = json.loads(hit["content"])
            return outcome(data.pop("status"), data, replayed=True)
        spec = TOOLS.get(tool)
        if spec is None:
            return self._deny(claim, tool_call_id, tool, "unknown tool", stream)
        if not Draft202012Validator(spec.parameters).is_valid(args):
            return await asyncio.to_thread(self._deny, claim, tool_call_id, tool, "schema", stream)
        return await asyncio.to_thread(self._submit_sync, claim, tool_call_id, tool, args, stream)

    def _submit_sync(
        self, claim: Claim, tool_call_id: str, tool: str, args: dict[str, Any], stream: str
    ) -> dict[str, Any]:
        spec = TOOLS[tool]
        req_sha = sha256_hex({"tool": tool, "args": args})
        with self.db.tx() as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (claim.run_id,))
            try:
                run = control.check_current(c, claim)
            except control.Cancelled:
                run = None
                cancelled = True
            except control.ControlError as e:
                if isinstance(e, control.StaleGoal):
                    return self._finish_denied(c, claim, tool_call_id, tool, args, req_sha, spec, "STALE_GOAL", stream)
                raise RpcError(e.code) from None
            else:
                cancelled = False
            task = c.execute(
                "SELECT profile_code FROM agent.tasks WHERE tenant_id=%s AND id=%s", (claim.tenant_id, claim.task_id)
            ).fetchone()
            assert task is not None
            intent = c.execute(
                "SELECT * FROM agent.tool_intents WHERE tenant_id=%s AND run_id=%s AND invocation_key=%s FOR UPDATE",
                (claim.tenant_id, claim.run_id, tool_call_id),
            ).fetchone()
            if intent is not None:
                if intent["request_sha256"] != req_sha or intent["tool_key"] != tool:
                    raise RpcError("INTENT_CONFLICT")
                c.execute(
                    "UPDATE agent.tool_intents SET submissions=submissions+1 WHERE tenant_id=%s AND id=%s",
                    (claim.tenant_id, intent["id"]),
                )
                st = intent["status_code"]
                if cancelled and st in ("SENDING", "PENDING_APPROVAL") and spec.risk_class == "read":
                    return outcome("CANCELLED")
                if st == "SENDING":
                    if spec.risk_class == "read":
                        pass  # idempotent read: re-execute below
                    else:
                        c.execute(
                            "UPDATE agent.tool_intents SET status_code='UNKNOWN', updated_at=now()"
                            " WHERE tenant_id=%s AND id=%s AND status_code='SENDING'",
                            (claim.tenant_id, intent["id"]),
                        )
                        c.execute(
                            "UPDATE agent.side_effect_records SET outcome_code='UNKNOWN', updated_at=now()"
                            " WHERE tenant_id=%s AND tool_intent_id=%s AND outcome_code='SENDING'",
                            (claim.tenant_id, intent["id"]),
                        )
                        self._event(c, claim, "tool.effect_unknown", intent["id"], {"tool": tool})
                        return outcome("UNKNOWN", {"note": "MAY_HAVE_SENT; reconcile before any retry"})
                elif st == "UNKNOWN":
                    return outcome("UNKNOWN", {"note": "MAY_HAVE_SENT; reconcile before any retry"})
                elif st in TERMINAL_INTENT:
                    return outcome(st, dict(intent["result_json"] or {}))
                elif st == "PENDING_APPROVAL":
                    pass  # handled by final admission below (outside this TX)
            else:
                if cancelled:
                    return self._finish_denied(c, claim, tool_call_id, tool, args, req_sha, spec, "CANCELLED", stream)
                assert run is not None
                if tool not in PROFILE_TOOLS.get(task["profile_code"], ()):
                    return self._finish_denied(
                        c,
                        claim,
                        tool_call_id,
                        tool,
                        args,
                        req_sha,
                        spec,
                        "DENIED",
                        stream,
                        error="tool not allowed for this profile",
                    )
                n = c.execute(
                    "SELECT count(*) AS n FROM agent.tool_intents WHERE tenant_id=%s AND run_id=%s",
                    (claim.tenant_id, claim.run_id),
                ).fetchone()
                if n is not None and n["n"] >= self.max_tool_calls:
                    return self._finish_denied(
                        c, claim, tool_call_id, tool, args, req_sha, spec, "BUDGET_EXHAUSTED", stream
                    )
                intent_id = uuid7()
                status = "PENDING_APPROVAL" if spec.approval_required else "SENDING"
                c.execute(
                    "INSERT INTO agent.tool_intents(tenant_id, id, task_id, run_id, tool_key, invocation_key,"
                    " request_sha256, safe_arguments_json, risk_class, goal_revision, status_code, stream)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        claim.tenant_id,
                        intent_id,
                        claim.task_id,
                        claim.run_id,
                        tool,
                        tool_call_id,
                        req_sha,
                        Jsonb(args),
                        spec.risk_class,
                        claim.goal_revision,
                        status,
                        stream,
                    ),
                )
                self._event(c, claim, "tool.intent_recorded", intent_id, {"tool": tool, "risk": spec.risk_class})
                if spec.approval_required:
                    apr = uuid7()
                    summary = f"{tool} to={args.get('to', '')}"[:200]
                    c.execute(
                        "INSERT INTO agent.approval_records(tenant_id, id, tool_intent_id, task_id, run_id, owner_user_id,"
                        " tool_key, request_sha256, goal_revision, safe_summary, expires_at)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now() + make_interval(secs => %s))",
                        (
                            claim.tenant_id,
                            apr,
                            intent_id,
                            claim.task_id,
                            claim.run_id,
                            claim.owner_user_id,
                            tool,
                            req_sha,
                            claim.goal_revision,
                            summary,
                            self.ttl,
                        ),
                    )
                    append_event(
                        c,
                        tenant_id=claim.tenant_id,
                        aggregate_type="approval",
                        aggregate_id=apr,
                        kind="approval.requested",
                        payload={"tool": tool},
                        actor=SERVICE,
                        owner_user_id=claim.owner_user_id,
                        task_id=claim.task_id,
                        run_id=claim.run_id,
                    )
                    return outcome("PENDING_APPROVAL", approvalId=apr, requestSha256=req_sha)
                intent = c.execute(
                    "SELECT * FROM agent.tool_intents WHERE tenant_id=%s AND id=%s", (claim.tenant_id, intent_id)
                ).fetchone()
        assert intent is not None
        if intent["status_code"] == "PENDING_APPROVAL":
            return self._final_admission(claim, intent, tool_call_id, tool, args, req_sha, stream)
        return self._dispatch(claim, intent, tool_call_id, tool, args, stream, fence=1)

    def _finish_denied(
        self,
        c: Any,
        claim: Claim,
        tool_call_id: str,
        tool: str,
        args: dict[str, Any],
        req_sha: str,
        spec: Any,
        status: str,
        stream: str,
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        intent_id = uuid7()
        res = {"error": error} if error else {}
        c.execute(
            "INSERT INTO agent.tool_intents(tenant_id, id, task_id, run_id, tool_key, invocation_key, request_sha256,"
            " safe_arguments_json, risk_class, goal_revision, status_code, stream, result_json)"
            " SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s, t.goal_revision, %s, %s, %s FROM agent.tasks t"
            " WHERE t.tenant_id=%s AND t.id=%s ON CONFLICT (tenant_id, run_id, invocation_key) DO NOTHING",
            (
                claim.tenant_id,
                intent_id,
                claim.task_id,
                claim.run_id,
                tool,
                tool_call_id,
                req_sha,
                Jsonb(args),
                spec.risk_class,
                status,
                stream,
                Jsonb(res),
                claim.tenant_id,
                claim.task_id,
            ),
        )
        o = outcome(status, res)
        if status not in ("CANCELLED",):
            try:
                control.append_head(
                    c,
                    claim,
                    "tool",
                    {"kind": "tool", "tool_call_id": tool_call_id, "tool_name": tool, "content": content_of(o)},
                    writer="tool_gateway",
                    stream=stream,
                    tool_call_id=tool_call_id,
                    require_current_goal=False,
                )
            except control.ControlError:
                pass  # stale worker: ledger records the refusal, the next worker re-submits and reads it
        return o

    # ------------------------------------------------------------------ final admission
    def _final_admission(
        self, claim: Claim, intent: Row, tool_call_id: str, tool: str, args: dict[str, Any], req_sha: str, stream: str
    ) -> dict[str, Any]:
        with self.db.tx() as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (claim.run_id,))
            run = c.execute(
                "SELECT r.status_code, r.lease_epoch, r.cancel_requested, r.pause_requested, r.restore_incarnation, t.goal_revision,"
                " t.owner_user_id, t.workspace_id, s.restore_incarnation AS cur_inc FROM agent.runs r JOIN agent.tasks t ON"
                " t.tenant_id=r.tenant_id AND t.id=r.task_id CROSS JOIN agent.schema_control s"
                " WHERE r.tenant_id=%s AND r.id=%s FOR SHARE OF r",
                (claim.tenant_id, claim.run_id),
            ).fetchone()
            apr = c.execute(
                "SELECT id, decision_code, request_sha256, goal_revision, owner_user_id, expires_at < now() AS expired"
                " FROM agent.approval_records WHERE tenant_id=%s AND tool_intent_id=%s FOR SHARE",
                (claim.tenant_id, intent["id"]),
            ).fetchone()
            led = c.execute(
                "SELECT status_code, result_json FROM agent.tool_intents WHERE tenant_id=%s AND id=%s FOR UPDATE",
                (claim.tenant_id, intent["id"]),
            ).fetchone()
            assert run is not None and apr is not None and led is not None
            if led["status_code"] != "PENDING_APPROVAL":
                return outcome(led["status_code"], dict(led["result_json"] or {}))
            if not control.scope_active(c, claim.tenant_id, claim.owner_user_id, str(run["workspace_id"])):
                raise RpcError("SCOPE_REVOKED")
            if run["pause_requested"] or run["status_code"] == "PAUSED":
                raise RpcError("PAUSED")
            if (
                run["lease_epoch"] != claim.lease_epoch
                or run["owner_user_id"] != claim.owner_user_id
                or run["restore_incarnation"] != run["cur_inc"]
                or claim.restore_incarnation != run["cur_inc"]
            ):
                raise RpcError("FENCED")
            final: str | None = None
            if run["status_code"] in ("CANCELLED", "CANCELLING") or run["cancel_requested"]:
                final = "CANCELLED"
            elif apr["decision_code"] == "REVOKED":
                final = "REVOKED"
            elif apr["decision_code"] == "REJECTED":
                final = "DENIED"
            elif apr["decision_code"] == "EXPIRED" or apr["expired"]:
                final = "EXPIRED"
            elif apr["decision_code"] == "PENDING":
                return outcome("PENDING_APPROVAL", approvalId=apr["id"], requestSha256=apr["request_sha256"])
            elif apr["request_sha256"] != req_sha or apr["owner_user_id"] != claim.owner_user_id:
                final = "DENIED"
            elif not (run["goal_revision"] == apr["goal_revision"] == claim.goal_revision == intent["goal_revision"]):
                final = "STALE_GOAL"
            if final is not None:
                c.execute(
                    "UPDATE agent.tool_intents SET status_code=%s, updated_at=now(), record_version=record_version+1"
                    " WHERE tenant_id=%s AND id=%s",
                    (final, claim.tenant_id, intent["id"]),
                )
                self._event(c, claim, "tool.effect_recorded", intent["id"], {"tool": tool, "outcome": final})
                o = outcome(final)
            else:
                c.execute(
                    "UPDATE agent.tool_intents SET status_code='SENDING', updated_at=now(),"
                    " record_version=record_version+1 WHERE tenant_id=%s AND id=%s",
                    (claim.tenant_id, intent["id"]),
                )
                o = None
        if o is not None:
            if o["status"] != "CANCELLED":
                self._record_head(claim, tool_call_id, tool, o, stream)
            return o
        return self._dispatch(claim, intent, tool_call_id, tool, args, stream, fence=1)

    # ------------------------------------------------------------------ dispatch
    def _dispatch(
        self, claim: Claim, intent: Row, tool_call_id: str, tool: str, args: dict[str, Any], stream: str, *, fence: int
    ) -> dict[str, Any]:
        spec = TOOLS[tool]
        effect_key = f"{claim.tenant_id}:{intent['id']}"
        with self.db.tx() as c:
            prev = c.execute(
                "SELECT coalesce(max(attempt_no), 0) AS n FROM agent.tool_attempts WHERE tenant_id=%s"
                " AND tool_intent_id=%s",
                (claim.tenant_id, intent["id"]),
            ).fetchone()
            attempt_no = (prev["n"] if prev else 0) + 1
            attempt_id = uuid7()
            c.execute(
                "INSERT INTO agent.tool_attempts(tenant_id, id, tool_intent_id, attempt_no, fence_token, worker_ref,"
                " run_lease_epoch, status_code) VALUES (%s,%s,%s,%s,%s,%s,%s,'SENDING')",
                (
                    claim.tenant_id,
                    attempt_id,
                    intent["id"],
                    attempt_no,
                    attempt_no,
                    claim.worker_ref,
                    claim.lease_epoch,
                ),
            )
            if spec.risk_class != "read":
                c.execute(
                    "INSERT INTO agent.side_effect_records(tenant_id, id, tool_intent_id, effect_key, request_sha256,"
                    " outcome_code, attempt_fence) VALUES (%s,%s,%s,%s,%s,'SENDING',%s)",
                    (claim.tenant_id, uuid7(), intent["id"], effect_key, intent["request_sha256"], attempt_no),
                )
        executor = self.executors.get(spec.executor)
        if executor is None:
            return self._settle(
                claim,
                intent,
                attempt_id,
                tool_call_id,
                tool,
                stream,
                "FAILED",
                {"error": "executor unavailable"},
                effect="FAILED",
            )
        permit = None
        dest = executor.destination()
        if dest is not None:
            if self.sentinel is None:
                return self._settle(
                    claim,
                    intent,
                    attempt_id,
                    tool_call_id,
                    tool,
                    stream,
                    "FAILED",
                    {"error": "egress requires Sentinel"},
                    effect="FAILED",
                )
            try:
                permit = self.sentinel.authorize(claim, intent["id"], dest)
            except RpcError as e:
                return self._settle(
                    claim,
                    intent,
                    attempt_id,
                    tool_call_id,
                    tool,
                    stream,
                    "DENIED",
                    {"error": f"sentinel:{e.code}"},
                    effect="FAILED",
                )
        if hasattr(executor, "context"):  # executors that need the admitted identity (sandbox grants)
            executor.context[effect_key] = {
                "tenant_id": claim.tenant_id,
                "task_id": claim.task_id,
                "run_id": claim.run_id,
                "intent_id": intent["id"],
                "lease_epoch": claim.lease_epoch,
            }
        fault("gateway_before_send")
        try:
            res = _run(executor.execute(effect_key, args, permit))
        except ExecutorFailed as e:
            return self._settle(
                claim,
                intent,
                attempt_id,
                tool_call_id,
                tool,
                stream,
                "FAILED",
                {"error": str(e)[:100]},
                effect="FAILED",
            )
        except ExecutorUnknown:
            return self._settle(
                claim,
                intent,
                attempt_id,
                tool_call_id,
                tool,
                stream,
                "UNKNOWN",
                {"note": "MAY_HAVE_SENT; reconcile before any retry"},
                effect="UNKNOWN",
            )
        fault("gateway_after_send")  # external effect happened, ledger still SENDING
        status = "DONE" if spec.risk_class == "read" else "CONFIRMED"
        return self._settle(
            claim,
            intent,
            attempt_id,
            tool_call_id,
            tool,
            stream,
            status,
            res,
            effect="CONFIRMED",
            receipt=res.get("receipt"),
        )

    def _settle(
        self,
        claim: Claim,
        intent: Row,
        attempt_id: str,
        tool_call_id: str,
        tool: str,
        stream: str,
        status: str,
        result: dict[str, Any],
        *,
        effect: str,
        receipt: str | None = None,
    ) -> dict[str, Any]:
        with self.db.tx() as c:
            c.execute(
                "UPDATE agent.tool_intents SET status_code=%s, result_json=%s, updated_at=now(),"
                " record_version=record_version+1 WHERE tenant_id=%s AND id=%s AND status_code='SENDING'",
                (status, Jsonb(result), claim.tenant_id, intent["id"]),
            )
            c.execute(
                "UPDATE agent.tool_attempts SET status_code=%s, ended_at=now() WHERE tenant_id=%s AND id=%s",
                (
                    "CONFIRMED"
                    if status in ("DONE", "CONFIRMED")
                    else ("UNKNOWN" if status == "UNKNOWN" else "FAILED"),
                    claim.tenant_id,
                    attempt_id,
                ),
            )
            c.execute(
                "UPDATE agent.side_effect_records SET outcome_code=%s, external_receipt_ref=%s, updated_at=now()"
                " WHERE tenant_id=%s AND tool_intent_id=%s AND outcome_code='SENDING'",
                (effect, receipt, claim.tenant_id, intent["id"]),
            )
            self._event(
                c,
                claim,
                "tool.effect_unknown" if status == "UNKNOWN" else "tool.effect_recorded",
                intent["id"],
                {"tool": tool, "outcome": status},
            )
        o = outcome(status, result)
        if status != "UNKNOWN":
            try:
                self._record_head(claim, tool_call_id, tool, o, stream)
            except control.ControlError:
                pass  # ledger is authoritative; a newer worker will read the stored outcome
        return o

    # ------------------------------------------------------------------ child result admission
    def record_internal(self, claim: Claim, tool_call_id: str, tool: str, content: str) -> dict[str, Any]:
        """Framework-internal tool results (subagent `task`) enter HEAD only if grounded: the content must be exactly
        the final answer of a sub-agent stream committed in HEAD (by the Model Gateway) after the proposal."""
        if tool not in INTERNAL_TOOLS or len(content) > MAX_CONTENT:
            raise RpcError("DENIED", "not an internal tool")
        with self.db.tx() as c:
            control.check_current(c, claim)
            prop = control.find_proposal(c, claim.tenant_id, claim.run_id, tool_call_id)
            if prop is None or prop[0].get("name") != tool:
                raise RpcError("NOT_IN_HEAD")
            entries = control.head_entries(c, claim.tenant_id, claim.run_id)
            prop_rev = next(
                e["checkpoint_revision"]
                for e in entries
                if e["entry_kind"] == "assistant"
                and any(t.get("id") == tool_call_id for t in e["checkpoint_json"].get("tool_calls", []))
            )
            if tool == "task":
                finals = {
                    e["checkpoint_json"].get("content")
                    for e in entries
                    if e["checkpoint_revision"] > prop_rev
                    and e["stream"].startswith("subagent:")
                    and e["entry_kind"] == "assistant"
                    and not e["checkpoint_json"].get("tool_calls")
                }
                admitted = content in finals
            else:
                admitted = _grounded_read(content, entries, prop[0])
            o = outcome(
                "DONE" if admitted else "DENIED",
                {"childResult": content} if admitted else {"error": "child result not grounded in HEAD"},
            )
            entry_content = content if admitted else content_of(o)
            control.append_head(
                c,
                claim,
                "tool",
                {"kind": "tool", "tool_call_id": tool_call_id, "tool_name": tool, "content": entry_content},
                writer="tool_gateway",
                stream=prop[1],
                tool_call_id=tool_call_id,
                require_current_goal=False,
            )
        return {**o, "content": entry_content}

    # ------------------------------------------------------------------ reconciliation (operator)
    async def reconcile(self, tenant_id: str, intent_id: str) -> dict[str, Any]:
        initial = self.db.one("SELECT * FROM agent.tool_intents WHERE tenant_id=%s AND id=%s", (tenant_id, intent_id))
        if initial is None:
            raise RpcError("NOT_FOUND")
        if initial["status_code"] not in ("UNKNOWN", "SENDING"):
            return {"status": initial["status_code"]}
        executor = self.executors.get(TOOLS[initial["tool_key"]].executor)
        permit = None
        dest = executor.destination() if executor else None
        if dest is not None and self.sentinel is not None:
            permit = await asyncio.to_thread(self.sentinel.authorize_reconcile, tenant_id, intent_id, dest)
        answer = await executor.query(f"{tenant_id}:{intent_id}", permit) if executor else None
        # An unproven negative or malformed positive must never clear the
        # Effect Ledger. Only an authenticated, receipt-backed positive is
        # supported; strong negative proofs require a separate verified contract.
        if not isinstance(answer, dict) or answer.get("sent") is not True or not valid_receipt(answer.get("receipt")):
            return {"status": "UNKNOWN"}
        status, effect = "CONFIRMED", "RECONCILED_SENT"
        result = {"receipt": answer["receipt"]}
        # Lock order is task -> run -> intent, matching claim/segment settlement.
        # Do not keep these DB locks across the external receipt query.
        with self.db.tx() as c:
            pointer = c.execute(
                "SELECT task_id, run_id FROM agent.tool_intents WHERE tenant_id=%s AND id=%s",
                (tenant_id, intent_id),
            ).fetchone()
            if pointer is None:
                raise RpcError("NOT_FOUND")
            task = c.execute(
                "SELECT id, owner_user_id, workspace_id, goal_revision, status_code, active_run_id"
                " FROM agent.tasks WHERE tenant_id=%s AND id=%s FOR UPDATE",
                (tenant_id, pointer["task_id"]),
            ).fetchone()
            run = c.execute(
                "SELECT id, status_code, lease_epoch, restore_incarnation, cancel_requested, pause_requested"
                " FROM agent.runs WHERE tenant_id=%s AND id=%s FOR UPDATE",
                (tenant_id, pointer["run_id"]),
            ).fetchone()
            intent = c.execute(
                "SELECT * FROM agent.tool_intents WHERE tenant_id=%s AND id=%s FOR UPDATE",
                (tenant_id, intent_id),
            ).fetchone()
            if task is None or run is None or intent is None:
                raise RpcError("NOT_FOUND")
            if intent["status_code"] not in ("UNKNOWN", "SENDING"):
                return {"status": intent["status_code"]}  # concurrent operator already committed
            # A reconciled result is admissible to HEAD only when backed by the
            # exact frozen assistant proposal, intent hash, and stream.
            proposal = control.find_proposal(c, tenant_id, intent["run_id"], intent["invocation_key"])
            if (
                proposal is None
                or proposal[0].get("name") != intent["tool_key"]
                or proposal[1] != intent["stream"]
                or sha256_hex({"tool": intent["tool_key"], "args": proposal[0].get("args")}) != intent["request_sha256"]
            ):
                raise RpcError("NOT_IN_HEAD", "reconciled intent is not grounded in a frozen tool proposal")
            c.execute(
                "UPDATE agent.tool_intents SET status_code=%s, result_json=%s,"
                " record_version=record_version+1, updated_at=now()"
                " WHERE tenant_id=%s AND id=%s",
                (status, Jsonb(result), tenant_id, intent_id),
            )
            c.execute(
                "UPDATE agent.side_effect_records SET outcome_code=%s, external_receipt_ref=%s,"
                " last_checked_at=now(), updated_at=now() WHERE tenant_id=%s AND tool_intent_id=%s",
                (effect, answer.get("receipt"), tenant_id, intent_id),
            )
            c.execute(
                "UPDATE agent.tool_attempts SET status_code=%s, ended_at=now()"
                " WHERE tenant_id=%s AND tool_intent_id=%s AND status_code IN ('SENDING','UNKNOWN')",
                ("CONFIRMED" if status == "CONFIRMED" else "FAILED", tenant_id, intent_id),
            )
            # The ledger remains the source of truth. Append the receipt-backed
            # tool result through HEAD's idempotent CAS, not by calling execute().
            if run["status_code"] not in control.TERMINAL:
                claim = Claim(
                    tenant_id,
                    task["owner_user_id"],
                    task["id"],
                    run["id"],
                    run["lease_epoch"],
                    intent["goal_revision"],
                    "tool_gateway:reconcile",
                    run["restore_incarnation"],
                )
                control.append_head(
                    c,
                    claim,
                    "tool",
                    {
                        "kind": "tool",
                        "tool_call_id": intent["invocation_key"],
                        "tool_name": intent["tool_key"],
                        "content": content_of(outcome(status, result)),
                    },
                    writer="tool_gateway:reconcile",
                    stream=intent["stream"],
                    tool_call_id=intent["invocation_key"],
                    require_current_goal=False,
                )
            append_event(
                c,
                tenant_id=tenant_id,
                aggregate_type="tool",
                aggregate_id=intent_id,
                kind="tool.effect_recorded",
                payload={"tool": intent["tool_key"], "outcome": status, "reconciled": True},
                actor=SERVICE,
                owner_user_id=task["owner_user_id"],
                task_id=task["id"],
                run_id=run["id"],
            )
            pending = control._has_unresolved_effects(c, tenant_id, run["id"])
            # Only the final resolution may release WAITING_RECONCILIATION.
            # Never make a cancelled/revoked run claimable again.
            if not pending and run["status_code"] in ("WAITING_RECONCILIATION", "CANCELLING"):
                revoked = not control.scope_active(c, tenant_id, str(task["owner_user_id"]), str(task["workspace_id"]))
                if run["cancel_requested"] or revoked or run["status_code"] == "CANCELLING":
                    target = "CANCELLED"
                elif run["pause_requested"]:
                    target = "PAUSED"
                else:
                    target = "CREATED"
                c.execute(
                    "UPDATE agent.runs SET status_code=%s, lease_epoch=lease_epoch+1,"
                    " lease_expires_at=now()-interval '1 millisecond', framework_ref=NULL,"
                    " error_code=NULL, ended_at=CASE WHEN %s THEN now() ELSE ended_at END,"
                    " record_version=record_version+1, updated_at=now()"
                    " WHERE tenant_id=%s AND id=%s",
                    (target, target == "CANCELLED", tenant_id, run["id"]),
                )
                task_state = "RUNNING" if target == "CREATED" else target
                if task["status_code"] not in control.TERMINAL:
                    c.execute(
                        "UPDATE agent.tasks SET status_code=%s, record_version=record_version+1,"
                        " updated_at=now() WHERE tenant_id=%s AND id=%s",
                        (task_state, tenant_id, task["id"]),
                    )
                ev = append_event(
                    c,
                    tenant_id=tenant_id,
                    aggregate_type="run",
                    aggregate_id=run["id"],
                    kind="run.state_changed",
                    payload={"from": run["status_code"], "to": target, "cause": "effect_reconciled"},
                    actor=SERVICE,
                    owner_user_id=task["owner_user_id"],
                    task_id=task["id"],
                    run_id=run["id"],
                )
                if target == "CREATED" and task["active_run_id"] == run["id"]:
                    enqueue_run_job(
                        c,
                        tenant_id=tenant_id,
                        task_id=task["id"],
                        run_id=run["id"],
                        kind="run.resume",
                        goal_revision=task["goal_revision"],
                        dedupe_key=f"run:{run['id']}:reconciled:{intent_id}",
                        event_id=ev,
                    )
        return {"status": status}


_LINENO = re.compile(r"^\s*\d+\t", re.MULTILINE)
# DeepAgents 0.7.34: read_file returns a status header followed by raw, UNNUMBERED source lines.
# Never accept unverified metadata, trailing notices, or a fake header just because a few words match a skill.
_SKILL_READ_HEADER = re.compile(
    r"\A@@ lines ([1-9][0-9]*)-([1-9][0-9]*) of ([1-9][0-9]*)(?: \| next offset ([1-9][0-9]*))? @@\n"
)


def _grounded_packaged_skill_read(content: str, args: dict[str, Any]) -> bool:
    """Verify the complete rendered page against the precise packaged skill and committed read_file arguments.

    DeepAgents wraps the verbatim page with `@@ lines A-B of T | next offset N @@`.
    The header is not itself evidence; it and every body character must match
    a read-only file at the proposed path. Deliberately reject mid-line truncation
    and diagnostic pages rather than launder partial/unverified bytes into HEAD.
    """
    from aiagent_domain.tools import SKILLS_DIR

    file_path, offset, limit = args.get("file_path"), args.get("offset", 0), args.get("limit", 100)
    if not isinstance(file_path, str) or not file_path.startswith("/skills/"):
        return False
    if type(offset) is not int or type(limit) is not int or offset < 0 or limit <= 0:
        return False
    posix = PurePosixPath(file_path)
    if str(posix) != file_path or ".." in file_path.split("/") or "." in file_path.split("/"):
        return False
    parts = posix.parts
    if len(parts) < 4 or parts[:2] != ("/", "skills") or not parts[-1].endswith((".md", ".txt")):
        return False
    candidate = SKILLS_DIR.joinpath(*parts[2:])
    # The runtime reads immutable packaged skills, not symlink destinations.
    if not candidate.is_file() or any(p.is_symlink() for p in (candidate, *candidate.parents)):
        return False
    header = _SKILL_READ_HEADER.match(content)
    if header is None:
        return False
    start, end, total = (int(header.group(i)) for i in (1, 2, 3))
    next_offset = int(header.group(4)) if header.group(4) is not None else None

    # FilesystemBackend.read uses splitlines(keepends=True), so a final LF does
    # not add a phantom line. _prepare_read_window preserves empty source rows.
    source_lines = candidate.read_text(encoding="utf-8").splitlines(keepends=True)
    total_lines = len(source_lines)
    expected_end = min(offset + limit, total_lines)
    if (start, end, total) != (offset + 1, expected_end, total_lines) or start > end:
        return False
    if next_offset != (expected_end if expected_end < total_lines else None):
        return False
    raw_page = "".join(source_lines[offset:expected_end]).replace("\r\n", "\n").replace("\r", "\n")
    rows = raw_page.split("\n")
    if rows and rows[-1] == "":
        rows.pop()
    # DeepAgents pads lost final blank rows when building its source block.
    rows.extend([""] * max(0, end - start + 1 - len(rows)))
    expected_body = "\n".join(rows)
    return content[header.end() :] == expected_body


def _grounded_read(content: str, entries: list[Row], proposal: dict[str, Any]) -> bool:
    """Admit skill pages only by exact file/offset/limit; preserve HEAD-only offload reads."""
    args = proposal.get("args")
    if not isinstance(args, dict) or not isinstance(args.get("file_path"), str):
        return False
    if args["file_path"].startswith("/skills"):
        return _grounded_packaged_skill_read(content, args)

    # Non-skill offloaded content can only repeat material already committed to
    # this run's HEAD. A fabricated skill excerpt or metadata is not sufficient.
    body = _LINENO.sub("", content).strip()
    if not body:
        return True
    corpus = "\n".join(str(e["checkpoint_json"].get("content", "")) for e in entries)
    return body in corpus


def _run(coro: Any) -> Any:
    """Executors are async; _dispatch runs in a worker thread (blocking DB code), so drive the coroutine here."""
    return asyncio.run(coro)
