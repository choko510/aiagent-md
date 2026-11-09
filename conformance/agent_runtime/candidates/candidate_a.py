"""Candidate A: PydanticAI 2.66 + pydantic-ai-harness 0.66 + DBOS 3.4, behind AgentRuntimePort.

- Model: FunctionModel whose function only converts messages and calls the independent ModelGateway.
- Tools: plain function tools whose body is `gateway_tool_stub` (no Secret / network / shell / CDP).
- Durability: DBOSDurability (model requests become DBOS steps) inside one DBOS workflow per run segment.
- Approval pause: ApprovalRequired -> DeferredToolRequests. Resume: DeferredToolResults(approve=...)
  only means "ask the Tool Gateway again"; the gateway's approval row is the authority.
- No harness file/shell/code-mode/web capability is attached.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from dbos import DBOS, SetEnqueueOptions, SetWorkflowID
from dbos._error import DBOSQueueDeduplicatedError
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities.abstract import AbstractCapability
from pydantic_ai.durable_exec.dbos import DBOSDurability
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai_harness.compaction import SummarizingCompaction
from pydantic_ai_harness.subagents import SubAgent, SubAgents

from aiagent_conformance.control import Claim, Control, fault
from aiagent_conformance.gateways import (
    TOOL_SCHEMAS,
    AwaitingApproval,
    ModelGateway,
    SealedModelRequest,
    ToolGateway,
    UnknownEffect,
    gateway_tool_stub,
    sealed_model_call,
)
from aiagent_conformance.port import DerivedStateMismatch, SegmentOutcome, final_in_head, verify_against_head

NAME = "A:pydantic-ai+dbos"
FRAMEWORK_SCHEMAS = ["dbos"]



@dataclass(frozen=True)
class Services:
    ctl: Control
    mgw: ModelGateway
    tgw: ToolGateway


# The *current run* is a per-task ContextVar set at the top of each segment workflow execution, so
# concurrent runs in one worker process (each its own asyncio task) never share it. The claim is NOT a
# workflow input: a recovered workflow uses the fence of the worker that recovered it.
CURRENT_CLAIM: ContextVar[Claim] = ContextVar("aiagent_current_claim")


def to_neutral(messages: list[ModelMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, UserPromptPart):
                    out.append({"kind": "user", "content": p.content if isinstance(p.content, str) else str(p.content)})
                elif isinstance(p, SystemPromptPart):
                    out.append({"kind": "system", "content": p.content})
                elif isinstance(p, ToolReturnPart):
                    out.append({"kind": "tool", "tool_call_id": p.tool_call_id, "tool_name": p.tool_name, "content": p.model_response_str()})
                elif isinstance(p, RetryPromptPart):
                    out.append({"kind": "tool" if p.tool_name else "user", "tool_call_id": p.tool_call_id, "tool_name": p.tool_name, "content": p.model_response()})
        elif isinstance(m, ModelResponse):
            text = "".join(p.content for p in m.parts if isinstance(p, TextPart))
            calls = [{"id": p.tool_call_id, "name": p.tool_name, "args": p.args_as_dict()} for p in m.parts if isinstance(p, ToolCallPart)]
            out.append({"kind": "assistant", "content": text, "tool_calls": calls})
    return out


def from_neutral(entries: list[dict[str, Any]]) -> list[ModelMessage]:
    msgs: list[ModelMessage] = []
    for e in entries:
        if e["kind"] == "user":
            msgs.append(ModelRequest(parts=[UserPromptPart(e["content"])]))
        elif e["kind"] == "assistant":
            parts: list[Any] = []
            if e.get("content"):
                parts.append(TextPart(e["content"]))
            parts += [ToolCallPart(tc["name"], tc["args"], tc["id"]) for tc in e.get("tool_calls", [])]
            msgs.append(ModelResponse(parts=parts or [TextPart("")]))
        elif e["kind"] == "tool":
            part = ToolReturnPart(e["tool_name"], e["content"], e["tool_call_id"])
            if msgs and isinstance(msgs[-1], ModelRequest) and all(isinstance(p, ToolReturnPart) for p in msgs[-1].parts):
                msgs[-1] = ModelRequest(parts=[*msgs[-1].parts, part])
            else:
                msgs.append(ModelRequest(parts=[part]))
    return msgs


def _model(svc: Services, stream: str, policy: str) -> FunctionModel:
    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        claim = CURRENT_CLAIM.get()
        neutral = [m for m in to_neutral(messages) if m["kind"] != "system"]
        tools = [t.name for t in info.function_tools]
        req = SealedModelRequest(claim, stream, neutral, tools, policy)
        resp = await asyncio.to_thread(sealed_model_call, svc.ctl, svc.mgw, req)
        return from_neutral([resp])[0]  # type: ignore[return-value]

    return FunctionModel(fn, model_name=f"fake-gateway-{stream}")


def _tools(svc: Services, stream: str) -> list[Any]:
    async def call(ctx: RunContext[Claim], tool: str, args: dict[str, Any]) -> str:
        try:
            return await gateway_tool_stub(svc.ctl, svc.tgw, ctx.deps, ctx.tool_call_id or "", tool, args, stream)
        except AwaitingApproval as e:
            raise ApprovalRequired(metadata={"approval_id": e.approval_id}) from None

    async def search(ctx: RunContext[Claim], query: str) -> str:
        """Read-only web search."""
        return await call(ctx, "search", {"query": query})

    async def send_message(ctx: RunContext[Claim], to: str, body: str) -> str:
        """Send an external message (irreversible, needs approval)."""
        return await call(ctx, "send_message", {"to": to, "body": body})

    return [search, send_message]


@dataclass
class HeadRecorder(AbstractCapability[Claim]):
    """Records results of framework-internal tools (`delegate_task`) in Checkpoint HEAD so the framework
    transcript can be compared with HEAD in full. Gateway tools record their own results."""

    svc: Services
    stream: str = "main"

    async def wrap_tool_execute(self, ctx: RunContext[Claim], *, call: ToolCallPart, tool_def: Any, args: Any, handler: Any) -> Any:
        result = await handler(args)
        if call.tool_name not in TOOL_SCHEMAS:
            content = ToolReturnPart(call.tool_name, result, call.tool_call_id).model_response_str()
            await asyncio.to_thread(
                self.svc.ctl.append_head, ctx.deps, "tool",
                {"kind": "tool", "tool_call_id": call.tool_call_id, "tool_name": call.tool_name, "content": content},
                stream=self.stream, tool_call_id=call.tool_call_id)
        return result


def build_agent(svc: Services, policy: str, *, parallel_mode: str = "sequential", compaction: bool = False) -> Agent[Claim, Any]:
    caps: list[Any] = [HeadRecorder(svc)]
    if policy == "delegate":
        child = Agent(
            _model(svc, "subagent:researcher", policy),
            name="researcher",
            description="Researches prices",
            deps_type=Claim,
            tools=_tools(svc, "subagent:researcher")[:1],  # read-only tool only
        )
        caps.append(SubAgents(agents=[SubAgent(child)]))
    if compaction:
        caps.append(SummarizingCompaction(model=_model(svc, "summarize", policy), max_messages=6, keep_messages=4))
    caps.append(DBOSDurability(name=f"aiagent_a_{policy}_{int(compaction)}", parallel_execution_mode=parallel_mode))  # type: ignore[arg-type]
    return Agent(
        _model(svc, "main", policy),
        name=f"aiagent_a_{policy}_{int(compaction)}",
        deps_type=Claim,
        output_type=[str, DeferredToolRequests],
        tools=_tools(svc, "main"),
        capabilities=caps,
    )


class CandidateA:
    name = NAME
    framework_schemas = FRAMEWORK_SCHEMAS

    def __init__(self, ctl: Control, mgw: ModelGateway, tgw: ToolGateway, db_url: str, *, parallel_mode: str = "sequential",
                 compaction: bool = False, executor_id: str = "local"):
        self.ctl, self.mgw, self.tgw, self.executor_id = ctl, mgw, tgw, executor_id
        svc = Services(ctl, mgw, tgw)
        self.agents = {p: build_agent(svc, p, parallel_mode=parallel_mode, compaction=compaction) for p in ("notify", "parallel_writes", "delegate")}
        agents = self.agents
        self.claims: dict[str, Claim] = {}  # keyed by run: concurrent runs never read each other's claim
        claims = self.claims

        @DBOS.step(name="aiagent_a.read_head")
        async def read_head(run_id: str) -> list[dict[str, Any]]:
            return ctl.transcript(run_id)

        @DBOS.workflow(name="aiagent_a.segment")
        async def segment_wf(run_id: str, policy: str) -> dict[str, Any]:
            # Inputs carry no fence/claim and no history. The HEAD snapshot MUST be read inside a step:
            # a plain read makes the workflow non-deterministic, and on recovery DBOS then replays recorded
            # model outputs at shifted step positions (observed in STEP 2, PYRT-T04 swap).
            claim = claims[run_id]
            CURRENT_CLAIM.set(claim)
            head = await read_head(run_id)
            if final_in_head(head):
                return {"status": "FINAL", "text": head[-1]["content"], "messages": head}
            history = from_neutral(head)
            prompt = None
            last = history[-1]
            pending = []
            if isinstance(last, ModelResponse):
                pending = [p.tool_call_id for p in last.parts if isinstance(p, ToolCallPart)]
            elif isinstance(last, ModelRequest) and isinstance(last.parts[-1], UserPromptPart):
                prompt = last.parts[-1].content
                history = history[:-1]
            deferred = DeferredToolResults(approvals={i: True for i in pending}) if pending else None
            res = await agents[policy].run(prompt, message_history=history, deferred_tool_results=deferred, deps=claim)
            if isinstance(res.output, DeferredToolRequests):
                return {"status": "AWAITING_APPROVAL", "approval_ids": [res.output.metadata.get(c.tool_call_id, {}).get("approval_id") for c in res.output.approvals]}
            fault("before_segment_return")  # every model step is recorded; the workflow has not completed
            return {"status": "FINAL", "text": res.output,
                    "messages": [m for m in to_neutral(res.all_messages()) if m["kind"] != "system"]}

        self.segment_wf = segment_wf
        # One executor_id per worker process: DBOS.launch() then auto-recovers nothing that belongs to
        # another worker. Recovery of a dead worker's workflow is driven by the aiagent-md claim instead.
        cfg: dict[str, Any] = {"name": "aiagent_a", "system_database_url": db_url, "log_level": "WARNING",
                               "executor_id": executor_id, "notification_listener_polling_interval_sec": 0.05}
        if os.environ.get("AIAGENT_DBOS_SERIALIZER") == "portable_json":
            from dbos._serialization import DBOSPortableJSONSerializer

            cfg["serializer"] = DBOSPortableJSONSerializer()
        DBOS(config=cfg)  # type: ignore[arg-type]

    async def launch(self) -> None:
        DBOS.launch()
        self.queue = await DBOS.register_queue_async("aiagent_runs", worker_concurrency=4, polling_interval_sec=0.1)

    async def run_segment(self, claim: Claim, policy: str) -> SegmentOutcome:
        self.claims[claim.run_id] = claim
        CURRENT_CLAIM.set(claim)
        ref = self.ctl.run(claim.run_id)["framework_ref"]
        try:
            st = await DBOS.get_workflow_status_async(ref) if ref else None
            if st is not None and st.status == "PENDING" and st.executor_id != self.executor_id:
                # Dead worker's segment: re-execute it here (recorded steps replay, the rest runs again).
                # (private API: DBOS has no public "recover this one workflow of a dead executor" call)
                await asyncio.to_thread(DBOS._recover_pending_workflows, [st.executor_id or "local"])
                handle = await DBOS.retrieve_workflow_async(ref)
            elif st is not None and st.status in ("PENDING", "ENQUEUED"):
                handle = await DBOS.retrieve_workflow_async(ref)  # our own queue runner executes it
            else:
                ref = f"{claim.run_id}:{uuid.uuid4().hex[:8]}"
                self.ctl.set_framework_ref(claim, ref)
                with SetWorkflowID(ref):
                    handle = await DBOS.start_workflow_async(self.segment_wf, claim.run_id, policy)
            out = await handle.get_result()
            if out["status"] == "FINAL":
                # The workflow result is derived (DBOS may have replayed recorded steps): it must equal HEAD.
                head = self.ctl.transcript(claim.run_id)
                verify_against_head(out["messages"], head, complete=True)
                out["text"] = head[-1]["content"]
        except AwaitingApproval as e:
            return SegmentOutcome("AWAITING_APPROVAL", approval_ids=[e.approval_id])
        except UnknownEffect:
            return SegmentOutcome("UNKNOWN_EFFECT")
        except DerivedStateMismatch as e:
            await self._discard(ref)
            return SegmentOutcome("REBUILD", detail=str(e))
        except Exception as e:  # corrupted/undecodable framework state, DBOS step errors, ...
            if type(e).__name__ in ("FencedOut", "StaleGoal", "Cancelled"):
                return SegmentOutcome(type(e).__name__.upper())
            await self._discard(ref)
            return SegmentOutcome("REBUILD", detail=f"{type(e).__name__}: {e}"[:300])
        return SegmentOutcome(out["status"], text=out.get("text"), approval_ids=out.get("approval_ids", []))

    async def enqueue(self, claim: Claim, policy: str) -> dict[str, Any]:
        """QueuePort via DBOS Queue. A duplicate delivery of the same run is rejected by deduplication_id."""
        ref = self.ctl.run(claim.run_id)["framework_ref"]
        st = await DBOS.get_workflow_status_async(ref) if ref else None
        if st is not None and st.status in ("PENDING", "ENQUEUED"):
            return {"enqueued": False, "deduplicated": 0, "recovering": True}
        ref = f"{claim.run_id}:{uuid.uuid4().hex[:8]}"
        self.ctl.set_framework_ref(claim, ref)
        deduped = 0
        with SetWorkflowID(ref), SetEnqueueOptions(deduplication_id=claim.run_id):
            await self.queue.enqueue_async(self.segment_wf, claim.run_id, policy)
        try:  # simulated duplicate delivery of the same job
            with SetWorkflowID(ref + ":dup"), SetEnqueueOptions(deduplication_id=claim.run_id):
                await self.queue.enqueue_async(self.segment_wf, claim.run_id, policy)
        except DBOSQueueDeduplicatedError:
            deduped = 1
        return {"enqueued": True, "deduplicated": deduped}

    async def _discard(self, ref: str | None) -> None:
        if ref:
            try:
                await DBOS.cancel_workflow_async(ref)
            except Exception:
                pass
        self.ctl.db.execute("UPDATE aiagent.runs SET framework_ref = NULL WHERE framework_ref = %s", (ref,))

    async def close(self) -> None:
        DBOS.destroy(destroy_registry=True)
