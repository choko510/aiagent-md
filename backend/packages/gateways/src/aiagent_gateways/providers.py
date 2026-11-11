"""Model providers behind the Model Gateway. Providers emit a neutral event stream; the gateway owns retries,
fallback, budget, privacy gating and the response checkpoint. Credentials are injected only here.

* FakeModelProvider - deterministic, state-based policies (STEP 2 fixture semantics) so a re-inference after a crash
  proposes the same logical action; tool_call ids are minted fresh per call like real providers. It also streams
  private reasoning and partial tool arguments so the Stream Privacy Gate is exercised for real.
* AnthropicProvider - Messages API over HTTPS (streaming SSE) through the Trusted Egress proxy. Never used in CI.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
from aiagent_core.config import Secret


@dataclass(frozen=True)
class Capability:
    tools: bool
    streaming: bool
    max_context_chars: int
    provider: str
    external_recipient: bool  # sends tenant data to a third party (needs recipient consent)


@dataclass
class ProviderEvent:
    type: str  # text | reasoning | tool_start | tool_args | tool_end | usage | stop
    text: str = ""
    tool_id: str = ""
    tool_name: str = ""
    usage: dict[str, int] = field(default_factory=dict)


class ProviderError(Exception):
    def __init__(self, code: str, retryable: bool) -> None:
        super().__init__(code)
        self.code, self.retryable = code, retryable


# ------------------------------------------------------------------------------------------------ FakeModel
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


def fake_policy(stream: str, messages: list[dict[str, Any]], tools: list[str]) -> dict[str, Any]:
    first = next((m.get("content", "") for m in messages if m.get("kind") == "user"), "").lower()
    r = _results(messages)
    if stream == "summarize":
        return {"content": f"SUMMARY of {len(messages)} earlier messages (goal: widget price)."}
    if stream.startswith("subagent"):
        if "search" in r:
            return {"content": "child-result: widget price is 42"}
        return _call("search", query="widget price")
    steer_idx = max(
        (
            i
            for i, m in enumerate(messages)
            if m.get("kind") == "user" and "goal revised" in str(m.get("content", "")).lower()
        ),
        default=None,
    )
    if steer_idx is not None:
        after = _results(messages[steer_idx + 1 :])
        if "bob" in str(messages[steer_idx].get("content", "")).lower():
            if "send_message" in after:
                return {"content": f"done:{after['send_message'].get('status')}"}
            return _call("send_message", to="bob", body="price=42")
        return {"content": "answer: goal revised, nothing sent"}
    # Deterministic two-page skill read for the gateway/Agent runtime integration test.
    # No model-side bypass: these calls must be proposed, admitted, and committed by the real gateways.
    if "yomiyasu fixture" in first:
        if "yomiyasuを使わない" in first:
            return {"content": "指定に従って通常回答しました。"}
        pages = [m for m in messages if m.get("kind") == "tool" and m.get("tool_name") == "read_file"]
        path = "/skills/yomiyasu/SKILL.md"
        if "read_file" not in tools:
            return {"content": "error: read_file not exposed"}
        if not pages:
            return _call("read_file", file_path=path, offset=0, limit=8)
        if len(pages) == 1:
            if not str(pages[0].get("content", "")).startswith("@@ lines "):
                return {"content": "error: first skill page rejected"}
            return _call("read_file", file_path=path, offset=8, limit=1000)
        if not all(str(m.get("content", "")).startswith("@@ lines ") for m in pages):
            return {"content": "error: skill page rejected"}
        return {"content": "推敲済み：日本語の文章を読みやすく整えました。"}
    if "delegate" in first:
        if any(m.get("kind") == "tool" for m in messages):
            return {"content": "done:delegated"}
        if "task" not in tools:
            return {"content": f"error:no delegate tool in {sorted(tools)}"}
        return _call("task", description="[subtask] find the widget price", subagent_type="researcher")
    if "parallel" in first:
        if any(m.get("kind") == "tool" for m in messages):
            return {"content": "done:parallel"}
        return {
            "content": "",
            "tool_calls": [
                {"name": "send_message", "args": {"to": "alice", "body": "hi"}},
                {"name": "send_message", "args": {"to": "bob", "body": "hi"}},
            ],
        }
    if "sandbox" in first:
        if "sandbox_exec" in r:
            return {"content": f"done:sandbox:{r['sandbox_exec'].get('status')}"}
        return _call("sandbox_exec", argv=["python3", "-c", "print(6*7)"], cwdRelative=".")
    if "notify" in first:
        if "send_message" in r:
            return {"content": f"done:{r['send_message'].get('status')}"}
        if "search" in r:
            return _call("send_message", to="alice", body="price=42")
        return _call("search", query="widget price")
    if "search" in first:
        if "search" in r:
            return {"content": "The widget price is 42."}
        return _call("search", query="widget price")
    return {"content": f"answer: {first[:80]}"}


class FakeModelProvider:
    def __init__(self, key: Secret | None = None) -> None:
        self.key = key  # held to prove it never leaves the gateway

    def capability(self, model: str) -> Capability:
        return Capability(True, True, 400_000, "fake", False)

    async def stream(
        self,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: str = "main",
    ) -> AsyncIterator[ProviderEvent]:
        msgs = messages
        names = [t["name"] for t in tools]
        out = fake_policy(stream, msgs, names)
        if os.environ.get("AIAGENT_FAKE_PROVIDER_FAIL"):
            code = os.environ["AIAGENT_FAKE_PROVIDER_FAIL"]
            raise ProviderError(code, code in ("RATE_LIMITED", "UNAVAILABLE"))
        yield ProviderEvent(
            "reasoning",
            text="(private chain of thought: user=" + (msgs[0].get("content", "")[:20] if msgs else "") + ")",
        )
        text = out.get("content", "")
        for i in range(0, len(text), 8):
            yield ProviderEvent("text", text=text[i : i + 8])
        for tc in out.get("tool_calls", []):
            tid = "call_" + uuid.uuid4().hex[:16]
            raw = json.dumps(tc["args"])
            yield ProviderEvent("tool_start", tool_id=tid, tool_name=tc["name"])
            for i in range(0, len(raw), 5):
                yield ProviderEvent("tool_args", tool_id=tid, text=raw[i : i + 5])
            yield ProviderEvent("tool_end", tool_id=tid)
        chars = sum(len(str(m.get("content", ""))) for m in msgs)
        yield ProviderEvent("usage", usage={"input_tokens": chars // 4 + 1, "output_tokens": len(text) // 4 + 1})
        yield ProviderEvent("stop")


# ------------------------------------------------------------------------------------------------ Anthropic
class AnthropicProvider:
    """Anthropic Messages API (streaming). The key comes from the gateway's secret file only; HTTP goes through the
    Trusted Egress proxy (`proxy`), never directly. Not exercised in CI (no credential)."""

    URL = "https://api.anthropic.com/v1/messages"

    def __init__(
        self,
        key: Secret,
        proxy: str | None,
        *,
        proxy_token: Secret | None = None,
        url: str | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        self.key, self.proxy, self.timeout_s = key, proxy, timeout_s
        self.proxy_token = proxy_token  # identifies the Model Gateway to the Trusted Egress (standing destination)
        self.url = url or self.URL

    def _client(self) -> httpx.AsyncClient:
        proxy = None
        if self.proxy:
            tok = self.proxy_token.reveal() if self.proxy_token is not None else ""
            proxy = httpx.Proxy(self.proxy, headers={"Proxy-Authorization": f"Aiagent model_gateway:{tok}"})
        return httpx.AsyncClient(proxy=proxy, timeout=self.timeout_s, trust_env=False)

    def capability(self, model: str) -> Capability:
        return Capability(True, True, 600_000, "anthropic", True)

    @staticmethod
    def _messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m["kind"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["kind"] == "assistant":
                blocks: list[dict[str, Any]] = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                blocks += [
                    {"type": "tool_use", "id": t["id"], "name": t["name"], "input": t["args"]}
                    for t in m.get("tool_calls", [])
                ]
                out.append({"role": "assistant", "content": blocks})
            elif m["kind"] == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}],
                    }
                )
        return out

    async def stream(
        self,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: str = "main",
    ) -> AsyncIterator[ProviderEvent]:
        body = {
            "model": model,
            "max_tokens": 4096,
            "stream": True,
            "system": system,
            "messages": self._messages(messages),
            "tools": [
                {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools
            ],
        }
        headers = {
            "x-api-key": self.key.reveal(),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        async with self._client() as client:
            async with client.stream("POST", self.url, headers=headers, json=body) as resp:
                if resp.status_code in (429, 529):
                    raise ProviderError("RATE_LIMITED", True)
                if resp.status_code >= 500:
                    raise ProviderError("UNAVAILABLE", True)
                if resp.status_code >= 400:
                    raise ProviderError(f"HTTP_{resp.status_code}", False)
                current: dict[int, str] = {}
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    ev = json.loads(line[5:])
                    t = ev.get("type")
                    if t == "content_block_start" and ev["content_block"]["type"] == "tool_use":
                        current[ev["index"]] = ev["content_block"]["id"]
                        yield ProviderEvent(
                            "tool_start", tool_id=ev["content_block"]["id"], tool_name=ev["content_block"]["name"]
                        )
                    elif t == "content_block_delta":
                        d = ev["delta"]
                        if d["type"] == "text_delta":
                            yield ProviderEvent("text", text=d["text"])
                        elif d["type"] == "thinking_delta":
                            yield ProviderEvent("reasoning", text=d.get("thinking", ""))
                        elif d["type"] == "input_json_delta":
                            yield ProviderEvent("tool_args", tool_id=current[ev["index"]], text=d["partial_json"])
                    elif t == "content_block_stop" and ev["index"] in current:
                        yield ProviderEvent("tool_end", tool_id=current.pop(ev["index"]))
                    elif t == "message_delta" and "usage" in ev:
                        yield ProviderEvent("usage", usage={"output_tokens": ev["usage"].get("output_tokens", 0)})
                    elif t == "message_start":
                        u = ev["message"].get("usage", {})
                        yield ProviderEvent("usage", usage={"input_tokens": u.get("input_tokens", 0)})
                    elif t == "message_stop":
                        yield ProviderEvent("stop")
