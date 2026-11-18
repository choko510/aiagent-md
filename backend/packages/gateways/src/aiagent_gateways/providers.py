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
import ssl
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


# Maximum output tokens requested from a real provider. Budget reservations must cover this bound, not a smaller
# arbitrary headroom, or a long answer can exceed the reserved amount.
MAX_OUTPUT_TOKENS = 4096


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
    if stream == "verify":
        override = os.environ.get("AIAGENT_FAKE_NLI_VERDICT")
        if override in ("SUPPORTED", "REFUTED", "UNVERIFIED"):
            return {"content": json.dumps({"verdict": override})}
        last_user = next((str(m.get("content", "")) for m in reversed(messages) if m.get("kind") == "user"), "")
        try:
            payload = json.loads(last_user)
            quote = str(payload.get("quote", ""))
            source_text = str(payload.get("sourceText", ""))
        except (ValueError, TypeError, AttributeError):
            return {"content": json.dumps({"verdict": "UNVERIFIED"})}
        if not quote or quote not in source_text or "__FORCE_NLI_REFUTE__" in source_text:
            return {"content": json.dumps({"verdict": "REFUTED"})}
        return {"content": json.dumps({"verdict": "SUPPORTED"})}
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
    # Deterministic nihongo fixture for the gateway/Agent runtime integration tests: chat uses only the embedded
    # Lite, deliverables read the full SKILL.md, technical documents add one reference, and code/JSON stay untouched.
    # No model-side bypass: these calls must be proposed, admitted, and committed by the real gateways.
    if "nihongo fixture" in first:
        if "nihongoを使わない" in first or "nihongo を使わない" in first:
            return {"content": "指定に従って通常回答しました。"}
        if "read_file" not in tools:
            return {"content": "error: read_file not exposed"}
        pages = [m for m in messages if m.get("kind") == "tool" and m.get("tool_name") == "read_file"]
        deliverable = any(
            key in first
            for key in ("文章", "推敲", "メール", "お知らせ", "提案", "スライド", "議事録", "readme", "技術文書")
        )
        if not deliverable:
            if "コード" in first or "json" in first:
                return {"content": "コードとJSONは変更していません。"}
            return {"content": "通常会話としてLiteのみで回答しました。"}
        technical = "readme" in first or "技術文書" in first
        path = "/skills/nihongo/SKILL.md"
        if not pages:
            return _call("read_file", file_path=path, offset=0, limit=100)
        if len(pages) == 1:
            first_page = str(pages[0].get("content", ""))
            if not first_page.startswith("@@ lines ") or "next offset 100" not in first_page:
                return {"content": "error: first skill page rejected"}
            return _call("read_file", file_path=path, offset=100, limit=1000)
        if not all(str(m.get("content", "")).startswith("@@ lines ") for m in pages):
            return {"content": "error: skill page rejected"}
        if technical and len(pages) == 2:
            return _call("read_file", file_path="/skills/nihongo/references/technical-writing.md", offset=0, limit=1000)
        if technical and "# 技術文書" not in str(pages[2].get("content", "")):
            return {"content": "error: technical reference rejected"}
        if technical:
            return {"content": "更新済み：技術文書の用語と手順を保って読みやすく整えました。"}
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
    if "compose browser" in first:
        navs = [m for m in messages if m.get("kind") == "tool" and m.get("tool_name") == "browser_navigate"]
        if not navs:
            return _call("browser_navigate", url="http://portal:8099/portal/entry")
        first_nav = json.loads(str(navs[0].get("content", "{}")))
        sid = str(first_nav.get("sessionId") or "")
        if "browser_submit" not in r:
            return _call("browser_submit", sessionId=sid, kind="test_id", selector="enter-portal")
        if len(navs) == 1:
            return _call("browser_navigate", url="http://portal:8099/portal/dashboard", sessionId=sid)
        last_nav = json.loads(str(navs[-1].get("content", "{}")))
        obs = str(last_nav.get("observation") or "")
        return {"content": f"done:compose-browser:{last_nav.get('state')}:{obs[:120]}"}
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
        # A real provider bills the system prompt too; excluding it hid budget under-reservation in tests.
        chars = len(system) + sum(len(str(m.get("content", ""))) for m in msgs)
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
        verify: bool | ssl.SSLContext = True,
    ) -> None:
        self.key, self.proxy, self.timeout_s, self.verify = key, proxy, timeout_s, verify
        self.proxy_token = proxy_token  # identifies the Model Gateway to the Trusted Egress (standing destination)
        self.url = url or self.URL

    def _client(self) -> httpx.AsyncClient:
        return _egress_http_client(self.proxy, self.proxy_token, self.timeout_s, self.verify)

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
            "max_tokens": MAX_OUTPUT_TOKENS,
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
        message_stop_seen = False
        try:
            async with self._client() as client:
                async with client.stream("POST", self.url, headers=headers, json=body) as resp:
                    if resp.status_code in (429, 529):
                        raise ProviderError("RATE_LIMITED", True)
                    if resp.status_code >= 500:
                        # A 5xx may be emitted after the service accepted/billed the
                        # request. Do not infer non-execution from an error status.
                        raise ProviderError("OUTCOME_UNKNOWN", False)
                    if resp.status_code >= 400:
                        raise ProviderError(f"HTTP_{resp.status_code}", False)
                    current: dict[int, str] = {}
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            ev = json.loads(line[5:])
                        except ValueError as exc:
                            raise ProviderError("INVALID_STREAM", False) from exc
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
                            message_stop_seen = True
                            yield ProviderEvent("stop")
        except httpx.TransportError as exc:
            # Once request bytes may have left the Gateway, transport loss is
            # UNKNOWN. Never feed it to the ordinary transient retry path.
            raise ProviderError("OUTCOME_UNKNOWN", False) from exc
        if not message_stop_seen:
            raise ProviderError("INCOMPLETE_STREAM", False)


def _egress_http_client(
    proxy_url: str | None,
    proxy_token: Secret | None,
    timeout_s: float,
    verify: bool | ssl.SSLContext,
) -> httpx.AsyncClient:
    proxy = None
    if proxy_url:
        tok = proxy_token.reveal() if proxy_token is not None else ""
        proxy = httpx.Proxy(proxy_url, headers={"Proxy-Authorization": f"Aiagent model_gateway:{tok}"})
    return httpx.AsyncClient(proxy=proxy, timeout=timeout_s, trust_env=False, verify=verify)


# ------------------------------------------------------------------------------------------------ OpenAI / Compatible
class OpenAIProvider:
    """OpenAI Chat Completions & OpenAI-compatible (Ollama/vLLM/LiteLLM) streaming provider."""

    URL = "https://api.openai.com/v1/chat/completions"

    def __init__(
        self,
        key: Secret | None,
        proxy: str | None,
        *,
        proxy_token: Secret | None = None,
        url: str | None = None,
        provider_name: str = "openai",
        external_recipient: bool = True,
        max_context_chars: int = 400_000,
        timeout_s: float = 120.0,
        verify: bool | ssl.SSLContext = True,
    ) -> None:
        self.key = key
        self.proxy = proxy
        self.proxy_token = proxy_token
        self.url = url or self.URL
        self.provider_name = provider_name
        self.external_recipient = external_recipient
        self.max_context_chars = max_context_chars
        self.timeout_s = timeout_s
        self.verify = verify

    def capability(self, model: str) -> Capability:
        del model
        return Capability(True, True, self.max_context_chars, self.provider_name, self.external_recipient)

    @staticmethod
    def _messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            if m["kind"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["kind"] == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
                tcs = m.get("tool_calls") or []
                if tcs:
                    msg["tool_calls"] = [
                        {
                            "id": t["id"],
                            "type": "function",
                            "function": {"name": t["name"], "arguments": json.dumps(t["args"])},
                        }
                        for t in tcs
                    ]
                out.append(msg)
            elif m["kind"] == "tool":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": m["tool_call_id"],
                        "content": m["content"],
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
        del stream
        body: dict[str, Any] = {
            "model": model,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": self._messages(system, messages),
        }
        # Bound output to the same value the budget reserves. o-series rejects `max_tokens`.
        if model.startswith(("o1", "o3", "o4")):
            body["max_completion_tokens"] = MAX_OUTPUT_TOKENS
        else:
            body["max_tokens"] = MAX_OUTPUT_TOKENS
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]},
                }
                for t in tools
            ]
        headers = {"content-type": "application/json"}
        if self.key is not None:
            headers["authorization"] = "Bearer " + self.key.reveal()
        done_seen = False
        pending_tools: dict[int, dict[str, Any]] = {}
        open_tools: dict[int, str] = {}
        try:
            async with _egress_http_client(self.proxy, self.proxy_token, self.timeout_s, self.verify) as client:
                async with client.stream("POST", self.url, headers=headers, json=body) as resp:
                    if resp.status_code in (429, 529):
                        raise ProviderError("RATE_LIMITED", True)
                    if resp.status_code >= 500:
                        raise ProviderError("OUTCOME_UNKNOWN", False)
                    if resp.status_code >= 400:
                        raise ProviderError(f"HTTP_{resp.status_code}", False)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            done_seen = True
                            if pending_tools:
                                raise ProviderError("INVALID_STREAM", False)
                            for idx in sorted(open_tools):
                                yield ProviderEvent("tool_end", tool_id=open_tools.pop(idx))
                            yield ProviderEvent("stop")
                            continue
                        try:
                            ev = json.loads(raw)
                        except ValueError as exc:
                            raise ProviderError("INVALID_STREAM", False) from exc
                        usage = ev.get("usage")
                        if isinstance(usage, dict):
                            u_out: dict[str, int] = {}
                            if isinstance(usage.get("prompt_tokens"), int):
                                u_out["input_tokens"] = usage["prompt_tokens"]
                            if isinstance(usage.get("completion_tokens"), int):
                                u_out["output_tokens"] = usage["completion_tokens"]
                            if u_out:
                                yield ProviderEvent("usage", usage=u_out)
                        choices = ev.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(reasoning, str) and reasoning:
                            yield ProviderEvent("reasoning", text=reasoning)
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            yield ProviderEvent("text", text=content)
                        for tc in delta.get("tool_calls") or []:
                            idx = int(tc.get("index", 0))
                            fn = tc.get("function") or {}
                            if idx in open_tools:
                                args_part = fn.get("arguments")
                                if isinstance(args_part, str) and args_part:
                                    yield ProviderEvent("tool_args", tool_id=open_tools[idx], text=args_part)
                                continue
                            buf = pending_tools.setdefault(
                                idx,
                                {"id": None, "name": "", "args": []},
                            )
                            if tc.get("id"):
                                buf["id"] = str(tc["id"])
                            if fn.get("name"):
                                buf["name"] = str(fn["name"])
                            args_part = fn.get("arguments")
                            if isinstance(args_part, str) and args_part:
                                buf["args"].append(args_part)
                            if buf["name"]:
                                tid = str(buf["id"] or f"call_{uuid.uuid4().hex[:16]}")
                                open_tools[idx] = tid
                                yield ProviderEvent("tool_start", tool_id=tid, tool_name=buf["name"])
                                for buffered_arg in buf["args"]:
                                    if buffered_arg:
                                        yield ProviderEvent("tool_args", tool_id=tid, text=buffered_arg)
                                pending_tools.pop(idx, None)
                        if choice.get("finish_reason") is not None:
                            if pending_tools:
                                raise ProviderError("INVALID_STREAM", False)
                            for idx in sorted(open_tools):
                                yield ProviderEvent("tool_end", tool_id=open_tools.pop(idx))
        except httpx.TransportError as exc:
            raise ProviderError("OUTCOME_UNKNOWN", False) from exc
        if not done_seen:
            raise ProviderError("INCOMPLETE_STREAM", False)


# ------------------------------------------------------------------------------------------------ Gemini
class GeminiProvider:
    """Google Gemini `:streamGenerateContent?alt=sse` streaming provider (header API key, never URL query)."""

    URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse"

    def __init__(
        self,
        key: Secret,
        proxy: str | None,
        *,
        proxy_token: Secret | None = None,
        url_template: str | None = None,
        provider_name: str = "gemini",
        external_recipient: bool = True,
        max_context_chars: int = 800_000,
        timeout_s: float = 120.0,
        verify: bool | ssl.SSLContext = True,
    ) -> None:
        self.key = key
        self.proxy = proxy
        self.proxy_token = proxy_token
        self.url_template = url_template or self.URL_TEMPLATE
        self.provider_name = provider_name
        self.external_recipient = external_recipient
        self.max_context_chars = max_context_chars
        self.timeout_s = timeout_s
        self.verify = verify

    def capability(self, model: str) -> Capability:
        del model
        return Capability(True, True, self.max_context_chars, self.provider_name, self.external_recipient)

    @staticmethod
    def _contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m["kind"] == "user":
                out.append({"role": "user", "parts": [{"text": m["content"]}]})
            elif m["kind"] == "assistant":
                parts: list[dict[str, Any]] = [{"text": m["content"]}] if m.get("content") else []
                for tc in m.get("tool_calls") or []:
                    parts.append({"functionCall": {"name": tc["name"], "args": tc["args"]}})
                out.append({"role": "model", "parts": parts or [{"text": ""}]})
            elif m["kind"] == "tool":
                out.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": m.get("tool_name") or "tool",
                                    "response": {"content": m["content"]},
                                }
                            }
                        ],
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
        del stream
        url = self.url_template.format(model=model)
        body: dict[str, Any] = {
            "contents": self._contents(messages),
            "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}
                        for t in tools
                    ]
                }
            ]
        headers = {"x-goog-api-key": self.key.reveal(), "content-type": "application/json"}
        finish_seen = False
        try:
            async with _egress_http_client(self.proxy, self.proxy_token, self.timeout_s, self.verify) as client:
                async with client.stream("POST", url, headers=headers, json=body) as resp:
                    if resp.status_code in (429, 529):
                        raise ProviderError("RATE_LIMITED", True)
                    if resp.status_code >= 500:
                        raise ProviderError("OUTCOME_UNKNOWN", False)
                    if resp.status_code >= 400:
                        raise ProviderError(f"HTTP_{resp.status_code}", False)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            finish_seen = True
                            yield ProviderEvent("stop")
                            continue
                        try:
                            ev = json.loads(raw)
                        except ValueError as exc:
                            raise ProviderError("INVALID_STREAM", False) from exc
                        um = ev.get("usageMetadata")
                        if isinstance(um, dict):
                            u_out: dict[str, int] = {}
                            if isinstance(um.get("promptTokenCount"), int):
                                u_out["input_tokens"] = um["promptTokenCount"]
                            if isinstance(um.get("candidatesTokenCount"), int):
                                u_out["output_tokens"] = um["candidatesTokenCount"]
                            if u_out:
                                yield ProviderEvent("usage", usage=u_out)
                        for cand in ev.get("candidates") or []:
                            content = cand.get("content") or {}
                            for part in content.get("parts") or []:
                                if not isinstance(part, dict):
                                    continue
                                if part.get("thought") is True and isinstance(part.get("text"), str):
                                    yield ProviderEvent("reasoning", text=part["text"])
                                elif isinstance(part.get("text"), str) and part["text"]:
                                    yield ProviderEvent("text", text=part["text"])
                                elif isinstance(part.get("functionCall"), dict):
                                    fc = part["functionCall"]
                                    tid = f"call_{uuid.uuid4().hex[:16]}"
                                    tname = str(fc.get("name") or "")
                                    if not tname:
                                        raise ProviderError("INVALID_STREAM", False)
                                    yield ProviderEvent("tool_start", tool_id=tid, tool_name=tname)
                                    yield ProviderEvent("tool_args", tool_id=tid, text=json.dumps(fc.get("args") or {}))
                                    yield ProviderEvent("tool_end", tool_id=tid)
                            if cand.get("finishReason") is not None:
                                finish_seen = True
                                yield ProviderEvent("stop")
        except httpx.TransportError as exc:
            raise ProviderError("OUTCOME_UNKNOWN", False) from exc
        if not finish_seen:
            raise ProviderError("INCOMPLETE_STREAM", False)
