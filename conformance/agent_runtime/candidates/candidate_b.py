"""Candidate B: DeepAgents 0.7.34 + LangGraph 1.2.20 + langgraph-checkpoint-postgres 3.2.0, behind AgentRuntimePort.

- Model: a BaseChatModel whose `_agenerate` only converts messages and calls the independent ModelGateway.
- Tools: LangChain tools whose body is `gateway_tool_stub`; built-in filesystem tools are replaced by a
  FilesystemMiddleware exposing no tools over an in-state backend (no host FS / shell / execute).
- Durability: AsyncPostgresSaver checkpoints (derived cache). thread_id = framework_ref.
- Approval pause: langgraph `interrupt()` inside the tool. Resume: Command(resume=...) re-runs the tool
  node (node replay); the Tool Gateway's approval row is the authority.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.profiles.harness.harness_profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.errors import GraphInterrupt
from langgraph.types import Command, interrupt
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from aiagent_conformance.control import Claim, Control
from aiagent_conformance.gateways import (
    AwaitingApproval,
    ModelGateway,
    SealedModelRequest,
    ToolGateway,
    UnknownEffect,
    gateway_tool_stub,
    sealed_model_call,
    settle_pending,
)
from aiagent_conformance.port import DerivedStateMismatch, SegmentOutcome

NAME = "B:deepagents+langgraph"

# DeepAgents adds a "general-purpose" subagent (inheriting every main-agent tool) unless the harness
# profile resolved for the model disables it. Profiles are keyed by provider; ours is the class name.
register_harness_profile(
    "gatewaychatmodel", HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False))
)
FRAMEWORK_SCHEMAS = ["public"]  # checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations

CLAIMS: dict[str, Claim] = {}
_ctx: dict[str, Any] = {}


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
        run_id = _ctx["run_id"]
        names = [t["function"]["name"] for t in kwargs.get("tools", [])]
        _ctx.setdefault("seen_tools", {}).setdefault(self.gw_stream, set()).update(names)
        req = SealedModelRequest(CLAIMS[run_id], self.gw_stream, to_neutral(messages), names, self.policy)
        import asyncio

        resp = await asyncio.to_thread(sealed_model_call, _ctx["ctl"], _ctx["mgw"], req)
        return ChatResult(generations=[ChatGeneration(message=from_neutral([resp])[0])])


def _tools(stream: str) -> list[Any]:
    async def call(config: RunnableConfig, tool_call_id: str, name: str, args: dict[str, Any]) -> str:
        claim = CLAIMS[config["configurable"]["run_id"]]
        while True:
            try:
                return await gateway_tool_stub(_ctx["ctl"], _ctx["tgw"], claim, tool_call_id, name, args, stream)
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
            "middleware": [FilesystemMiddleware(backend=StateBackend(), tools=["read_file"])],
        })
    return create_deep_agent(
        model=model,
        tools=_tools("main"),
        system_prompt="You are the aiagent-md conformance agent.",
        middleware=[no_fs],
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
        _ctx.update(ctl=ctl, mgw=mgw, tgw=tgw)
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
        CLAIMS[claim.run_id] = claim
        _ctx["run_id"] = claim.run_id
        graph = await self._graph(policy)
        ref = self.ctl.run(claim.run_id)["framework_ref"]
        head = self.ctl.transcript(claim.run_id)
        try:
            inp: Any
            if ref is None:
                # Rebuild from HEAD. Dangling tool calls must be settled through the gateway first:
                # DeepAgents' PatchToolCallsMiddleware would otherwise mark them "cancelled" silently.
                await settle_pending(self.ctl, self.tgw, claim)
                head = self.ctl.transcript(claim.run_id)
                ref = f"{claim.run_id}:{uuid.uuid4().hex[:8]}"
                self.ctl.set_framework_ref(claim, ref)
                inp = {"messages": from_neutral(head)}
                cfg = self._cfg(ref, claim)
            else:
                cfg = self._cfg(ref, claim)
                state = await graph.aget_state(cfg)
                self._verify(state.values.get("messages", []), head)
                if state.interrupts:
                    inp = Command(resume={i.id: {"ask_gateway": True} for i in state.interrupts})
                elif state.next:
                    inp = None  # killed mid-superstep: LangGraph replays the pending node(s)
                else:
                    return self._final(state.values)
            result = await graph.ainvoke(inp, cfg, durability="sync" if not self.parallel else "async")
            if result.get("__interrupt__"):
                return SegmentOutcome("AWAITING_APPROVAL", approval_ids=[i.value.get("approval_id") for i in result["__interrupt__"]])
            return self._final(result)
        except UnknownEffect:
            return SegmentOutcome("UNKNOWN_EFFECT")
        except AwaitingApproval as e:
            return SegmentOutcome("AWAITING_APPROVAL", approval_ids=[e.approval_id])
        except GraphInterrupt:  # pragma: no cover - surfaced via __interrupt__ normally
            return SegmentOutcome("AWAITING_APPROVAL")
        except Exception as e:
            if type(e).__name__ in ("FencedOut", "StaleGoal"):
                return SegmentOutcome(type(e).__name__.upper())
            self.ctl.db.execute("UPDATE aiagent.runs SET framework_ref = NULL WHERE run_id = %s", (claim.run_id,))
            return SegmentOutcome("REBUILD", detail=f"{type(e).__name__}: {e}"[:300])

    def _cfg(self, ref: str, claim: Claim) -> RunnableConfig:
        return {"configurable": {"thread_id": ref, "run_id": claim.run_id}, "recursion_limit": 50,
                "max_concurrency": None if self.parallel else 1}

    @staticmethod
    def _verify(state_messages: list[BaseMessage], head: list[dict[str, Any]]) -> None:
        """Framework state must agree with HEAD on every tool call it carries (derived-cache check)."""
        head_calls = {tc["id"]: tc for e in head if e["kind"] == "assistant" for tc in e.get("tool_calls", [])}
        for m in to_neutral(state_messages):
            for tc in m.get("tool_calls", []) if m["kind"] == "assistant" else []:
                h = head_calls.get(tc["id"])
                if h is None or h["name"] != tc["name"] or h["args"] != tc["args"]:
                    raise DerivedStateMismatch(f"checkpoint tool call {tc} not in HEAD")

    @staticmethod
    def _final(values: dict[str, Any]) -> SegmentOutcome:
        msgs = values.get("messages", [])
        last = msgs[-1] if msgs else None
        return SegmentOutcome("FINAL", text=last.text if isinstance(last, AIMessage) else None)

    async def close(self) -> None:
        if hasattr(self, "conn"):
            await self.conn.close()
