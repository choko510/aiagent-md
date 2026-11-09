"""Candidate B: DeepAgents 0.7.34 + LangGraph 1.2.20 + langgraph-checkpoint-postgres 3.2.0, behind AgentRuntimePort.

- Model: a BaseChatModel whose `_agenerate` only converts messages and calls the independent ModelGateway.
- Tools: LangChain tools whose body is `gateway_tool_stub`; built-in filesystem tools are replaced by a
  FilesystemMiddleware exposing no tools over an in-state backend (no host FS / shell / execute).
- Durability: AsyncPostgresSaver checkpoints (derived cache). thread_id = framework_ref.
- Approval pause: langgraph `interrupt()` inside the tool. Resume: Command(resume=...) re-runs the tool
  node (node replay); the Tool Gateway's approval row is the authority.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.profiles.harness.harness_profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)
from langchain.agents.middleware import AgentMiddleware
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.config import get_config
from langgraph.errors import GraphInterrupt
from langgraph.types import Command, interrupt
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from aiagent_conformance.control import Claim, Control, fault, race
from aiagent_conformance.gateways import (
    TOOL_SCHEMAS,
    AwaitingApproval,
    ModelGateway,
    SealedModelRequest,
    ToolGateway,
    UnknownEffect,
    gateway_tool_stub,
    sealed_model_call,
    settle_pending,
)
from aiagent_conformance.port import DerivedStateMismatch, SegmentOutcome, final_in_head, safe_detail, verify_against_head

NAME = "B:deepagents+langgraph"

# DeepAgents adds a "general-purpose" subagent (inheriting every main-agent tool) unless the harness
# profile resolved for the model disables it. Profiles are keyed by provider; ours is the class name.
register_harness_profile(
    "gatewaychatmodel", HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False))
)
FRAMEWORK_SCHEMAS = ["public"]  # checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations



@dataclass(frozen=True)
class RunCtx:
    """Per-run services and claim, passed through RunnableConfig (never a module global), so several runs
    can execute concurrently in one worker process without sharing a "current run"."""

    claim: Claim
    ctl: Control
    mgw: ModelGateway
    tgw: ToolGateway


def run_ctx(config: RunnableConfig | None = None) -> RunCtx:
    cfg = config if config is not None else get_config()
    return cfg["configurable"]["__aiagent"]


def to_neutral(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            continue
        if isinstance(m, HumanMessage):
            out.append({"kind": "user", "content": m.text})
        elif isinstance(m, AIMessage):
            out.append({"kind": "assistant", "content": m.text,
                        "tool_calls": [{"id": tc["id"], "name": tc["name"], "args": tc["args"]} for tc in m.tool_calls]})
        elif isinstance(m, ToolMessage):
            out.append({"kind": "tool", "tool_call_id": m.tool_call_id, "tool_name": m.name, "content": m.text})
    return out


def from_neutral(entries: list[dict[str, Any]]) -> list[BaseMessage]:
    msgs: list[BaseMessage] = []
    for e in entries:
        if e["kind"] == "user":
            msgs.append(HumanMessage(e["content"]))
        elif e["kind"] == "assistant":
            msgs.append(AIMessage(e.get("content", ""), tool_calls=[{"id": t["id"], "name": t["name"], "args": t["args"]} for t in e.get("tool_calls", [])]))
        elif e["kind"] == "tool":
            msgs.append(ToolMessage(e["content"], tool_call_id=e["tool_call_id"], name=e["tool_name"]))
    return msgs


class GatewayChatModel(BaseChatModel):
    """Holds no credential and no provider client. Retries/fallbacks belong to the Model Gateway."""

    gw_stream: str = "main"
    policy: str = "notify"

    @property
    def _llm_type(self) -> str:
        return "aiagent-fake-gateway"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def _generate(self, *a: Any, **k: Any) -> ChatResult:  # pragma: no cover - async only
        raise NotImplementedError

    async def _agenerate(self, messages: list[BaseMessage], stop: Any = None,
                         run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any) -> ChatResult:
        rc = run_ctx()
        names = [t["function"]["name"] for t in kwargs.get("tools", [])]
        req = SealedModelRequest(rc.claim, self.gw_stream, to_neutral(messages), names, self.policy)
        resp = await asyncio.to_thread(sealed_model_call, rc.ctl, rc.mgw, req)
        return ChatResult(generations=[ChatGeneration(message=from_neutral([resp])[0])])


def _tools(stream: str) -> list[Any]:
    async def call(config: RunnableConfig, tool_call_id: str, name: str, args: dict[str, Any]) -> str:
        rc = run_ctx(config)
        while True:
            try:
                return await gateway_tool_stub(rc.ctl, rc.tgw, rc.claim, tool_call_id, name, args, stream)
            except AwaitingApproval as e:
                # Pause the graph. The resume value is ignored: only the gateway's approval row counts.
                # If the gateway still says PENDING after a resume, the next interrupt() pauses again.
                interrupt({"approval_id": e.approval_id, "tool_call_id": tool_call_id})

    @tool
    async def search(query: str, tool_call_id: Annotated[str, InjectedToolCallId], config: RunnableConfig) -> str:
        """Read-only web search."""
        return await call(config, tool_call_id, "search", {"query": query})

    @tool
    async def send_message(to: str, body: str, tool_call_id: Annotated[str, InjectedToolCallId], config: RunnableConfig) -> str:
        """Send an external message (irreversible, needs approval)."""
        return await call(config, tool_call_id, "send_message", {"to": to, "body": body})

    return [search, send_message]


class HeadRecorder(AgentMiddleware):
    """Records results of framework-internal tools (`task`, `read_file`) in Checkpoint HEAD, so the HEAD holds
    every message the framework state holds and the checkpoint can be compared with it in full."""

    def __init__(self, stream: str):
        super().__init__()
        self.stream = stream

    @property
    def name(self) -> str:
        return f"HeadRecorder[{self.stream}]"

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        result = await handler(request)
        if request.tool_call["name"] in TOOL_SCHEMAS:
            return result  # gateway tools record their own result
        # DeepAgents' `task` returns a Command(update={"messages": [ToolMessage, ...]}), not a ToolMessage.
        msgs = [result] if isinstance(result, ToolMessage) else [
            m for m in (getattr(result, "update", None) or {}).get("messages", []) if isinstance(m, ToolMessage)]
        rc = run_ctx(request.runtime.config)
        for m in msgs:
            rc.ctl.append_head(rc.claim, "tool", {"kind": "tool", "tool_call_id": m.tool_call_id,
                                                  "tool_name": request.tool_call["name"], "content": m.text},
                               stream=self.stream, tool_call_id=m.tool_call_id)
        return result


def build_graph(policy: str, checkpointer: Any, *, compaction: bool) -> Any:
    profile = {"max_input_tokens": 8000} if compaction else {"max_input_tokens": 1_000_000}
    model = GatewayChatModel(gw_stream="main", policy=policy, profile=profile)
    no_fs = FilesystemMiddleware(backend=StateBackend(), tools=["read_file"])  # replaces built-in FS/execute tools; read_file is mandatory (StateBackend = graph state only)
    subagents = []
    if policy == "delegate":
        subagents.append({
            "name": "researcher",
            "description": "Researches prices",
            "system_prompt": "You research prices.",
            "tools": _tools("subagent:researcher")[:1],
            "model": GatewayChatModel(gw_stream="subagent:researcher", policy=policy, profile=profile),
            "middleware": [FilesystemMiddleware(backend=StateBackend(), tools=["read_file"]), HeadRecorder("subagent:researcher")],
        })
    return create_deep_agent(
        model=model,
        tools=_tools("main"),
        system_prompt="You are the aiagent-md conformance agent.",
        middleware=[no_fs, HeadRecorder("main")],
        subagents=subagents,
        backend=StateBackend(),
        checkpointer=checkpointer,
    )


class CandidateB:
    name = NAME
    framework_schemas = FRAMEWORK_SCHEMAS

    def __init__(self, ctl: Control, mgw: ModelGateway, tgw: ToolGateway, db_url: str, *, parallel: bool = False, compaction: bool = False):
        self.ctl, self.mgw, self.tgw, self.db_url = ctl, mgw, tgw, db_url
        self.parallel, self.compaction = parallel, compaction
        self.graphs: dict[str, Any] = {}

    async def launch(self) -> None:
        pass  # nothing runs in the background: recovery happens only when run_segment is called

    async def _graph(self, policy: str) -> Any:
        if policy not in self.graphs:
            if not hasattr(self, "conn"):
                self.conn = await AsyncConnection.connect(self.db_url, autocommit=True, prepare_threshold=0, row_factory=dict_row)
                self.saver = AsyncPostgresSaver(self.conn)
                await self.saver.setup()
            self.graphs[policy] = build_graph(policy, self.saver, compaction=self.compaction)
        return self.graphs[policy]

    async def run_segment(self, claim: Claim, policy: str) -> SegmentOutcome:
        graph = await self._graph(policy)
        ref = self.ctl.run(claim.run_id)["framework_ref"]
        head = self.ctl.transcript(claim.run_id)
        try:
            inp: Any
            if ref is not None:
                cfg = self._cfg(ref, claim)
                state = await graph.aget_state(cfg)
                verify_against_head(to_neutral(state.values.get("messages", [])), head)
                if final_in_head(head):
                    return SegmentOutcome("FINAL", text=head[-1]["content"])  # HEAD is the answer; checkpoint only verified
                if state.interrupts:
                    inp = Command(resume={i.id: {"ask_gateway": True} for i in state.interrupts})
                elif state.next:
                    inp = None  # killed mid-superstep: LangGraph replays the pending node(s)
                else:
                    raise DerivedStateMismatch("checkpoint is complete but HEAD has no final answer")
            else:
                if final_in_head(head):
                    return SegmentOutcome("FINAL", text=head[-1]["content"])
                # Rebuild from HEAD. Dangling tool calls must be settled through the gateway first:
                # DeepAgents' PatchToolCallsMiddleware would otherwise mark them "cancelled" silently.
                await settle_pending(self.ctl, self.tgw, claim)
                head = self.ctl.transcript(claim.run_id)
                ref = f"{claim.run_id}:{uuid.uuid4().hex[:8]}"
                self.ctl.set_framework_ref(claim, ref)
                inp = {"messages": from_neutral(head)}
                cfg = self._cfg(ref, claim)
            result = await graph.ainvoke(inp, cfg, durability="sync" if not self.parallel else "async")
            if result.get("__interrupt__"):
                return SegmentOutcome("AWAITING_APPROVAL", approval_ids=[i.value.get("approval_id") for i in result["__interrupt__"]])
            fault("before_segment_return")  # framework finished and checkpointed; adapter has not returned yet
            head = self.ctl.transcript(claim.run_id)
            verify_against_head(to_neutral(result.get("messages", [])), head, complete=True)
            return SegmentOutcome("FINAL", text=head[-1]["content"])
        except UnknownEffect:
            return SegmentOutcome("UNKNOWN_EFFECT")
        except AwaitingApproval as e:
            return SegmentOutcome("AWAITING_APPROVAL", approval_ids=[e.approval_id])
        except GraphInterrupt:  # pragma: no cover - surfaced via __interrupt__ normally
            return SegmentOutcome("AWAITING_APPROVAL")
        except Exception as e:
            if type(e).__name__ in ("FencedOut", "StaleGoal", "Cancelled"):
                return SegmentOutcome(type(e).__name__.upper())
            race("before_discard_framework_ref", self.ctl.url)
            self.ctl.discard_framework_ref(claim, ref)
            return SegmentOutcome("REBUILD", detail=safe_detail(e))

    def _cfg(self, ref: str, claim: Claim) -> RunnableConfig:
        rc = RunCtx(claim, self.ctl, self.mgw, self.tgw)
        return {"configurable": {"thread_id": ref, "__aiagent": rc}, "recursion_limit": 50,
                "max_concurrency": None if self.parallel else 1}

    async def close(self) -> None:
        if hasattr(self, "conn"):
            await self.conn.close()
