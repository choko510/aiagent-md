"""AgentRuntimePort implementation (segment granularity, as validated in STEP 2).

A run segment runs until FINAL / approval interrupt / UNKNOWN effect / loss of fence, goal or cancel. The framework
checkpoint (LangGraph, dedicated schema, strict serializer) is a derived cache: on any disagreement with the
canonical Checkpoint HEAD, on a rejected blob, or on a steer that added user input, it is discarded (only if it is
still this claim's pointer) and the graph is rebuilt from HEAD after settling dangling tool calls via the gateway.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from aiagent_core import control
from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_domain import sha256_hex
from aiagent_domain.rpcwire import RpcError
from aiagent_domain.tools import TOOLS
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from .agent import (
    AwaitingApproval,
    GatewayRejected,
    Gateways,
    RunCtx,
    UnknownEffect,
    build_graph,
    fault,
    from_neutral,
    submit_tool,
    to_neutral,
)
from .serde import CheckpointRejected, StrictSerializer


class DerivedStateMismatch(Exception):
    """Framework state disagrees with the aiagent-md Checkpoint HEAD."""


@dataclass
class SegmentOutcome:
    status: (
        str  # FINAL | AWAITING_APPROVAL | UNKNOWN_EFFECT | REBUILD | FENCEDOUT | STALEGOAL | CANCELLED | BUDGET | ERROR
    )
    text: str | None = None
    approval_ids: list[str] = field(default_factory=list)
    detail: str | None = None


_CODE_TO_STATUS = {
    "FENCED": "FENCEDOUT",
    "RESTORE_FENCED": "FENCEDOUT",
    "TENANT": "FENCEDOUT",
    "STALE_GOAL": "STALEGOAL",
    "CANCELLED": "CANCELLED",
    "TERMINAL": "CANCELLED",
    "BUDGET_EXHAUSTED": "BUDGET",
}


def normalize_order(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tool results directly follow the assistant message that proposed them (provider ordering rule), even when a
    steer's user entry was committed in between. Stable otherwise."""
    tools = {e["tool_call_id"]: e for e in entries if e["kind"] == "tool"}
    out: list[dict[str, Any]] = []
    placed: set[str] = set()
    for e in entries:
        if e["kind"] == "tool":
            if e["tool_call_id"] not in placed and not any(
                t["id"] == e["tool_call_id"] for a in out if a["kind"] == "assistant" for t in a.get("tool_calls", [])
            ):
                out.append(e)
                placed.add(e["tool_call_id"])
            continue
        out.append(e)
        if e["kind"] == "assistant":
            for tc in e.get("tool_calls", []):
                if tc["id"] in tools and tc["id"] not in placed:
                    out.append(tools[tc["id"]])
                    placed.add(tc["id"])
    return out


def _norm(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": m.get("kind"),
        "content": m.get("content") or "",
        "tool_call_id": m.get("tool_call_id") if m.get("kind") == "tool" else None,
        "tool_calls": [[tc.get("id"), tc.get("name"), tc.get("args")] for tc in m.get("tool_calls") or []],
    }


def verify_against_head(framework: list[dict[str, Any]], head: list[dict[str, Any]], *, complete: bool = False) -> int:
    """Framework transcript must equal HEAD (normalized order) as a prefix; it may lag (killed after the HEAD commit,
    before the checkpoint write) but never differ or run ahead. Diagnostics carry digests only, never content."""
    f = [_norm(m) for m in framework if m.get("kind") in ("user", "assistant", "tool")]
    h = [_norm(m) for m in normalize_order(head)]
    if len(f) > len(h):
        raise DerivedStateMismatch(f"framework ahead of HEAD ({len(f)} > {len(h)})")
    for i, (a, b) in enumerate(zip(f, h, strict=False)):
        if a != b:
            raise DerivedStateMismatch(
                f"message {i} differs: kind={a['kind']}/{b['kind']} fw={sha256_hex(a)[:16]} head={sha256_hex(b)[:16]}"
            )
    if complete and len(f) != len(h):
        raise DerivedStateMismatch(f"framework final transcript shorter than HEAD ({len(f)} < {len(h)})")
    return len(f)


def safe_detail(e: BaseException) -> str:
    if isinstance(e, DerivedStateMismatch | CheckpointRejected):
        return f"{type(e).__name__}: {e}"
    return f"{type(e).__name__}: digest={sha256_hex(str(e))[:16]}"


def final_in_head(head: list[dict[str, Any]]) -> bool:
    return bool(head) and head[-1]["kind"] == "assistant" and not head[-1].get("tool_calls")


class DeepAgentsRuntime:
    name = "deepagents+langgraph"

    def __init__(
        self,
        db: Database,
        checkpointer_dsn: str,
        gateways: Gateways,
        *,
        model: str = "fake-gateway",
        max_input_tokens: int = 100_000,
    ) -> None:
        self.db, self.checkpointer_dsn, self.gw = db, checkpointer_dsn, gateways
        self.model, self.max_input_tokens = model, max_input_tokens
        self.graphs: dict[str, Any] = {}
        self.pool: Any = None
        self.saver: Any = None

    async def launch(self) -> None:
        """Nothing runs in the background: recovery happens only when run_segment is called for a claimed run.
        The checkpointer never runs DDL (`setup()` is not called; schema is owned by migrations)."""
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        self.pool = AsyncConnectionPool(
            self.checkpointer_dsn,
            min_size=1,
            max_size=8,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await self.pool.open()
        self.saver = AsyncPostgresSaver(self.pool, serde=StrictSerializer())  # type: ignore[arg-type]

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    def graph(self, profile: str) -> Any:
        if profile not in self.graphs:
            self.graphs[profile] = build_graph(profile, self.saver, max_input_tokens=self.max_input_tokens)
        return self.graphs[profile]

    def _cfg(self, ref: str, rc: RunCtx) -> dict[str, Any]:
        return {"configurable": {"thread_id": ref, "__aiagent": rc}, "recursion_limit": 60, "max_concurrency": 1}

    def _head(self, claim: Claim) -> list[dict[str, Any]]:
        with self.db.conn() as c:
            return control.transcript(c, claim.tenant_id, claim.run_id, "main")

    async def _settle_pending(self, rc: RunCtx) -> None:
        """Before (re)building framework state from HEAD, resolve every proposed-but-unanswered tool call of the main
        stream through the gateway (same invocation keys -> idempotent; never a second external send)."""
        head = self._head(rc.claim)
        answered = {e["tool_call_id"] for e in head if e["kind"] == "tool"}
        for e in head:
            if e["kind"] != "assistant":
                continue
            for tc in e.get("tool_calls", []):
                if tc["id"] in answered:
                    continue
                if tc["name"] in TOOLS:
                    o = await submit_tool(rc, tc["id"], tc["name"], tc["args"])
                    if o["status"] == "PENDING_APPROVAL":
                        raise AwaitingApproval(str(o.get("approvalId")), tc["id"])
                    if o["status"] == "UNKNOWN":
                        raise UnknownEffect(tc["id"])
                    if o["status"] == "CANCELLED":
                        raise GatewayRejected("CANCELLED")
                else:
                    # an internal call (sub-agent / read_file) interrupted by a crash: record an explicit, ungrounded
                    # result so HEAD and framework agree; the model may re-delegate.
                    try:
                        await rc.gw.tool.call(
                            "tool.record_internal",
                            {
                                "claim": rc.claim.to_wire(),
                                "toolCallId": tc["id"],
                                "tool": tc["name"],
                                "content": "[interrupted before completion; no result]",
                            },
                        )
                    except RpcError as err:
                        raise GatewayRejected(err.code) from None

    async def run_segment(self, claim: Claim, profile: str, *, on_delta: Any = None) -> SegmentOutcome:
        graph = self.graph(profile)
        rc = RunCtx(claim, self.gw, self.model, on_delta)
        ref = control.framework_ref(self.db, claim.tenant_id, claim.run_id)
        try:
            head = self._head(claim)
            inp: Any
            if ref is not None:
                cfg = self._cfg(ref, rc)
                state = await graph.aget_state(cfg)
                fw = to_neutral(state.values.get("messages", []))
                n = verify_against_head(fw, head)
                if final_in_head(head):
                    return SegmentOutcome("FINAL", text=head[-1]["content"])
                if any(m["kind"] == "user" for m in normalize_order(head)[n:]):
                    raise DerivedStateMismatch("HEAD has new user input (steer) not in framework state")
                if state.interrupts:
                    inp = Command(resume={i.id: {"ask_gateway": True} for i in state.interrupts})
                elif state.next:
                    inp = None  # killed mid-superstep: LangGraph replays the pending node(s)
                else:
                    raise DerivedStateMismatch("checkpoint complete but HEAD has no final answer")
            else:
                if final_in_head(head):
                    return SegmentOutcome("FINAL", text=head[-1]["content"])
                await self._settle_pending(rc)
                head = self._head(claim)
                if final_in_head(head):
                    return SegmentOutcome("FINAL", text=head[-1]["content"])
                ref = f"{claim.run_id}:{uuid.uuid4().hex[:8]}"
                control.set_framework_ref(self.db, claim, ref)
                inp = {"messages": from_neutral(normalize_order(head))}
                cfg = self._cfg(ref, rc)
            result = await graph.ainvoke(inp, cfg, durability="sync")
            if result.get("__interrupt__"):
                return SegmentOutcome(
                    "AWAITING_APPROVAL", approval_ids=[str(i.value.get("approval_id")) for i in result["__interrupt__"]]
                )
            fault("before_segment_return")
            head = self._head(claim)
            verify_against_head(to_neutral(result.get("messages", [])), head, complete=True)
            if not final_in_head(head):
                raise DerivedStateMismatch("graph finished without a final answer in HEAD")
            return SegmentOutcome("FINAL", text=head[-1]["content"])
        except UnknownEffect:
            return SegmentOutcome("UNKNOWN_EFFECT")
        except AwaitingApproval as e:
            return SegmentOutcome("AWAITING_APPROVAL", approval_ids=[e.approval_id])
        except GraphInterrupt:  # pragma: no cover - surfaced via __interrupt__ normally
            return SegmentOutcome("AWAITING_APPROVAL")
        except GatewayRejected as e:
            st = _CODE_TO_STATUS.get(e.code)
            if st is not None:
                return SegmentOutcome(st, detail=e.code)
            if e.code in ("NOT_IN_HEAD", "INTENT_CONFLICT"):
                control.discard_framework_ref(self.db, claim, ref)
                return SegmentOutcome("REBUILD", detail=e.code)
            return SegmentOutcome("ERROR", detail=e.code)
        except control.ControlError as e:
            return SegmentOutcome(_CODE_TO_STATUS.get(e.code, "ERROR"), detail=e.code)
        except Exception as e:
            control.discard_framework_ref(self.db, claim, ref)
            return SegmentOutcome("REBUILD", detail=safe_detail(e))
