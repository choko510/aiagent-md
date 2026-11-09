"""Deterministic FakeModel policies shared by both candidates (the "same fixture").

A policy is a pure function of the neutral request (stream, messages, tool names). It is state-based,
not call-count based, so a re-inference after a crash proposes the same *logical* action; tool_call ids
are minted fresh by the gateway, as a real provider would.
"""

from __future__ import annotations

import json
from typing import Any, Callable

Policy = Callable[[str, list[dict[str, Any]], list[str]], dict[str, Any]]

USER_NOTIFY = "Find the widget price and notify alice."
USER_PARALLEL = "Notify alice and bob at once."
USER_DELEGATE = "Delegate the price research, then answer."
SUBTASK = "[subtask] find the widget price"


def _results(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for m in messages:
        if m.get("kind") == "tool":
            try:
                out[m.get("tool_name") or "?"] = json.loads(m["content"])
            except (TypeError, ValueError):
                out[m.get("tool_name") or "?"] = {"raw": m.get("content")}
    return out


def _call(name: str, **args: Any) -> dict[str, Any]:
    return {"content": "", "tool_calls": [{"name": name, "args": args}]}


def notify(stream: str, messages: list[dict[str, Any]], tools: list[str]) -> dict[str, Any]:
    r = _results(messages)
    if "send_message" in r:
        return {"content": f"done:{r['send_message'].get('status')}"}
    if "search" in r:
        return _call("send_message", to="alice", body="price=42")
    return _call("search", query="widget price")


def parallel_writes(stream: str, messages: list[dict[str, Any]], tools: list[str]) -> dict[str, Any]:
    if any(m.get("kind") == "tool" for m in messages):
        return {"content": "done:parallel"}
    return {
        "content": "",
        "tool_calls": [
            {"name": "send_message", "args": {"to": "alice", "body": "hi"}},
            {"name": "send_message", "args": {"to": "bob", "body": "hi"}},
        ],
    }


DELEGATE_TOOLS = ("task", "delegate_task", "delegate")


def delegate(stream: str, messages: list[dict[str, Any]], tools: list[str]) -> dict[str, Any]:
    if stream.startswith("subagent"):
        r = _results(messages)
        if "search" in r:
            return {"content": "child-result: widget price is 42"}
        return _call("search", query="widget price")
    if any(m.get("kind") == "tool" for m in messages):
        return {"content": "done:delegated"}
    name = next((t for t in tools if t in DELEGATE_TOOLS), None)
    if name is None:
        return {"content": f"error:no delegate tool in {sorted(tools)}"}
    if name == "task":  # DeepAgents SubAgentMiddleware
        return _call("task", description=SUBTASK, subagent_type="researcher")
    return _call(name, agent_name="researcher", task=SUBTASK)  # pydantic-ai-harness SubAgents


POLICIES: dict[str, Policy] = {"notify": notify, "parallel_writes": parallel_writes, "delegate": delegate}
USER_TEXT = {"notify": USER_NOTIFY, "parallel_writes": USER_PARALLEL, "delegate": USER_DELEGATE}

# Long prior conversation that forces the framework's compaction to fire (same bytes for A and B).
LONG_HISTORY = [
    {"kind": "user" if i % 2 == 0 else "assistant", "content": f"[history {i}] " + ("lorem ipsum " * 400)}
    for i in range(12)
]


def is_summarization_request(messages: list[dict[str, Any]]) -> bool:
    tail = " ".join(str(m.get("content", ""))[:4000] for m in messages[-2:] + messages[:1]).lower()
    return "summar" in tail and "[history" in " ".join(str(m.get("content", "")) for m in messages)
