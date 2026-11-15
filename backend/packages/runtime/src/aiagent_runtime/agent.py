"""DeepAgents + LangGraph graph behind the AgentRuntimePort (ADR-038). The framework is a *derived* executor:

* Model: `GatewayChatModel` only converts messages and calls the independent Model Gateway over authenticated RPC.
  It holds no credential and no provider client; retries/fallback belong to the gateway.
* Tools: stubs generated from the registry allowlist of the run's profile. Their body only asks the Tool Gateway
  (which owns intent/approval/ledger and writes the HEAD result). Approval pauses the graph with `interrupt()`; the
  resume value is ignored - only the gateway's approval row counts.
* Framework built-ins: no shell/execute, no host filesystem, no general-purpose subagent. The filesystem middleware
  exposes only `read_file` over in-state storage plus packaged read-only skills; framework-internal tool results
  (`task`, `read_file`) enter HEAD only through the Tool Gateway's grounded ChildResult admission.
* Compaction: DeepAgents' non-mutating summarization with the summarizer routed through the Model Gateway
  (`summarize` stream). Raw messages stay in state and HEAD; the summary is a derived view.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from aiagent_core.control import Claim
from aiagent_core.rpc import RpcClient
from aiagent_domain.rpcwire import RpcError
from aiagent_domain.tools import PROFILE_TOOLS, SKILLS_DIR, TOOLS
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.summarization import create_summarization_middleware
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
from langgraph.config import get_config
from langgraph.types import interrupt

SYSTEM_PROMPT = (
    "You are aiagent-md. Tools require Gateway review; irreversible actions need explicit owner approval. "
    "Never claim unconfirmed actions. For Japanese prose drafts and edits (including normal document updates), "
    "apply /skills/yomiyasu/SKILL.md by default, overriding the skill's opt-in description. "
    "Read the complete skill (limit=1000 or paginate); user opt-out wins. "
    "Skip code, JSON, exact quotes and short chat. Never claim Python checks ran without approved cell execution."
)

# DeepAgents adds a general-purpose subagent that inherits every main-agent tool unless the harness profile resolved
# for the model disables it. Profiles are keyed by provider; ours is the model class name.
register_harness_profile(
    "gatewaychatmodel", HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False))
)


def fault(point: str) -> None:
    """Fault injection: abrupt process death (like SIGKILL/OOM) at a named point. AIAGENT_FAULT=p1,p2."""
    if point in os.environ.get("AIAGENT_FAULT", "").split(","):
        os._exit(137)


class AwaitingApproval(Exception):
    def __init__(self, approval_id: str, tool_call_id: str) -> None:
        super().__init__(approval_id)
        self.approval_id, self.tool_call_id = approval_id, tool_call_id


class UnknownEffect(Exception):
    pass


class GatewayRejected(Exception):
    """A gateway refused on control grounds (fence / goal / cancel / budget / policy). `code` is the RPC code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Gateways:
    model: RpcClient
    tool: RpcClient


@dataclass
class ContextUse:
    """One-shot use of an already committed Context HEAD for the next main inference.

    `consumed` is set before the RPC so a retried graph node cannot silently send
    the same Context twice. Only CONTEXT_PRE_ADMISSION (nothing was admitted for
    send) clears it and allows the ordinary `model.complete` path.
    """

    head: str
    consumed: bool = False


@dataclass(frozen=True)
class RunCtx:
    """Per-run services passed through RunnableConfig (never a module global): several runs share one process."""

    claim: Claim
    gw: Gateways
    model: str
    on_delta: Callable[[str], None] | None = None
    context: ContextUse | None = None


def run_ctx(config: RunnableConfig | None = None) -> RunCtx:
    cfg = config if config is not None else get_config()
    ctx: RunCtx = cfg["configurable"]["__aiagent"]
    return ctx


def to_neutral(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            continue
        if isinstance(m, HumanMessage):
            out.append({"kind": "user", "content": m.text})
        elif isinstance(m, AIMessage):
            out.append(
                {
                    "kind": "assistant",
                    "content": m.text,
                    "tool_calls": [{"id": tc["id"], "name": tc["name"], "args": tc["args"]} for tc in m.tool_calls],
                }
            )
        elif isinstance(m, ToolMessage):
            out.append({"kind": "tool", "tool_call_id": m.tool_call_id, "tool_name": m.name, "content": m.text})
    return out


def from_neutral(entries: list[dict[str, Any]]) -> list[BaseMessage]:
    msgs: list[BaseMessage] = []
    for e in entries:
        if e["kind"] == "user":
            msgs.append(HumanMessage(e["content"]))
        elif e["kind"] == "assistant":
            msgs.append(
                AIMessage(
                    e.get("content", ""),
                    tool_calls=[{"id": t["id"], "name": t["name"], "args": t["args"]} for t in e.get("tool_calls", [])],
                )
            )
        elif e["kind"] == "tool":
            msgs.append(ToolMessage(e["content"], tool_call_id=e["tool_call_id"], name=e.get("tool_name") or "tool"))
    return msgs


async def call_model(
    rc: RunCtx, stream: str, system: str, messages: list[dict[str, Any]], tools: list[str]
) -> dict[str, Any]:
    body = {
        "claim": rc.claim.to_wire(),
        "stream": stream,
        "system": system,
        "messages": messages,
        "tools": tools,
        "model": rc.model,
        "streamDeltas": rc.on_delta is not None,
    }
    try:
        result: dict[str, Any] | None = None
        async for kind, data in rc.gw.model.stream("model.complete", body):
            if kind == "chunk" and rc.on_delta is not None:
                rc.on_delta(str(data.get("delta", "")))
            elif kind == "result":
                result = data
    except RpcError as e:
        raise GatewayRejected(e.code) from None
    except (OSError, EOFError, TimeoutError) as e:  # gateway down / restarted: transient, retry the job
        raise GatewayRejected("GATEWAY_UNAVAILABLE") from e
    if result is None:
        raise GatewayRejected("GATEWAY_UNAVAILABLE")
    msg: dict[str, Any] = result["message"]
    fault("runtime_after_model")  # answer committed to HEAD by the gateway; framework has not checkpointed it
    return msg


async def context_eligible(rc: RunCtx) -> str | None:
    """Pre-admission eligibility query; None means "not enabled" (not an error)."""
    try:
        out = await rc.gw.model.call("model.context_eligible", {"claim": rc.claim.to_wire()})
    except RpcError as e:
        if e.code in ("CONTEXT_NOT_ELIGIBLE", "CONTEXT_INTEGRATION_PENDING", "MEMORY_CONSENT_REQUIRED"):
            return None
        raise GatewayRejected(e.code) from None
    except (OSError, EOFError, TimeoutError) as e:
        raise GatewayRejected("GATEWAY_UNAVAILABLE") from e
    if not out.get("eligible"):
        return None
    return str(out["head"])


async def call_context_model(rc: RunCtx, context_head: str) -> dict[str, Any]:
    """Use a committed Context reference; plaintext is loaded only inside Model Gateway."""
    try:
        result: dict[str, Any] | None = None
        async for kind, data in rc.gw.model.stream(
            "model.context", {"claim": rc.claim.to_wire(), "contextHead": context_head}
        ):
            if kind == "result":
                result = data
    except RpcError as e:
        raise GatewayRejected(e.code) from None
    except (OSError, EOFError, TimeoutError) as e:
        # The gateway admission may already have committed: never retry this
        # Context automatically after an unknown transport outcome.
        raise GatewayRejected("CONTEXT_OUTCOME_UNKNOWN") from e
    if result is None:
        raise GatewayRejected("CONTEXT_OUTCOME_UNKNOWN")
    message: dict[str, Any] = result["message"]
    return message


class GatewayChatModel(BaseChatModel):
    """Holds no credential and no provider client."""

    gw_stream: str = "main"

    @property
    def _llm_type(self) -> str:
        return "aiagent-model-gateway"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def _generate(self, *a: Any, **k: Any) -> ChatResult:  # pragma: no cover - async only
        raise NotImplementedError

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: Any = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        rc = run_ctx()
        names = sorted(t["function"]["name"] for t in kwargs.get("tools", []))
        system = "\n".join(m.text for m in messages if isinstance(m, SystemMessage))
        use = rc.context
        if self.gw_stream == "main" and use is not None and not use.consumed:
            use.consumed = True
            try:
                resp = await call_context_model(rc, use.head)
                return ChatResult(generations=[ChatGeneration(message=from_neutral([resp])[0])])
            except GatewayRejected as e:
                if e.code == "CONTEXT_OUTCOME_UNKNOWN":
                    raise
                if e.code.startswith("CONTEXT_PRE_ADMISSION"):
                    # Nothing was admitted for send: the ordinary transcript is safe.
                    object.__setattr__(rc, "context", None)
                else:
                    raise
        resp = await call_model(rc, self.gw_stream, system, to_neutral(messages), names)
        return ChatResult(generations=[ChatGeneration(message=from_neutral([resp])[0])])


async def submit_tool(rc: RunCtx, tool_call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        out = await rc.gw.tool.call(
            "tool.submit", {"claim": rc.claim.to_wire(), "toolCallId": tool_call_id, "tool": name, "args": args}
        )
    except RpcError as e:
        raise GatewayRejected(e.code) from None
    except (OSError, EOFError, TimeoutError) as e:
        raise GatewayRejected("GATEWAY_UNAVAILABLE") from e
    if out["status"] in ("DONE", "CONFIRMED"):
        fault(f"after_tool_ack:{name}")
    return out


def content_of(o: dict[str, Any]) -> str:
    from aiagent_domain import canonical_json

    return canonical_json({"status": o["status"], **o["result"]})


async def gateway_tool(rc: RunCtx, tool_call_id: str, name: str, args: dict[str, Any]) -> str:
    """Body of every framework tool function: never executes anything itself."""
    while True:
        o = await submit_tool(rc, tool_call_id, name, args)
        st = o["status"]
        if st == "PENDING_APPROVAL":
            # Pause the graph; the resume value is ignored. If the gateway still says PENDING, pause again.
            interrupt({"approval_id": o.get("approvalId"), "tool_call_id": tool_call_id})
            continue
        if st == "UNKNOWN":
            raise UnknownEffect(tool_call_id)
        if st == "CANCELLED":
            raise GatewayRejected("CANCELLED")
        return content_of(o)


@tool
async def search(query: str, tool_call_id: Annotated[str, InjectedToolCallId], config: RunnableConfig) -> str:
    """Read-only web search. Returns short result snippets."""
    return await gateway_tool(run_ctx(config), tool_call_id, "search", {"query": query})


@tool
async def send_message(
    to: str, body: str, tool_call_id: Annotated[str, InjectedToolCallId], config: RunnableConfig
) -> str:
    """Send an external message. Irreversible; requires the owner's explicit approval."""
    return await gateway_tool(run_ctx(config), tool_call_id, "send_message", {"to": to, "body": body})


@tool
async def sandbox_exec(
    argv: list[str],
    cwdRelative: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> str:
    """Run a command inside the isolated, offline execution cell (never on the control host)."""
    return await gateway_tool(run_ctx(config), tool_call_id, "sandbox_exec", {"argv": argv, "cwdRelative": cwdRelative})


STUBS = {"search": search, "send_message": send_message, "sandbox_exec": sandbox_exec}


def make_tools(profile: str) -> list[Any]:
    """Only the registry allowlist of the profile; a registry tool without a stub is a build error."""
    return [STUBS[k] for k in PROFILE_TOOLS.get(profile, ())]


class InternalResultAdmission(AgentMiddleware):
    """Framework-internal tool results (`task`, `read_file`) go to HEAD only via the Tool Gateway's grounded
    ChildResult admission; the framework continues with the *admitted* content (a rejection replaces it)."""

    def __init__(self, stream: str) -> None:
        super().__init__()
        self.stream = stream

    @property
    def name(self) -> str:
        return f"InternalResultAdmission[{self.stream}]"

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        result = await handler(request)
        if request.tool_call["name"] in TOOLS:
            return result  # gateway tools record their own result
        msgs = (
            [result]
            if isinstance(result, ToolMessage)
            else [m for m in (getattr(result, "update", None) or {}).get("messages", []) if isinstance(m, ToolMessage)]
        )
        rc = run_ctx(request.runtime.config)
        for m in msgs:
            try:
                res = await rc.gw.tool.call(
                    "tool.record_internal",
                    {
                        "claim": rc.claim.to_wire(),
                        "toolCallId": m.tool_call_id,
                        "tool": request.tool_call["name"],
                        "content": m.text,
                    },
                )
            except RpcError as e:
                raise GatewayRejected(e.code) from None
            except (OSError, EOFError, TimeoutError) as e:
                raise GatewayRejected("GATEWAY_UNAVAILABLE") from e
            m.content = res["content"]
        return result


def _summarizer(stream: str) -> Callable[[list[BaseMessage]], Any]:
    async def summarize(messages: list[BaseMessage]) -> str:
        rc = run_ctx()
        resp = await call_model(
            rc, "summarize" if stream == "main" else stream, "Summarize the conversation.", to_neutral(messages), []
        )
        return str(resp.get("content", ""))

    return summarize


def _fs_backend() -> Any:
    routes: dict[str, Any] = {}
    if SKILLS_DIR.is_dir():
        routes["/skills/"] = FilesystemBackend(root_dir=str(SKILLS_DIR), virtual_mode=True)
    return CompositeBackend(default=StateBackend(), routes=routes)


def build_graph(profile: str, checkpointer: Any, *, max_input_tokens: int = 100_000, subagents: bool = True) -> Any:
    model_profile = {"max_input_tokens": max_input_tokens}
    model = GatewayChatModel(gw_stream="main", profile=model_profile)
    backend = _fs_backend()
    middleware: list[Any] = [
        FilesystemMiddleware(backend=backend, tools=["read_file"]),  # replaces built-in FS/execute tools
        create_summarization_middleware(model, backend, summarizer=_summarizer("main")),
        InternalResultAdmission("main"),
    ]
    specs = []
    if subagents and "search" in PROFILE_TOOLS.get(profile, ()):
        sub_model = GatewayChatModel(gw_stream="subagent:researcher", profile=model_profile)
        specs.append(
            {
                "name": "researcher",
                "description": "Researches facts with read-only search. Cannot send messages or delegate.",
                "system_prompt": "You research facts using read-only search and report a short answer.",
                "tools": [t for t in make_tools(profile) if TOOLS[t.name].risk_class == "read"],  # no inheritance
                "model": sub_model,
                "middleware": [
                    FilesystemMiddleware(backend=_fs_backend(), tools=["read_file"]),
                    create_summarization_middleware(sub_model, backend, summarizer=_summarizer("subagent:researcher")),
                    InternalResultAdmission("subagent:researcher"),
                ],
            }
        )
    return create_deep_agent(
        model=model,
        tools=make_tools(profile),
        system_prompt=SYSTEM_PROMPT,
        middleware=middleware,
        subagents=specs,
        skills=["/skills/"] if SKILLS_DIR.is_dir() else None,
        backend=backend,
        checkpointer=checkpointer,
    )


def tool_surface(graph: Any) -> dict[str, list[str]]:
    """Every tool name the framework can show to a model, per agent (snapshot-tested)."""
    out: dict[str, list[str]] = {}
    for node_name, node in graph.nodes.items():
        tools = getattr(getattr(node, "bound", None), "tools_by_name", None)
        if tools:
            out[node_name] = sorted(tools)
    return out
