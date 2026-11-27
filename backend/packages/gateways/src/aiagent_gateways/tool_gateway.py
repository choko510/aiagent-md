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
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any

from aiagent_core import config, control
from aiagent_core.control import Claim
from aiagent_core.db import Database, Row
from aiagent_core.events import append_event, enqueue_run_job
from aiagent_core.step4_gate import is_reserved_tool
from aiagent_domain import canonical_json, is_uuid7, sha256_hex, uuid7
from aiagent_domain.rpcwire import RpcError
from aiagent_domain.tools import INTERNAL_TOOLS, PROFILE_TOOLS, TOOLS
from jsonschema import Draft202012Validator
from psycopg.types.json import Jsonb

from .executors import Destination, Executor, ExecutorFailed, ExecutorUnknown, valid_receipt

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
        development_git_enabled: bool = False,
    ) -> None:
        if development_git_enabled and config.profile() == "production":
            raise ValueError("Git commit requires verified production isolation")
        self.db, self.executors, self.sentinel = db, executors, sentinel
        self.ttl, self.max_tool_calls = approval_ttl_s, max_tool_calls
        self._development_git_enabled = development_git_enabled

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
        if tool == "git.commit":
            return await asyncio.to_thread(self._deny, claim, tool_call_id, tool, "DEVELOPMENT_ONLY", stream)
        # STEP 4 names must remain fail-closed even if a registry/profile changes
        # before the PostgreSQL, Run/Goal/fence, effect-ledger and Sentinel adapters exist.
        if is_reserved_tool(tool):
            return await asyncio.to_thread(self._deny, claim, tool_call_id, tool, "STEP4_NOT_INTEGRATED", stream)
        spec = TOOLS.get(tool)
        if spec is None:
            return self._deny(claim, tool_call_id, tool, "unknown tool", stream)
        if not Draft202012Validator(spec.parameters).is_valid(args):
            return await asyncio.to_thread(self._deny, claim, tool_call_id, tool, "schema", stream)
        return await asyncio.to_thread(self._submit_sync, claim, tool_call_id, tool, args, stream)

    async def submit_development_git(self, claim: Claim, tool_call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Internal Development endpoint; caller authentication is enforced by the RPC server."""
        if not self._development_git_enabled or config.profile() == "production":
            raise RpcError("DENIED", "development Git is disabled")
        with self.db.conn() as c:
            control.check_current(c, claim)
            rows = c.execute(
                "SELECT tc,stream,writer_ref FROM agent.run_checkpoints, "
                "jsonb_array_elements(checkpoint_json->'tool_calls') tc WHERE tenant_id=%s AND run_id=%s "
                "AND entry_kind='assistant' AND tc->>'id'=%s",
                (claim.tenant_id, claim.run_id, tool_call_id),
            ).fetchall()
            hit = control.find_tool(c, claim.tenant_id, claim.run_id, tool_call_id)
        if (
            len(rows) != 1
            or rows[0]["writer_ref"] != "development_git_adapter"
            or rows[0]["tc"].get("name") != "git.commit"
            or rows[0]["tc"].get("args") != args
        ):
            raise RpcError("NOT_IN_HEAD", "development proposal mismatch")
        if hit is not None:
            import json

            data = json.loads(hit["content"])
            return outcome(data.pop("status"), data, replayed=True)
        if not Draft202012Validator(TOOLS["git.commit"].parameters).is_valid(args):
            return await asyncio.to_thread(self._deny, claim, tool_call_id, "git.commit", "schema", rows[0]["stream"])
        return await asyncio.to_thread(
            self._submit_sync, claim, tool_call_id, "git.commit", args, rows[0]["stream"], development_git=True
        )

    def _submit_sync(
        self,
        claim: Claim,
        tool_call_id: str,
        tool: str,
        args: dict[str, Any],
        stream: str,
        *,
        development_git: bool = False,
    ) -> dict[str, Any]:
        spec = TOOLS[tool]
        executor = self.executors.get(spec.executor)
        bind_fn = getattr(executor, "bind_request", None) if executor is not None else None
        bound_form: dict[str, Any] | None = None
        bind_error: str | None = None
        if development_git and (tool != "git.commit" or not callable(bind_fn)):
            bind_error = "Git authority unavailable"
        req_sha = sha256_hex({"tool": tool, "args": args})
        if callable(bind_fn):
            needs_bind = False
            with self.db.tx() as peek_c:
                try:
                    control.check_current(peek_c, claim)
                except control.ControlError:
                    pre_run_ok = False
                else:
                    pre_run_ok = True
                pre_task = peek_c.execute(
                    "SELECT profile_code FROM agent.tasks WHERE tenant_id=%s AND id=%s",
                    (claim.tenant_id, claim.task_id),
                ).fetchone()
                existing_peek = peek_c.execute(
                    "SELECT i.status_code, i.tool_key, i.safe_arguments_json, i.request_sha256, a.decision_code"
                    " FROM agent.tool_intents i"
                    " LEFT JOIN agent.approval_records a ON a.tenant_id=i.tenant_id AND a.tool_intent_id=i.id"
                    " WHERE i.tenant_id=%s AND i.run_id=%s AND i.invocation_key=%s",
                    (claim.tenant_id, claim.run_id, tool_call_id),
                ).fetchone()
            if existing_peek is not None and (
                existing_peek["status_code"] in ("SENDING", "UNKNOWN")
                or existing_peek["status_code"] in TERMINAL_INTENT
                or (
                    existing_peek["status_code"] == "PENDING_APPROVAL"
                    and existing_peek["decision_code"] in ("REJECTED", "REVOKED", "EXPIRED")
                )
            ):
                if existing_peek["tool_key"] != tool or canonical_json(
                    dict(existing_peek["safe_arguments_json"] or {})
                ) != canonical_json(args):
                    raise RpcError("INTENT_CONFLICT")
                req_sha = str(existing_peek["request_sha256"])
            elif (
                pre_run_ok
                and pre_task is not None
                and (
                    tool in PROFILE_TOOLS.get(pre_task["profile_code"], ())
                    or (development_git and tool == "git.commit")
                )
            ):
                needs_bind = True
            if needs_bind:
                try:
                    bound_form = bind_fn(tool, args, claim=claim)
                except ExecutorFailed as exc:
                    bind_error = str(exc)[:100]
                if bound_form is not None:
                    req_sha = sha256_hex({"tool": tool, "args": args, "bound": bound_form})
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
                if bind_error is not None and intent["status_code"] == "PENDING_APPROVAL":
                    raise RpcError("INTENT_CONFLICT")
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
                if tool not in PROFILE_TOOLS.get(task["profile_code"], ()) and not (
                    development_git and tool == "git.commit"
                ):
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
                if bind_error is not None:
                    return self._finish_denied(
                        c,
                        claim,
                        tool_call_id,
                        tool,
                        args,
                        req_sha,
                        spec,
                        "FAILED",
                        stream,
                        error=bind_error,
                    )
                # R3-01: Enforce Development Cycle CODE_READY, Commit SHA, RunFence & RepositoryBinding admission
                # inside ToolGateway for ALL `dev_github_pr` callers (runtime tool.submit AND api tool.dev_pr_dispatch).
                if tool == "dev_github_pr":
                    dev_exec = self.executors.get(spec.executor)
                    verify_cycle = getattr(dev_exec, "verify_cycle_admission", None)
                    if not callable(verify_cycle):
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
                            error="DEV_PR_EXECUTOR_UNAVAILABLE",
                        )
                    try:
                        verify_cycle(claim, args)
                    except ExecutorFailed as exc:
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
                            error=str(exc)[:100],
                        )
                n = c.execute(
                    "SELECT count(*) AS n FROM agent.tool_intents WHERE tenant_id=%s AND run_id=%s",
                    (claim.tenant_id, claim.run_id),
                ).fetchone()
                run_tool_cap = (
                    int(run["max_tool_calls"]) if run.get("max_tool_calls") is not None else self.max_tool_calls
                )
                effective_limit = min(self.max_tool_calls, run_tool_cap)
                if n is not None and n["n"] >= effective_limit:
                    return self._finish_denied(
                        c,
                        claim,
                        tool_call_id,
                        tool,
                        args,
                        req_sha,
                        spec,
                        "BUDGET_EXHAUSTED",
                        stream,
                        error="tool_budget_exceeded",
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
                    if bound_form is not None:
                        summary = (
                            f"{tool} {bound_form['method']} {bound_form['action']}"
                            f" sha={str(bound_form['payloadDigest'])[:12]}"
                        )[:200]
                    else:
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
            return self._final_admission(
                claim, intent, tool_call_id, tool, args, req_sha, stream, bound_form=bound_form
            )
        return self._dispatch(claim, intent, tool_call_id, tool, args, stream, fence=1, bound_form=bound_form)

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
        self,
        claim: Claim,
        intent: Row,
        tool_call_id: str,
        tool: str,
        args: dict[str, Any],
        req_sha: str,
        stream: str,
        *,
        bound_form: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.db.tx() as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (claim.run_id,))
            run = c.execute(
                "SELECT r.status_code, r.lease_epoch, r.lease_owner_ref,"
                " coalesce(r.lease_expires_at > clock_timestamp(), false) AS lease_valid,"
                " r.cancel_requested, r.pause_requested, r.restore_incarnation, t.goal_revision,"
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
                or run["lease_owner_ref"] != claim.worker_ref
                or not run["lease_valid"]
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
            elif tool == "dev_github_pr":
                dev_exec = self.executors.get(TOOLS[tool].executor)
                verify_cycle = getattr(dev_exec, "verify_cycle_admission", None)
                if not callable(verify_cycle):
                    final = "DENIED"
                else:
                    try:
                        verify_cycle(claim, args)
                    except ExecutorFailed:
                        final = "DENIED"
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
        return self._dispatch(claim, intent, tool_call_id, tool, args, stream, fence=1, bound_form=bound_form)

    # ------------------------------------------------------------------ dispatch
    def _dispatch(
        self,
        claim: Claim,
        intent: Row,
        tool_call_id: str,
        tool: str,
        args: dict[str, Any],
        stream: str,
        *,
        fence: int,
        bound_form: dict[str, Any] | None = None,
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
        try:
            dest_tool_fn = getattr(executor, "destination_for_tool", None)
            dest_fn = getattr(executor, "destination_for", None)
            if callable(dest_tool_fn):
                try:
                    dest = dest_tool_fn(tool, args, bound_form=bound_form, claim=claim, intent=intent)
                except TypeError:
                    try:
                        dest = dest_tool_fn(tool, args, bound_form=bound_form, claim=claim)
                    except TypeError:
                        try:
                            dest = dest_tool_fn(tool, args, bound_form=bound_form)
                        except TypeError:
                            dest = dest_tool_fn(tool, args)
            else:
                dest = dest_fn(args) if callable(dest_fn) else executor.destination()
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
        if hasattr(executor, "context"):  # executors that need the admitted identity (sandbox grants, browser fence)

            async def _issue_hop_permit(hop_dest: Destination) -> str:
                if self.sentinel is None:
                    raise ExecutorFailed("egress requires Sentinel")
                try:
                    return await asyncio.to_thread(self.sentinel.authorize, claim, intent["id"], hop_dest)
                except RpcError as e:
                    raise ExecutorFailed(f"sentinel:{e.code}") from e

            async def _issue_reconcile_permit(hop_dest: Destination) -> str:
                if self.sentinel is None:
                    raise ExecutorFailed("egress requires Sentinel")
                try:
                    return await asyncio.to_thread(
                        self.sentinel.authorize_reconcile, claim.tenant_id, intent["id"], hop_dest
                    )
                except RpcError as e:
                    raise ExecutorFailed(f"sentinel:{e.code}") from e

            executor.context[effect_key] = {
                "tool": tool,
                "tenant_id": claim.tenant_id,
                "owner_user_id": claim.owner_user_id,
                "task_id": claim.task_id,
                "run_id": claim.run_id,
                "intent_id": intent["id"],
                "request_sha256": str(intent["request_sha256"]),
                "attempt_id": str(attempt_id),
                "goal_revision": claim.goal_revision,
                "lease_epoch": claim.lease_epoch,
                "worker_ref": claim.worker_ref,
                "restore_incarnation": claim.restore_incarnation,
                "permit_issuer": _issue_hop_permit,
                "reconcile_permit_issuer": _issue_reconcile_permit,
                "bound_form": bound_form,
            }
        if tool == "git.commit":
            from .git_operation import admit_git_operation

            if (
                not self._development_git_enabled
                or config.profile() == "production"
                or not hasattr(executor, "context")
            ):
                return self._settle(
                    claim,
                    intent,
                    attempt_id,
                    tool_call_id,
                    tool,
                    stream,
                    "FAILED",
                    {"error": "Git authority unavailable"},
                    effect="FAILED",
                )
            try:
                with self.db.tx(tenant=claim.tenant_id) as c:
                    workspace_id = str(control.check_current(c, claim)["workspace_id"])
                import time

                operation_id = admit_git_operation(
                    self.db,
                    claim,
                    workspace_id=workspace_id,
                    attempt_id=attempt_id,
                    binding_id=args["bindingId"],
                    patch_id=args["patchId"],
                    deadline=time.monotonic() + 10,
                )
                executor.context[effect_key]["operation_id"] = operation_id
            except ExecutorFailed:
                return self._settle(
                    claim,
                    intent,
                    attempt_id,
                    tool_call_id,
                    tool,
                    stream,
                    "FAILED",
                    {"error": "Git operation denied"},
                    effect="FAILED",
                )
            except (ExecutorUnknown, control.ControlError):
                return self._settle(
                    claim,
                    intent,
                    attempt_id,
                    tool_call_id,
                    tool,
                    stream,
                    "UNKNOWN",
                    {"note": "Git operation requires reconciliation"},
                    effect="UNKNOWN",
                )
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
                "UPDATE agent.tool_intents SET status_code=%s,"
                " result_json=CASE WHEN %s = 'UNKNOWN' THEN coalesce(result_json, '{}'::jsonb) || %s::jsonb ELSE %s::jsonb END,"
                " updated_at=now(), record_version=record_version+1"
                " WHERE tenant_id=%s AND id=%s AND status_code='SENDING'",
                (status, status, Jsonb(result), Jsonb(result), claim.tenant_id, intent["id"]),
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

    async def dispatch_approved_dev_pr(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        workspace_id: str,
        intent_id: str,
        expected_args: dict[str, Any],
    ) -> dict[str, Any]:
        """Dispatch an already-proposed `dev_github_pr` intent through canonical ToolGateway.submit (R2-04/R2-09)."""
        if not is_uuid7(intent_id):
            raise RpcError("BAD_REQUEST")
        with self.db.conn() as c:
            row = c.execute(
                "SELECT i.task_id, i.run_id, i.tool_key, i.invocation_key, i.request_sha256, i.safe_arguments_json,"
                " i.goal_revision AS intent_goal_rev, r.lease_epoch, r.lease_owner_ref, r.restore_incarnation,"
                " t.owner_user_id, t.workspace_id, t.goal_revision"
                " FROM agent.tool_intents i"
                " JOIN agent.runs r ON r.tenant_id=i.tenant_id AND r.id=i.run_id"
                " JOIN agent.tasks t ON t.tenant_id=i.tenant_id AND t.id=i.task_id"
                " WHERE i.tenant_id=%s AND i.id=%s",
                (tenant_id, intent_id),
            ).fetchone()
        if (
            row is None
            or str(row["tool_key"]) != "dev_github_pr"
            or str(row["owner_user_id"]) != owner_user_id
            or str(row["workspace_id"]) != workspace_id
            or not row["lease_owner_ref"]
        ):
            raise RpcError("DENIED", "dev_github_pr intent out of scope or inactive")
        expected_sha = sha256_hex({"tool": "dev_github_pr", "args": expected_args})
        if dict(row["safe_arguments_json"] or {}) != expected_args or str(row["request_sha256"]) != expected_sha:
            raise RpcError("INTENT_CONFLICT", "dev_github_pr arguments mismatch approved tool_intent")
        claim = Claim(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            task_id=str(row["task_id"]),
            run_id=str(row["run_id"]),
            lease_epoch=int(row["lease_epoch"]),
            goal_revision=int(row["goal_revision"]),
            worker_ref=str(row["lease_owner_ref"]),
            restore_incarnation=int(row["restore_incarnation"]),
        )
        return await self.submit(claim, str(row["invocation_key"]), "dev_github_pr", expected_args)

    async def dispatch_preview_debug(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        cycle_id: str,
        lease_id: str,
        preview_token: str,
        target_port: int,
        task_id: str,
        run_id: str,
        path: str = "/",
        revoke_after: bool = True,
    ) -> dict[str, Any]:
        """Authorize & dispatch Browser Preview debug via dedicated PreviewCapability to Sandbox Cell (R3-02/R3-03/R3-05)."""
        from aiagent_development.browser_debugger import run_sandbox_cell_preview_debug_async
        from aiagent_development.contracts import ScopeDenied
        from aiagent_development.pg_store import PreviewBroker

        if not path.startswith("/") or ".." in path or "\\" in path or len(path) > 256:
            raise RpcError("BAD_REQUEST", "invalid preview path")
        broker = PreviewBroker(self.db)
        try:
            lease = await asyncio.to_thread(
                broker.verify_lease_details,
                tenant_id,
                owner_user_id,
                lease_id,
                preview_token,
                target_port,
                expected_cycle_id=cycle_id,
            )
        except ScopeDenied as exc:
            raise RpcError("DENIED", str(exc)) from None
        # R4-04: Derive task_id and run_id strictly from the DB-verified lease/cycle (NEVER trust caller-supplied IDs).
        if (task_id and task_id != lease.task_id) or (run_id and run_id != lease.run_id):
            raise RpcError("DENIED", "PREVIEW_TASK_OR_RUN_MISMATCH")

        target_url = f"http://127.0.0.1:{target_port}{path}"

        def _lease_alive() -> bool:
            try:
                live = broker.verify_lease_details(
                    tenant_id,
                    owner_user_id,
                    lease_id,
                    preview_token,
                    target_port,
                    expected_cycle_id=cycle_id,
                )
                return (
                    live.task_id == lease.task_id
                    and live.run_id == lease.run_id
                    and live.goal_revision == lease.goal_revision
                )
            except Exception:
                return False

        try:
            report = await run_sandbox_cell_preview_debug_async(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                task_id=lease.task_id,
                run_id=lease.run_id,
                lease=lease,
                target_url=target_url,
                lease_alive_check=_lease_alive,
            )
        except ScopeDenied as exc:
            raise RpcError("DENIED", str(exc)) from None
        except RuntimeError as exc:
            raise RpcError("FAILED", str(exc)) from None
        finally:
            if revoke_after:
                await asyncio.to_thread(broker.revoke_lease, tenant_id, owner_user_id, lease_id)

        return {
            "commitSha": report.commit_sha,
            "sourceUrlRedacted": report.source_url_redacted,
            "consoleErrors": list(report.console_errors),
            "networkErrors": list(report.network_errors),
            "layoutIssues": list(report.layout_issues),
            "status": report.status,
        }

    # ------------------------------------------------------------------ reconciliation (operator / api)
    async def reconcile(self, tenant_id: str, intent_id: str, *, owner_user_id: str | None = None) -> dict[str, Any]:
        initial = self.db.one(
            "SELECT i.*, t.owner_user_id, se.outcome_code AS side_effect_outcome"
            " FROM agent.tool_intents i"
            " JOIN agent.tasks t ON t.tenant_id=i.tenant_id AND t.id=i.task_id"
            " LEFT JOIN agent.side_effect_records se ON se.tenant_id=i.tenant_id AND se.tool_intent_id=i.id"
            " WHERE i.tenant_id=%s AND i.id=%s",
            (tenant_id, intent_id),
        )
        if initial is None:
            raise RpcError("NOT_FOUND")
        if owner_user_id is not None and str(initial["owner_user_id"]) != owner_user_id:
            raise RpcError("NOT_FOUND")
        if initial["status_code"] not in ("UNKNOWN", "SENDING"):
            return {"status": initial["status_code"]}
        spec = TOOLS[initial["tool_key"]]
        if spec.risk_class != "read" and initial.get("side_effect_outcome") not in ("SENDING", "UNKNOWN"):
            return {"status": initial["status_code"]}
        executor = self.executors.get(spec.executor)
        permit = None
        dest_reconcile_fn = getattr(executor, "destination_for_reconcile", None) if executor else None
        try:
            dest = (
                dest_reconcile_fn(tenant_id, initial)
                if callable(dest_reconcile_fn)
                else (executor.destination() if executor else None)
            )
        except ExecutorFailed:
            return {"status": "UNKNOWN"}
        if dest is not None and self.sentinel is not None:
            permit = await asyncio.to_thread(self.sentinel.authorize_reconcile, tenant_id, intent_id, dest)
        reconcile_key = f"{tenant_id}:{intent_id}"
        if executor is not None and hasattr(executor, "context") and self.sentinel is not None:

            async def _reconcile_page_permit(hop_dest: Destination) -> str:
                assert self.sentinel is not None
                return await asyncio.to_thread(self.sentinel.authorize_reconcile, tenant_id, intent_id, hop_dest)

            executor.context[reconcile_key] = {"reconcile_permit_issuer": _reconcile_page_permit}
        try:
            answer = await executor.query(reconcile_key, permit) if executor else None
        finally:
            if executor is not None and hasattr(executor, "context"):
                executor.context.pop(reconcile_key, None)
        # An unproven negative or malformed positive must never clear the
        # Effect Ledger. Only an authenticated, receipt-backed positive is
        # supported; strong negative proofs require a separate verified contract.
        if not isinstance(answer, dict) or answer.get("sent") is not True or not valid_receipt(answer.get("receipt")):
            return {"status": "UNKNOWN"}
        status, effect = "CONFIRMED", "RECONCILED_SENT"
        result: dict[str, Any] = {"receipt": answer["receipt"]}
        # Command reconciliation may restore only the redacted fields derived
        # from its signature-verified canonical receipt. Other executors keep
        # the original receipt-only reconciliation contract.
        from .command_dispatch import AuthorizedCommandExecutor
        from .dev_github_executor import DevGitHubPrExecutor

        if isinstance(executor, DevGitHubPrExecutor):
            pr_res = answer.get("prResult")
            if (
                not isinstance(pr_res, dict)
                or not isinstance(pr_res.get("prNumber"), int)
                or pr_res["prNumber"] < 1
                or not isinstance(pr_res.get("prUrl"), str)
                or not pr_res["prUrl"]
            ):
                return {"status": "UNKNOWN"}
            result.update(pr_res)
        elif isinstance(executor, AuthorizedCommandExecutor):
            command = answer.get("commandResult")
            if not isinstance(command, dict) or set(command) != {
                "toolAttemptId",
                "executionId",
                "receiptId",
                "bodySha256",
                "kind",
                "testId",
            }:
                return {"status": "UNKNOWN"}
            if (
                not all(
                    isinstance(command.get(k), str) and command[k]
                    for k in ("toolAttemptId", "executionId", "receiptId", "bodySha256", "kind")
                )
                or command["kind"] not in ("build", "static-check", "test")
                or (command["kind"] == "test") != (isinstance(command["testId"], str) and bool(command["testId"]))
                or (command["kind"] != "test" and command["testId"] is not None)
                or any(not is_uuid7(command[k]) for k in ("toolAttemptId", "executionId", "receiptId"))
                or re.fullmatch(r"[0-9a-f]{64}", command["bodySha256"]) is None
                or answer["receipt"] != "command:" + command["receiptId"]
            ):
                return {"status": "UNKNOWN"}
            result.update(command)
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
                control.append_reconciled_head(
                    c,
                    claim,
                    stream=intent["stream"],
                    intent_id=intent_id,
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


# DeepAgents 0.7.34 text read_file uses an unnumbered verbatim page below this header.
# Reject notices, midline-truncated content, bogus pagination, and ungrounded state files.
_READ_HEADER = re.compile(
    r"\A@@ lines ([1-9][0-9]*)-([1-9][0-9]*) of ([1-9][0-9]*)(?: \| next offset ([1-9][0-9]*))? @@\n"
)
_HISTORY_PATH = re.compile(
    r"/conversation_history/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.md\Z"
)


def _exact_read_page(content: str, args: dict[str, Any], source: str) -> bool:
    """Compare the full DeepAgents text page against a known HEAD or packaged source.

    Neither the header nor the proposed path grants trust; every page field and
    byte must agree with the source after DeepAgents' newline normalization.
    """
    offset, limit = args.get("offset", 0), args.get("limit", 100)
    if type(offset) is not int or type(limit) is not int or offset < 0 or limit <= 0:
        return False
    header = _READ_HEADER.match(content)
    if header is None:
        return False
    start, end, total = (int(header.group(i)) for i in (1, 2, 3))
    next_offset = int(header.group(4)) if header.group(4) is not None else None
    lines = source.splitlines(keepends=True)
    end_expected = min(offset + limit, len(lines))
    if (start, end, total) != (offset + 1, end_expected, len(lines)) or start > end:
        return False
    if next_offset != (end_expected if end_expected < len(lines) else None):
        return False
    raw_page = "".join(lines[offset:end_expected]).replace("\r\n", "\n").replace("\r", "\n")
    rows = raw_page.split("\n")
    if rows and rows[-1] == "":
        rows.pop()
    # The pinned formatter pads blank final source rows to the advertised range.
    rows.extend([""] * max(0, end - start + 1 - len(rows)))
    return content[header.end() :] == "\n".join(rows)


def _grounded_packaged_skill_read(content: str, args: dict[str, Any]) -> bool:
    """Verify the exact packaged file and rendered page; never trust path alone."""
    from aiagent_domain.tools import SKILLS_DIR

    file_path = args.get("file_path")
    if not isinstance(file_path, str) or not file_path.startswith("/skills/"):
        return False
    posix = PurePosixPath(file_path)
    if str(posix) != file_path or ".." in file_path.split("/") or "." in file_path.split("/"):
        return False
    parts = posix.parts
    if len(parts) < 4 or parts[:2] != ("/", "skills") or not parts[-1].endswith((".md", ".txt")):
        return False
    if any(part.startswith(".") for part in parts[2:]):
        return False
    candidate = SKILLS_DIR.joinpath(*parts[2:])
    if not candidate.is_file() or any(p.is_symlink() for p in (candidate, *candidate.parents)):
        return False
    return _exact_read_page(content, args, candidate.read_text(encoding="utf-8"))


def _sanitized_tool_id(tool_call_id: str) -> str:
    """Mirror pinned DeepAgents 0.7.34 sanitize_tool_call_id (128-byte path component)."""
    sanitized = tool_call_id.replace(".", "_").replace("/", "_").replace("\\", "_")
    if len(sanitized.encode("utf-8")) > 128:
        return "call-" + sha256(tool_call_id.encode("utf-8")).hexdigest()
    return sanitized


def _grounded_offload_read(content: str, entries: list[Row], args: dict[str, Any]) -> bool:
    """Admit only exact pages derived from this Run's original HEAD tool/user messages.

    Large tool result paths encode the committed tool_call_id. Conversation history
    uses a fresh UUID unrelated to HEAD; it is allowed only if the *entire page*
    reconstructs exactly from a HEAD user message. No arbitrary state files,
    path-only matches, or concatenated HEAD excerpts are accepted.
    """
    path = args.get("file_path")
    if not isinstance(path, str) or str(PurePosixPath(path)) != path:
        return False
    parts = PurePosixPath(path).parts
    if len(parts) == 3 and parts[:2] == ("/", "large_tool_results"):
        tool_component = parts[2]
        for entry in entries:
            if entry["entry_kind"] != "tool":
                continue
            message = entry["checkpoint_json"]
            tool_id, source = message.get("tool_call_id"), message.get("content")
            if (
                isinstance(tool_id, str)
                and isinstance(source, str)
                and _sanitized_tool_id(tool_id) == tool_component
                and _exact_read_page(content, args, source)
            ):
                return True
        return False
    if _HISTORY_PATH.fullmatch(path):
        return any(
            entry["entry_kind"] == "user"
            and isinstance(entry["checkpoint_json"].get("content"), str)
            and _exact_read_page(content, args, entry["checkpoint_json"]["content"])
            for entry in entries
        )
    return False


def _grounded_read(content: str, entries: list[Row], proposal: dict[str, Any]) -> bool:
    """All internal file reads must be exactly grounded in packaged data or the current Run HEAD."""
    args = proposal.get("args")
    if not isinstance(args, dict) or not isinstance(args.get("file_path"), str):
        return False
    if args["file_path"].startswith("/skills"):
        return _grounded_packaged_skill_read(content, args)
    return _grounded_offload_read(content, entries, args)


def _run(coro: Any) -> Any:
    """Executors are async; _dispatch runs in a worker thread (blocking DB code), so drive the coroutine here."""
    return asyncio.run(coro)
