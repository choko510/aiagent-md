"""Phase 5 end-to-end & security boundary tests: Multi-Provider Router/Circuit Breaker,
SearchRouter & SSRF-safe HttpFetch via Sentinel, Gated LLM NLI Compaction, and HTTPS Browser
with encrypted storageState + Human Takeover API over real PostgreSQL 16 & Chromium.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import json
import secrets
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from aiagent_api.app import _takeover_via_forward_proxy, create_app
from aiagent_browser import PlaywrightBrowserWorker
from aiagent_core import admin, commands, control, memory_tombstones, memory_writer
from aiagent_core.commands import Scope
from aiagent_core.config import Secret
from aiagent_core.db import Database
from aiagent_domain.rpcwire import RpcError
from aiagent_gateways.browser_executor import BrowserSessionExecutor
from aiagent_gateways.context_keys import KeyRing
from aiagent_gateways.executors import ExecutorFailed
from aiagent_gateways.model_gateway import ModelGateway, Route, RouteBreaker, RouteRates, SealedModelRequest
from aiagent_gateways.providers import MAX_OUTPUT_TOKENS, GeminiProvider, OpenAIProvider, ProviderError, ProviderEvent
from aiagent_gateways.sentinel_port import LocalSentinel
from aiagent_gateways.tool_gateway import ToolGateway
from aiagent_gateways.web_fetch import HttpFetchExecutor, SearchRouterExecutor, safe_http_get
from aiagent_sentinel.authority import EgressConfig, Sentinel
from aiagent_sentinel.egress import EgressProxy
from conftest import PgEnv
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

pytestmark = pytest.mark.pg


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _claim_new(
    dbs: dict[str, Database],
    scope: Scope,
    goal: str,
    *,
    profile: str = "default",
    allowed_models: list[str] | None = None,
) -> tuple[str, control.Claim]:
    ids = commands.submit_task(dbs["api"], scope, goal, profile=profile)
    if allowed_models is not None:
        with dbs["owner"].tx() as c:
            c.execute("UPDATE agent.runs SET allowed_models=%s WHERE id=%s", (allowed_models, ids["runId"]))
    claim = control.claim_run(dbs["runtime"], scope.tenant_id, ids["runId"], f"p5-worker-{ids['runId'][:8]}")
    return ids["taskId"], claim


def _propose(dbs: dict[str, Database], claim: control.Claim, call_id: str, tool: str, args: dict[str, Any]) -> None:
    with dbs["model_gateway"].tx() as c:
        control.append_head(
            c,
            claim,
            "assistant",
            {"kind": "assistant", "content": "", "tool_calls": [{"id": call_id, "name": tool, "args": args}]},
            writer="model_gateway",
            request_key=secrets.token_hex(32),
        )


def _make_self_signed_tls(tmp_dir: Path, hostname: str) -> tuple[Path, Path, ssl.SSLContext, ssl.SSLContext]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=7))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_dir / f"{hostname}.crt"
    key_path = tmp_dir / f"{hostname}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    srv_ctx.load_cert_chain(str(cert_path), str(key_path))
    cli_ctx = ssl.create_default_context(cafile=str(cert_path))
    return cert_path, key_path, srv_ctx, cli_ctx


# ---------------------------------------------------------------- Phase 5A: OpenAI / Gemini / Router / Breaker
def test_openai_and_gemini_streaming_providers_cost_recording_and_unknown_guard(
    dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    seen_requests: list[dict[str, Any]] = []

    class ProviderHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            body = json.loads(raw) if raw else {}
            seen_requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            if self.path == "/v1/fail500":
                self.send_response(500)
                self.end_headers()
                return
            if self.path == "/v1/chat/completions":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                chunks = [
                    {"choices": [{"delta": {"reasoning_content": "INTERNAL_SECRET_THOUGHT"}, "finish_reason": None}]},
                    {"choices": [{"delta": {"content": "Price check "}, "finish_reason": None}]},
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call_oa_1",
                                            "function": {"name": "search", "arguments": '{"qu'},
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ]
                    },
                    {
                        "choices": [
                            {
                                "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'ery":"laptop"}'}}]},
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
                    },
                ]
                for item in chunks:
                    self.wfile.write(f"data: {json.dumps(item)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                return
            if self.path.startswith("/gemini/"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                evs = [
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {"thought": True, "text": "hidden gemini chain"},
                                        {"text": "Verified: "},
                                        {"functionCall": {"name": "search", "args": {"query": "hotel"}}},
                                    ]
                                },
                                "finishReason": "STOP",
                            }
                        ],
                        "usageMetadata": {"promptTokenCount": 200, "candidatesTokenCount": 50},
                    }
                ]
                for item in evs:
                    self.wfile.write(f"data: {json.dumps(item)}\n\n".encode())
                return
            self.send_response(404)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        port = srv.server_address[1]
        scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
        _, claim_oa = _claim_new(dbs, scope, "test openai stream", allowed_models=["gpt-4o"])
        _, claim_gm = _claim_new(dbs, scope, "test gemini stream", allowed_models=["gemini-2.5-flash"])

        oa = OpenAIProvider(Secret("sk-oa-test-123"), None, url=f"http://127.0.0.1:{port}/v1/chat/completions")
        gm = GeminiProvider(
            Secret("gem-key-secret-456"),
            None,
            url_template=f"http://127.0.0.1:{port}/gemini/{{model}}:streamGenerateContent?alt=sse",
        )
        oa_fail = OpenAIProvider(Secret("sk-oa-fail"), None, url=f"http://127.0.0.1:{port}/v1/fail500")
        rates = {
            "gpt-4o": RouteRates(input_micro_usd_per_1m=2_500_000, output_micro_usd_per_1m=10_000_000),
            "gemini-2.5-flash": RouteRates(input_micro_usd_per_1m=1_000_000, output_micro_usd_per_1m=4_000_000),
        }
        gw = ModelGateway(
            dbs["model_gateway"],
            {"openai": oa, "gemini": gm, "oa_fail": oa_fail},
            {
                "gpt-4o": [Route("openai", "gpt-4o")],
                "gemini-2.5-flash": [Route("gemini", "gemini-2.5-flash")],
                "unknown-guard": [Route("oa_fail", "gpt-4o"), Route("openai", "gpt-4o")],
            },
            consent={(scope.tenant_id, "openai"), (scope.tenant_id, "gemini")},
            route_rates=rates,
        )

        out_oa = _run(
            gw.complete(
                SealedModelRequest.from_wire(
                    {
                        "claim": claim_oa.to_wire(),
                        "stream": "main",
                        "system": "sys",
                        "model": "gpt-4o",
                        "messages": [{"kind": "user", "content": "check laptop"}],
                        "tools": ["search"],
                    }
                )
            )
        )
        assert out_oa["message"]["content"] == "Price check "
        assert "INTERNAL_SECRET_THOUGHT" not in str(out_oa)
        assert out_oa["message"]["tool_calls"][0] == {
            "id": "call_oa_1",
            "name": "search",
            "args": {"query": "laptop"},
        }
        openai_req = next(r for r in seen_requests if r["path"] == "/v1/chat/completions")
        assert openai_req["body"]["max_tokens"] == MAX_OUTPUT_TOKENS  # output is capped to the reserved bound

        out_gm = _run(
            gw.complete(
                SealedModelRequest.from_wire(
                    {
                        "claim": claim_gm.to_wire(),
                        "stream": "main",
                        "system": "sys",
                        "model": "gemini-2.5-flash",
                        "messages": [{"kind": "user", "content": "check hotel"}],
                        "tools": ["search"],
                    }
                )
            )
        )
        assert out_gm["message"]["content"] == "Verified: "
        assert "hidden gemini chain" not in str(out_gm)
        assert out_gm["message"]["tool_calls"][0]["name"] == "search"
        gemini_req = next(r for r in seen_requests if r["path"].startswith("/gemini/"))
        assert "gem-key-secret-456" not in gemini_req["path"]
        assert gemini_req["headers"].get("x-goog-api-key") == "gem-key-secret-456"
        assert gemini_req["body"]["generationConfig"]["maxOutputTokens"] == MAX_OUTPUT_TOKENS

        with dbs["owner"].conn() as c:
            rec_oa = c.execute(
                "SELECT usage_kind, provider_key, input_units, output_units, estimated_cost_micro_usd"
                " FROM agent.cost_usage_records WHERE run_id=%s",
                (claim_oa.run_id,),
            ).fetchone()
            assert rec_oa is not None
            assert rec_oa["usage_kind"] == "MEASURED"
            assert rec_oa["provider_key"] == "openai"
            assert (rec_oa["input_units"], rec_oa["output_units"]) == (120, 30)
            assert rec_oa["estimated_cost_micro_usd"] == 600  # 120*2.5 + 30*10 = 600 micro-USD

        # 500 OUTCOME_UNKNOWN must fail immediately without failing over to 2nd route
        _, claim_unk = _claim_new(dbs, scope, "test unknown no failover", allowed_models=["unknown-guard"])
        before_count = len(seen_requests)
        from aiagent_domain.rpcwire import RpcError

        with pytest.raises(RpcError, match="PROVIDER_OUTCOME_UNKNOWN"):
            _run(
                gw.complete(
                    SealedModelRequest.from_wire(
                        {
                            "claim": claim_unk.to_wire(),
                            "stream": "main",
                            "system": "sys",
                            "model": "unknown-guard",
                            "messages": [{"kind": "user", "content": "fail once"}],
                            "tools": [],
                        }
                    )
                )
            )
        assert len(seen_requests) == before_count + 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_router_fallback_circuit_breaker_and_consent_enforcement(
    dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    class FlakyProvider:
        def __init__(self, name: str, *, fail: bool, external: bool = False) -> None:
            self.name = name
            self.fail = fail
            self.external = external
            self.calls = 0

        def capability(self, model: str) -> Any:
            del model
            from aiagent_gateways.providers import Capability

            return Capability(True, True, 100_000, self.name, self.external)

        async def stream(
            self,
            model: str,
            system: str,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: str = "main",
        ) -> Any:
            del model, system, messages, tools, stream
            self.calls += 1
            if self.fail:
                raise ProviderError("RATE_LIMITED", True)
            yield ProviderEvent("text", text=f"ok-from-{self.name}")
            yield ProviderEvent("usage", usage={"input_tokens": 10, "output_tokens": 5})
            yield ProviderEvent("stop")

    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    primary = FlakyProvider("primary_local", fail=True, external=False)
    backup = FlakyProvider("backup_local", fail=False, external=False)
    unconsented = FlakyProvider("unconsented_ext", fail=False, external=True)

    breaker = RouteBreaker(threshold=1, cooldown_s=60.0)
    gw = ModelGateway(
        dbs["model_gateway"],
        {"primary_local": primary, "backup_local": backup, "unconsented_ext": unconsented},
        {
            "routed-model": [Route("primary_local", "m1"), Route("backup_local", "m2")],
            "consent-guarded": [Route("primary_local", "m1"), Route("unconsented_ext", "m3")],
        },
        max_retries=0,
        breaker=breaker,
    )

    _, c1 = _claim_new(dbs, scope, "fallback test 1", allowed_models=["routed-model"])
    out1 = _run(
        gw.complete(
            SealedModelRequest.from_wire(
                {
                    "claim": c1.to_wire(),
                    "stream": "main",
                    "system": "s",
                    "model": "routed-model",
                    "messages": [{"kind": "user", "content": "hi"}],
                    "tools": [],
                }
            )
        )
    )
    assert out1["message"]["content"] == "ok-from-backup_local"
    assert primary.calls == 1 and backup.calls == 1

    # Verify recorded provider_key is the *actual* completed backup route, not routes[0]!
    with dbs["owner"].conn() as c:
        att = c.execute("SELECT provider_key FROM agent.model_attempts WHERE run_id=%s", (c1.run_id,)).fetchone()
        cost = c.execute("SELECT provider_key FROM agent.cost_usage_records WHERE run_id=%s", (c1.run_id,)).fetchone()
        assert att is not None and att["provider_key"] == "backup_local"
        assert cost is not None and cost["provider_key"] == "backup_local"

    # Circuit breaker is now open for primary_local -> second run goes straight to backup_local without hitting primary!
    _, c2 = _claim_new(dbs, scope, "fallback test 2", allowed_models=["routed-model"])
    out2 = _run(
        gw.complete(
            SealedModelRequest.from_wire(
                {
                    "claim": c2.to_wire(),
                    "stream": "main",
                    "system": "s",
                    "model": "routed-model",
                    "messages": [{"kind": "user", "content": "hi 2"}],
                    "tools": [],
                }
            )
        )
    )
    assert out2["message"]["content"] == "ok-from-backup_local"
    assert primary.calls == 1 and backup.calls == 2

    # Unconsented external provider cannot be reached as fallback either!
    _, c3 = _claim_new(dbs, scope, "fallback consent denied", allowed_models=["consent-guarded"])
    from aiagent_domain.rpcwire import RpcError

    with pytest.raises(RpcError, match="PROVIDER_RATE_LIMITED"):
        _run(
            gw.complete(
                SealedModelRequest.from_wire(
                    {
                        "claim": c3.to_wire(),
                        "stream": "main",
                        "system": "s",
                        "model": "consent-guarded",
                        "messages": [{"kind": "user", "content": "hi 3"}],
                        "tools": [],
                    }
                )
            )
        )
    assert unconsented.calls == 0


# ---------------------------------------------------------------- Phase 5B: SearchRouter + HttpFetch + Sentinel SSRF
def test_search_router_and_http_fetch_ssrf_and_sentinel_egress(
    dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    class WebHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_GET(self) -> None:
            if self.path.startswith("/searx"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "results": [
                                {
                                    "title": "Public Result",
                                    "url": "http://web.example.test/article",
                                    "content": "Safe snippet token sk-ant-secret9999999999999999999999 must redact",
                                }
                            ]
                        }
                    ).encode()
                )
                return
            if self.path == "/article":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><script>alert('xss')</script><h1>Report</h1>"
                    b"<p>Bearer sk-proj-topsecretvalue1234567890 ok</p></body></html>"
                )
                return
            if self.path == "/large":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"A" * 50000)
                return
            if self.path == "/redir-ssrf":
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest/meta-data")
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), WebHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    proxy_srv: EgressProxy | None = None
    try:
        port = srv.server_address[1]
        ecfg = EgressConfig.from_json(
            json.dumps(
                {
                    "destinations": {
                        "search-api": {
                            "scheme": "http",
                            "host": "127.0.0.1",
                            "port": port,
                            "methods": ["GET"],
                            "allow_private": ["127.0.0.1"],
                        },
                        "web-fetch": {
                            "scheme": "http*",
                            "host": "*",
                            "port": 0,
                            "methods": ["GET"],
                            "allow_private": ["127.0.0.1"],
                        },
                    }
                }
            ),
            production=False,
        )
        sentinel = Sentinel(dbs["sentinel"], ecfg, secrets.token_bytes(32))

        async def local_resolver(host: str, rport: int) -> list[str]:
            del rport
            if host == "web.example.test":
                return ["127.0.0.1"]
            try:
                return [str(ipaddress.ip_address(host))]
            except ValueError:
                return ["127.0.0.1"]

        # Start real Trusted Egress proxy in a background thread loop for HTTP proxy routing verification
        proxy_port_holder: list[int] = []
        proxy_ready = threading.Event()
        stop_ev_holder: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []

        def run_proxy() -> None:
            async def main() -> None:
                nonlocal proxy_srv
                proxy_srv = EgressProxy(sentinel, ecfg, resolver=local_resolver)
                p = await proxy_srv.start("127.0.0.1", 0)
                proxy_port_holder.append(p)
                stop_ev = asyncio.Event()
                stop_ev_holder.append((asyncio.get_running_loop(), stop_ev))
                proxy_ready.set()
                await stop_ev.wait()
                await proxy_srv.close()

            asyncio.run(main())

        pt = threading.Thread(target=run_proxy, daemon=True)
        pt.start()
        assert proxy_ready.wait(timeout=10.0)
        proxy_url = f"http://127.0.0.1:{proxy_port_holder[0]}"

        search_exec = SearchRouterExecutor(
            f"http://127.0.0.1:{port}/searx",
            backend_kind="searxng",
            proxy=proxy_url,
            resolver=local_resolver,
            allow_private_addrs=frozenset({"127.0.0.1"}),
        )
        fetch_exec = HttpFetchExecutor(
            proxy=proxy_url,
            resolver=local_resolver,
            allow_private_addrs=frozenset({"127.0.0.1"}),
        )
        gw = ToolGateway(
            dbs["tool_gateway"],
            {"mock_search": search_exec, "http_fetch": fetch_exec},
            sentinel=LocalSentinel(sentinel),
        )

        scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
        _, claim = _claim_new(dbs, scope, "research web article", profile="research")

        # 1. Real SearchRouter through Sentinel + EgressProxy
        _propose(dbs, claim, "c_srch", "search", {"query": "ai agent security"})
        srch_res = _run(gw.submit(claim, "c_srch", "search", {"query": "ai agent security"}))
        assert srch_res["status"] == "DONE", srch_res
        assert srch_res["result"]["trustBoundary"] == "UNTRUSTED_SEARCH_RESULTS"
        assert "sk-ant-secret" not in str(srch_res)
        assert srch_res["result"]["results"][0]["title"] == "Public Result"

        # 2. Safe HttpFetch through Sentinel + EgressProxy with HTML script stripping & secret redaction
        _propose(dbs, claim, "c_fetch", "http_fetch", {"url": f"http://web.example.test:{port}/article"})
        fetch_res = _run(gw.submit(claim, "c_fetch", "http_fetch", {"url": f"http://web.example.test:{port}/article"}))
        assert fetch_res["status"] == "DONE", fetch_res
        assert fetch_res["result"]["trustBoundary"] == "UNTRUSTED_WEB_CONTENT"
        assert "alert('xss')" not in fetch_res["result"]["text"]
        assert "sk-proj-" not in fetch_res["result"]["text"]
        assert "Report" in fetch_res["result"]["text"]
        assert len(fetch_res["result"]["contentSha256"]) == 64

        # 2b. Streaming byte cap truncation works safely
        _propose(
            dbs, claim, "c_large", "http_fetch", {"url": f"http://web.example.test:{port}/large", "maxBytes": 2048}
        )
        large_res = _run(
            gw.submit(
                claim, "c_large", "http_fetch", {"url": f"http://web.example.test:{port}/large", "maxBytes": 2048}
            )
        )
        assert large_res["status"] == "DONE", large_res
        assert large_res["result"]["truncated"] is True
        assert len(large_res["result"]["text"]) <= 2048

        # 3. Strict SSRF defenses: loopback without allow_private, metadata IP, numeric IP, userinfo, file://, 302->169.254
        for bad_url in (
            "file:///etc/passwd",
            "http://user:pass@web.example.test/article",
            "http://2130706433/article",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/secret",
        ):
            call_id = f"c_bad_{abs(hash(bad_url)) % 100000}"
            _propose(dbs, claim, call_id, "http_fetch", {"url": bad_url})
            bad_out = _run(gw.submit(claim, call_id, "http_fetch", {"url": bad_url}))
            assert bad_out["status"] == "FAILED", (bad_url, bad_out)

        # Redirect to 169.254.169.254 blocked at hop validation (no allow_private for 169.254.169.254!)
        with pytest.raises(ExecutorFailed, match="egress_denied"):
            _run(
                safe_http_get(
                    f"http://web.example.test:{port}/redir-ssrf",
                    resolver=local_resolver,
                    allow_private_addrs=frozenset({"127.0.0.1"}),
                )
            )
    finally:
        if "stop_ev_holder" in locals() and stop_ev_holder:
            lp, ev = stop_ev_holder[0]
            lp.call_soon_threadsafe(ev.set)
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------- Phase 5C: LLM Gated NLI Compaction
def test_llm_nli_gated_compaction_preserves_head_and_rejects_refuted(
    memory_dbs: dict[str, Database], memory_tenant: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dbs, tenant = memory_dbs, memory_tenant
    tlog_path = tmp_path / "tomb.jsonl"
    tlog = memory_tombstones.TombstoneLog(tlog_path, integrity_key=b"k" * 32)
    tlog.initialize()

    app = create_app(dbs["api"])
    monkeypatch.setenv("AIAGENT_MEMORY_TOMBSTONE_LOG", str(tlog_path))
    monkeypatch.setenv("AIAGENT_MEMORY_TOMBSTONE_HMAC_KEY", (b"k" * 32).hex())
    headers = {"Authorization": f"Bearer {tenant['token']}"}

    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    task_id, claim = _claim_new(dbs, scope, "summarize logs safely")
    with dbs["model_gateway"].tx() as c:
        control.append_head(
            c,
            claim,
            "user",
            {"kind": "user", "content": "Order 42 total is $100 and refunds are NOT permitted after dispatch."},
            writer="model_gateway",
            request_key=secrets.token_hex(32),
        )

    with TestClient(app) as client:
        r_con = client.put(
            f"/v1/tasks/{task_id}/context-policy",
            headers=headers,
            json={
                "schemaVersion": "1.0",
                "recipient": "fake",
                "model": "fake-gateway",
                "enabled": True,
                "semanticVerifierMode": "LLM_GATED",
            },
        )
        assert r_con.status_code == 200, r_con.text
        got = client.get(f"/v1/tasks/{task_id}/context-policy", headers=headers)
        assert got.status_code == 200
        assert got.json()["policy"]["semanticVerifierMode"] == "LLM_GATED"

    memory_writer.grant_memory_consent(
        dbs["api"],
        scope,
        recipient="fake",
        consent_basis="user_explicit",
    )

    from aiagent_gateways.providers import FakeModelProvider

    gw = ModelGateway(
        dbs["model_gateway"],
        {"fake": FakeModelProvider()},
        {"fake-gateway": [Route("fake", "fake-1")]},
        context_key=KeyRing.single(b"a" * 32),
        tombstone_log=tlog,
    )

    # 1. LLM_GATED compaction succeeds without colliding with HEAD CAS
    res = gw.compact_context(claim, auto_if_needed=False)
    assert res["compacted"] is True
    assert res["verifierMode"] == "LLM_NLI_GATED"
    assert res["head"] == "3"

    with dbs["model_gateway"].conn() as c:
        verify_attempts = c.execute(
            "SELECT stream, status_code FROM agent.model_attempts WHERE run_id=%s AND stream='verify'",
            (claim.run_id,),
        ).fetchall()
        assert len(verify_attempts) >= 1 and all(r["status_code"] == "COMPLETED" for r in verify_attempts)

    # 2. Refuted verdict fails closed (FACT_REFUTED) on a fresh run
    task_id2, claim2 = _claim_new(dbs, scope, "refuted logs")
    with dbs["model_gateway"].tx() as c:
        control.append_head(
            c,
            claim2,
            "user",
            {"kind": "user", "content": "Valid record text."},
            writer="model_gateway",
            request_key=secrets.token_hex(32),
        )
    with TestClient(app) as client:
        assert (
            client.put(
                f"/v1/tasks/{task_id2}/context-policy",
                headers=headers,
                json={
                    "schemaVersion": "1.0",
                    "recipient": "fake",
                    "model": "fake-gateway",
                    "enabled": True,
                    "semanticVerifierMode": "LLM_GATED",
                },
            ).status_code
            == 200
        )
    monkeypatch.setenv("AIAGENT_FAKE_NLI_VERDICT", "REFUTED")
    from aiagent_domain.rpcwire import RpcError

    with pytest.raises(RpcError, match="FACT_REFUTED"):
        gw.compact_context(claim2, auto_if_needed=False)
    monkeypatch.delenv("AIAGENT_FAKE_NLI_VERDICT", raising=False)

    # 3. Missing NLI model route fails closed (SEMANTIC_VERIFIER_UNAVAILABLE)
    gw_no_nli = ModelGateway(
        dbs["model_gateway"],
        {"fake": FakeModelProvider()},
        {"fake-gateway": [Route("fake", "fake-1")]},
        context_key=KeyRing.single(b"a" * 32),
        tombstone_log=tlog,
        nli_model="missing-nli-route",
    )
    with pytest.raises(RpcError, match="SEMANTIC_VERIFIER_UNAVAILABLE"):
        gw_no_nli.compact_context(claim2, auto_if_needed=False)


# ---------------------------------------------------------------- Phase 5D: Browser HTTPS + Encrypted Storage + Takeover
def test_browser_https_tls_challenge_encrypted_storage_and_human_takeover_e2e(
    pg: PgEnv, dbs: dict[str, Database], tenant: dict[str, str], tmp_path: Path
) -> None:
    _, _, srv_ctx, cli_ctx = _make_self_signed_tls(tmp_path, "alpha.test")

    class HttpsPortalHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if self.path == "/login":
                self.wfile.write(
                    b"<html><body><h1>Sign In</h1><input type='password' name='pw' value='TOP_SECRET_PW'>"
                    b"<button data-testid='go'>Enter</button></body></html>"
                )
            elif self.path == "/captcha":
                self.wfile.write(
                    b"<html><body><h1>Human Check</h1><div class='g-recaptcha'>Solve CAPTCHA</div></body></html>"
                )
            else:
                self.wfile.write(
                    b"<html><body><h1>Dashboard</h1><p>Welcome authenticated owner session-token-sk-secret9999999999</p>"
                    b"<button data-testid='refresh'>Refresh</button></body></html>"
                )

    https_srv = ThreadingHTTPServer(("127.0.0.1", 0), HttpsPortalHandler)
    https_srv.socket = srv_ctx.wrap_socket(https_srv.socket, server_side=True)
    t = threading.Thread(target=https_srv.serve_forever, daemon=True)
    t.start()

    async def local_https_resolver(host: str, rport: int) -> list[str]:
        del rport
        return ["127.0.0.1"] if host == "alpha.test" else []

    try:
        port = https_srv.server_address[1]
        ecfg = EgressConfig.from_json(
            json.dumps(
                {
                    "destinations": {
                        "web-fetch": {
                            "scheme": "https",
                            "host": "alpha.test",
                            "port": port,
                            "methods": ["GET"],
                            "allow_private": ["127.0.0.1"],
                        }
                    }
                }
            ),
            production=False,
        )
        sentinel = Sentinel(dbs["sentinel"], ecfg, secrets.token_bytes(32))
        ring = KeyRing.single(b"b" * 32, key_id="br-k1")
        worker = PlaywrightBrowserWorker()
        executor = BrowserSessionExecutor(
            dbs["tool_gateway"],
            allowed_web_hosts=frozenset({"alpha.test"}),
            allow_https=True,
            allow_private_addrs=frozenset({"127.0.0.1"}),
            resolver=local_https_resolver,
            verify_ssl=cli_ctx,
            storage_key_ring=ring,
            worker=worker,
        )
        gw = ToolGateway(
            dbs["tool_gateway"],
            {"browser_fixture": executor},
            sentinel=LocalSentinel(sentinel),
        )

        scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
        _, claim = _claim_new(dbs, scope, "navigate real https portal", profile="browser")

        try:
            # 1. Navigate HTTPS login wall -> halts safely in NEEDS_LOGIN, saves encrypted storageState & redacted observation
            login_url = f"https://alpha.test:{port}/login"
            _propose(dbs, claim, "b_login", "browser_navigate", {"url": login_url})
            out1 = _run(gw.submit(claim, "b_login", "browser_navigate", {"url": login_url}))
            assert out1["status"] == "DONE", out1
            assert out1["result"]["state"] == "NEEDS_LOGIN"
            assert out1["result"]["takeoverReason"] == "NEEDS_LOGIN"
            sid = out1["result"]["sessionId"]

            # Observation API is owner-scoped, redacted, and observeOnly=True
            app = create_app(dbs["api"])
            headers = {"Authorization": f"Bearer {tenant['token']}"}
            other_tenant = admin.provision_tenant(pg.owner)
            other_headers = {"Authorization": f"Bearer {other_tenant['token']}"}

            with TestClient(app) as client:
                r_obs = client.get(f"/v1/browser/sessions/{sid}/observation", headers=headers)
                assert r_obs.status_code == 200, r_obs.text
                obs = r_obs.json()
                assert obs["observeOnly"] is True
                assert obs["state"] == "NEEDS_LOGIN"
                assert obs["takeoverReason"] == "NEEDS_LOGIN"
                assert "TOP_SECRET_PW" not in obs["observation"]
                assert "Sign In" in obs["observation"]

                # Other tenant cannot read observation or hijack takeover
                assert client.get(f"/v1/browser/sessions/{sid}/observation", headers=other_headers).status_code == 404
                assert (
                    client.post(
                        f"/v1/browser/sessions/{sid}/takeover/start",
                        headers=other_headers,
                        json={"reason": "hijack"},
                    ).status_code
                    == 404
                )

                # Verify AES-GCM encrypted storage_state persisted in DB (ciphertext bytes + sha256, plaintext not stored)
                with dbs["api"].tx(tenant=tenant["tenantId"]) as c:
                    row = c.execute(
                        "SELECT takeover_reason, storage_state_ciphertext, storage_state_sha256"
                        " FROM agent.browser_sessions WHERE id=%s",
                        (sid,),
                    ).fetchone()
                    assert row is not None
                    assert row["takeover_reason"] == "NEEDS_LOGIN"
                    assert row["storage_state_ciphertext"] is not None
                    assert len(bytes(row["storage_state_ciphertext"])) > 16
                    assert row["storage_state_sha256"] is not None

                # 2. Owner starts takeover -> PAUSED/HUMAN, then completes -> ACTIVE/AGENT generation=2
                r_start = client.post(
                    f"/v1/browser/sessions/{sid}/takeover/start",
                    headers=headers,
                    json={"reason": "owner_login"},
                )
                assert r_start.status_code == 200, r_start.text
                assert r_start.json()["state"] == "PAUSED"
                assert r_start.json()["mode"] == "HUMAN"

                r_comp = client.post(f"/v1/browser/sessions/{sid}/takeover/complete", headers=headers)
                assert r_comp.status_code == 200, r_comp.text
                assert r_comp.json()["state"] == "ACTIVE"
                assert r_comp.json()["mode"] == "AGENT"
                assert r_comp.json()["generation"] == 2

                # 3. Subsequent navigate detects generation bump -> decrypts/restores storageState, loads /dashboard & redacts secret
                dash_url = f"https://alpha.test:{port}/dashboard"
                _propose(dbs, claim, "b_dash", "browser_navigate", {"url": dash_url})
                out2 = _run(gw.submit(claim, "b_dash", "browser_navigate", {"url": dash_url}))
                assert out2["status"] == "DONE", out2
                assert out2["result"]["state"] == "ACTIVE"
                assert "Dashboard" in out2["result"]["observation"]
                assert "sk-secret9999999999" not in out2["result"]["observation"]

                # 4. CAPTCHA page stops cleanly with CAPTCHA_CHALLENGE (zero bypass attempts)
                cap_url = f"https://alpha.test:{port}/captcha"
                _propose(dbs, claim, "b_cap", "browser_navigate", {"url": cap_url})
                out3 = _run(gw.submit(claim, "b_cap", "browser_navigate", {"url": cap_url}))
                assert out3["status"] == "DONE", out3
                assert out3["result"]["state"] == "NEEDS_LOGIN"
                assert out3["result"]["takeoverReason"] == "CAPTCHA_CHALLENGE"

                # 5. Owner aborts takeover -> CLOSED, encrypted storage wiped, subsequent takeover start returns 409
                r_abort = client.post(f"/v1/browser/sessions/{sid}/takeover/abort", headers=headers)
                assert r_abort.status_code == 200, r_abort.text
                assert r_abort.json()["state"] == "CLOSED"

                with dbs["api"].tx(tenant=tenant["tenantId"]) as c:
                    row2 = c.execute(
                        "SELECT state, storage_state_ciphertext, storage_state_sha256"
                        " FROM agent.browser_sessions WHERE id=%s",
                        (sid,),
                    ).fetchone()
                    assert row2 is not None
                    assert row2["state"] == "CLOSED"
                    assert row2["storage_state_ciphertext"] is None
                    assert row2["storage_state_sha256"] is None

                assert (
                    client.post(
                        f"/v1/browser/sessions/{sid}/takeover/start",
                        headers=headers,
                        json={"reason": "after_close"},
                    ).status_code
                    == 409
                )
        finally:
            executor.close()
    finally:
        https_srv.shutdown()
        https_srv.server_close()


# ------------------------------- Phase 5 Review Regression Suite (P1-1..P1-6, P2-1..P2-10)
def test_https_egress_proxy_redirects_cancel_race_and_production_fail_closed(
    dbs: dict[str, Database], tenant: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers Review Checklist #1 (HTTPS via EgressProxy), #2 (HTTP/HTTPS cancel race), #3 (per-hop redirects)."""
    _, _, alpha_srv_ctx, alpha_cli_ctx = _make_self_signed_tls(tmp_path, "alpha.test")
    hits: list[tuple[str, str]] = []

    class AlphaHttpsHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_GET(self) -> None:
            hits.append(("https_alpha", self.path))
            if self.path == "/secret":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"HTTPS via Trusted Egress Proxy")
                return
            self.send_response(404)
            self.end_headers()

    https_srv = ThreadingHTTPServer(("127.0.0.1", 0), AlphaHttpsHandler)
    https_srv.socket = alpha_srv_ctx.wrap_socket(https_srv.socket, server_side=True)
    threading.Thread(target=https_srv.serve_forever, daemon=True).start()
    https_port = https_srv.server_address[1]

    class HttpBounceHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_GET(self) -> None:
            hits.append(("http_src", self.path))
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/hop1")
                self.end_headers()
                return
            if self.path == "/hop1":
                self.send_response(302)
                self.send_header("Location", f"https://alpha.test:{https_port}/secret")
                self.end_headers()
                return
            if self.path == "/direct":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"plain http ok")
                return
            self.send_response(404)
            self.end_headers()

    http_srv = ThreadingHTTPServer(("127.0.0.1", 0), HttpBounceHandler)
    threading.Thread(target=http_srv.serve_forever, daemon=True).start()
    http_port = http_srv.server_address[1]

    proxy_srv: EgressProxy | None = None
    stop_ev_holder: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
    try:
        ecfg = EgressConfig.from_json(
            json.dumps(
                {
                    "destinations": {
                        "web-fetch": {
                            "scheme": "http*",
                            "host": "*",
                            "port": 0,
                            "methods": ["GET", "HEAD"],
                            "allow_private": ["127.0.0.1"],
                        }
                    }
                }
            ),
            production=False,
        )
        sentinel = Sentinel(dbs["sentinel"], ecfg, secrets.token_bytes(32))

        async def resolver(host: str, rport: int) -> list[str]:
            del rport
            return ["127.0.0.1"] if host in ("alpha.test", "bounce.test") else []

        proxy_port_holder: list[int] = []
        ready = threading.Event()

        def run_proxy() -> None:
            async def main() -> None:
                nonlocal proxy_srv
                proxy_srv = EgressProxy(sentinel, ecfg, resolver=resolver, upstream_ssl=alpha_cli_ctx)
                p = await proxy_srv.start("127.0.0.1", 0)
                proxy_port_holder.append(p)
                ev = asyncio.Event()
                stop_ev_holder.append((asyncio.get_running_loop(), ev))
                ready.set()
                await ev.wait()
                await proxy_srv.close()

            asyncio.run(main())

        threading.Thread(target=run_proxy, daemon=True).start()
        assert ready.wait(timeout=10.0)
        proxy_url = f"http://127.0.0.1:{proxy_port_holder[0]}"

        # Simulate network-isolated ToolGateway (resolver=None, proxy required): all HTTP/HTTPS must pass EgressProxy
        fetch_exec = HttpFetchExecutor(
            proxy=proxy_url,
            resolver=None,
            allow_private_addrs=frozenset({"127.0.0.1"}),
            require_proxy=True,
        )
        gw = ToolGateway(dbs["tool_gateway"], {"http_fetch": fetch_exec}, sentinel=LocalSentinel(sentinel))
        scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
        _, claim = _claim_new(dbs, scope, "https proxy & redirect chain", profile="research")

        # 1. Direct HTTPS through Trusted Egress Proxy (tool_gateway has resolver=None -> cannot touch internet!)
        _propose(dbs, claim, "p_https", "http_fetch", {"url": f"https://alpha.test:{https_port}/secret"})
        res1 = _run(gw.submit(claim, "p_https", "http_fetch", {"url": f"https://alpha.test:{https_port}/secret"}))
        assert res1["status"] == "DONE", res1
        assert "HTTPS via Trusted Egress Proxy" in res1["result"]["text"]

        # 2. Multi-hop HTTP -> same-host 302 -> cross-host HTTPS 302 minting a fresh permit per hop
        hits.clear()
        _propose(dbs, claim, "p_chain", "http_fetch", {"url": f"http://bounce.test:{http_port}/start"})
        res2 = _run(gw.submit(claim, "p_chain", "http_fetch", {"url": f"http://bounce.test:{http_port}/start"}))
        assert res2["status"] == "DONE", res2
        assert "HTTPS via Trusted Egress Proxy" in res2["result"]["text"]
        assert hits == [("http_src", "/start"), ("http_src", "/hop1"), ("https_alpha", "/secret")]
        with dbs["owner"].conn() as c:
            permits = c.execute(
                "SELECT scheme, host, port, uses FROM agent.egress_permits WHERE run_id=%s ORDER BY created_at",
                (claim.run_id,),
            ).fetchall()
            assert len(permits) == 4  # 1 direct HTTPS + 3 hops in chain
            assert all(int(r["uses"]) == 1 for r in permits)

        # 3. Cancel race after permit issuance blocks BOTH HTTP and HTTPS GET sends at EgressProxy!
        for scheme, t_host, t_port, t_path in (
            ("http", "bounce.test", http_port, "/direct"),
            ("https", "alpha.test", https_port, "/secret"),
        ):
            _, cancel_claim = _claim_new(dbs, scope, f"cancel race {scheme}", profile="research")
            it_id = uuid7_str = secrets.token_hex(16)
            del it_id, uuid7_str
            # Issue valid permit via Sentinel while run is RUNNING, then cancel run BEFORE send
            from aiagent_domain import uuid7

            intent_id = uuid7()
            with dbs["owner"].tx() as c:
                c.execute(
                    "INSERT INTO agent.tool_intents(tenant_id, id, task_id, run_id, tool_key, invocation_key,"
                    " request_sha256, safe_arguments_json, risk_class, goal_revision, status_code)"
                    " VALUES (%s,%s,%s,%s,'http_fetch',%s,repeat('b',64),'{}','read',1,'SENDING')",
                    (scope.tenant_id, intent_id, cancel_claim.task_id, cancel_claim.run_id, f"k-{scheme}"),
                )
            permit_tok = sentinel.authorize(cancel_claim, intent_id, scheme, t_host, t_port, "GET")
            with dbs["owner"].tx() as c:
                c.execute("UPDATE agent.runs SET cancel_requested=true WHERE id=%s", (cancel_claim.run_id,))
            before_hits = len(hits)
            with pytest.raises(ExecutorFailed, match="egress_denied"):
                _run(
                    safe_http_get(
                        f"{scheme}://{t_host}:{t_port}{t_path}",
                        proxy=proxy_url,
                        proxy_permit=permit_tok,
                        require_proxy=True,
                    )
                )
            assert len(hits) == before_hits  # Upstream was NEVER contacted

        # 4. Production without proxy fails closed even outside resolver bypass
        monkeypatch.setenv("AIAGENT_PROFILE", "production")
        with pytest.raises(ExecutorFailed, match="egress_proxy_required"):
            _run(safe_http_get(f"https://alpha.test:{https_port}/secret", proxy=None))
    finally:
        if stop_ev_holder:
            lp, ev = stop_ev_holder[0]
            lp.call_soon_threadsafe(ev.set)
        https_srv.shutdown()
        https_srv.server_close()
        http_srv.shutdown()
        http_srv.server_close()


def test_openai_compat_split_tool_chunks_fail_closed_external_weight_and_context_size(
    dbs: dict[str, Database], tenant: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers Review Checklist #10 (split SSE tool chunks) + P1-4, P2-6, P2-9."""
    from aiagent_core.config import ConfigError
    from aiagent_gateways.servers import load_model_config_from_env

    # P1-4: remote OpenAI-compat URL with EXTERNAL=0 must be rejected fail-closed!
    monkeypatch.setenv("AIAGENT_OPENAI_COMPAT_URL", "https://remote-llm.example.com/v1/chat/completions")
    monkeypatch.setenv("AIAGENT_OPENAI_COMPAT_EXTERNAL", "0")
    with pytest.raises(ConfigError, match="loopback"):
        load_model_config_from_env()
    monkeypatch.delenv("AIAGENT_OPENAI_COMPAT_EXTERNAL")

    # P1-4: without EXTERNAL=0, compat provider defaults to external_recipient=True (even for localhost)
    monkeypatch.setenv("AIAGENT_OPENAI_COMPAT_URL", "http://127.0.0.1:9999/v1/chat/completions")
    compat_providers, _, _ = load_model_config_from_env()
    assert compat_providers["openai_compat"].capability("local-llm").external_recipient is True
    monkeypatch.delenv("AIAGENT_OPENAI_COMPAT_URL")

    # #10 Split OpenAI-compatible SSE chunks (chunk 1 has id & partial args, chunk 2 supplies name)
    class SplitToolHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(n)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {
                    "choices": [
                        {"delta": {"tool_calls": [{"index": 0, "id": "call_sp_1", "function": {"arguments": '{"qu'}}]}}
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [{"index": 0, "function": {"name": "search", "arguments": 'ery":"px'}}]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'l"}'}}]},
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 12},
                },
            ]
            for ch in chunks:
                self.wfile.write(f"data: {json.dumps(ch)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), SplitToolHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        low_p = OpenAIProvider(
            None,
            None,
            url=f"http://127.0.0.1:{port}/low",
            provider_name="low_p",
            external_recipient=False,
            max_context_chars=2000,
        )
        high_p = OpenAIProvider(
            None,
            None,
            url=f"http://127.0.0.1:{port}/high",
            provider_name="high_p",
            external_recipient=False,
            max_context_chars=600,
        )
        scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
        _, c_wt = _claim_new(dbs, scope, "weight & split chunks", allowed_models=["weighted"])

        called: list[str] = []

        class SpyHigh(OpenAIProvider):
            async def stream(self, *a: Any, **kw: Any) -> Any:
                called.append("high")
                async for ev in super().stream(*a, **kw):
                    yield ev

        spy_high = SpyHigh(
            None,
            None,
            url=f"http://127.0.0.1:{port}/high",
            provider_name="high_p",
            external_recipient=False,
            max_context_chars=600,
        )
        gw = ModelGateway(
            dbs["model_gateway"],
            {"low_p": low_p, "high_p": spy_high},
            {"weighted": [Route("low_p", "m-low", weight=1), Route("high_p", "m-high", weight=10)]},
        )
        out = _run(
            gw.complete(
                SealedModelRequest.from_wire(
                    {
                        "claim": c_wt.to_wire(),
                        "stream": "main",
                        "system": "s",
                        "model": "weighted",
                        "messages": [{"kind": "user", "content": "hi"}],
                        "tools": ["search"],
                    }
                )
            )
        )
        assert called == ["high"]  # P2-9: higher weight route chosen over lower weight route!
        assert out["message"]["tool_calls"] == [{"id": "call_sp_1", "name": "search", "args": {"query": "pxl"}}]

        # P2-6: long system prompt + tool schema estimate exceeds max_context_chars even when messages are tiny!
        _, c_big = _claim_new(dbs, scope, "context size guard", allowed_models=["weighted"])
        gw_tiny = ModelGateway(dbs["model_gateway"], {"high_p": high_p}, {"weighted": [Route("high_p", "m-high")]})
        from aiagent_domain.rpcwire import RpcError

        with pytest.raises(RpcError, match="CONTEXT_TOO_LARGE"):
            _run(
                gw_tiny.complete(
                    SealedModelRequest.from_wire(
                        {
                            "claim": c_big.to_wire(),
                            "stream": "main",
                            "system": "X" * 500,
                            "model": "weighted",
                            "messages": [{"kind": "user", "content": "ok"}],
                            "tools": ["search"],
                        }
                    )
                )
            )
    finally:
        srv.shutdown()
        srv.server_close()


def test_nli_recipient_consent_isolation_revocation_race_and_tail_evidence_span(
    memory_dbs: dict[str, Database], memory_tenant: dict[str, str], tmp_path: Path
) -> None:
    """Covers Review Checklist #6 (NLI recipient isolation), #7 (revocation race), and P2-8 (>8k tail span)."""
    dbs, tenant = memory_dbs, memory_tenant
    tlog = memory_tombstones.TombstoneLog(tmp_path / "nli-tomb.jsonl", integrity_key=b"t" * 32)
    tlog.initialize()
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])

    from aiagent_domain.rpcwire import RpcError
    from aiagent_gateways.providers import Capability, FakeModelProvider

    class RecordingExternalProvider:
        def __init__(self, name: str, *, verdict: str = "SUPPORTED") -> None:
            self.name = name
            self.verdict = verdict
            self.seen_payloads: list[str] = []

        def capability(self, model: str) -> Capability:
            del model
            return Capability(False, True, 100_000, self.name, True)

        async def stream(
            self, model: str, system: str, messages: list[dict[str, Any]], tools: list[Any], *, stream: str = "main"
        ) -> Any:
            del model, system, tools, stream
            self.seen_payloads.append(messages[0]["content"])
            yield ProviderEvent("text", text=json.dumps({"verdict": self.verdict}))
            yield ProviderEvent("usage", usage={"input_tokens": 20, "output_tokens": 5})
            yield ProviderEvent("stop")

    provider_a = RecordingExternalProvider("prov_a")
    provider_b = RecordingExternalProvider("prov_b")

    app = create_app(dbs["api"])
    headers = {"Authorization": f"Bearer {tenant['token']}"}

    # Only consent to prov_a in DB! Never consent to prov_b.
    memory_writer.grant_memory_consent(dbs["api"], scope, recipient="prov_a", consent_basis="user_explicit")

    # 1. User consents only to prov_a, NLI route points to prov_b -> ZERO bytes may ever reach prov_b!
    task_id, claim = _claim_new(dbs, scope, "nli cross recipient guard", allowed_models=["fake-gateway", "nli-b"])
    with dbs["model_gateway"].tx() as c:
        control.append_head(
            c,
            claim,
            "user",
            {"kind": "user", "content": "Sensitive contract #99total is $500 and confidential."},
            writer="model_gateway",
            request_key=secrets.token_hex(32),
        )
    with TestClient(app) as client:
        assert (
            client.put(
                f"/v1/tasks/{task_id}/context-policy",
                headers=headers,
                json={
                    "schemaVersion": "1.0",
                    "recipient": "prov_a",
                    "model": "fake-gateway",
                    "enabled": True,
                    "semanticVerifierMode": "LLM_GATED",
                },
            ).status_code
            == 200
        )

    gw_nli_b = ModelGateway(
        dbs["model_gateway"],
        {"prov_a": provider_a, "prov_b": provider_b, "fake": FakeModelProvider()},
        {"fake-gateway": [Route("fake", "fake-1")], "nli-b": [Route("prov_b", "m-b")]},
        consent={
            (scope.tenant_id, "prov_a"),
            (scope.tenant_id, "prov_b"),
        },  # Even with deployment consent, user DB consent forbids B!
        context_key=KeyRing.single(b"n" * 32),
        tombstone_log=tlog,
        nli_model="nli-b",
    )
    with pytest.raises(RpcError, match="RECIPIENT_CONSENT|SEMANTIC_VERIFIER_UNAVAILABLE"):
        gw_nli_b.compact_context(claim, auto_if_needed=False)
    assert provider_b.seen_payloads == []
    with dbs["owner"].conn() as c:
        row_b = c.execute(
            "SELECT reason_code FROM agent.model_attempts WHERE run_id=%s AND stream='verify'",
            (claim.run_id,),
        ).fetchone()
        assert row_b is not None and row_b["reason_code"] == "RECIPIENT_CONSENT"

    # 2. P2-8: >12,000 char document with evidence at tail -> _select_evidence_span preserves tail fact for NLI!
    from aiagent_gateways.model_gateway import _select_evidence_span

    padding = "noise_entry " * 1000  # ~12,000 chars
    tail_fact = "ERROR: Invoice INV-8842 refunds are NOT permitted MUST_BE_PRESERVED_AT_TAIL."
    selected_span = _select_evidence_span(
        padding + tail_fact, "refunds are NOT permitted MUST_BE_PRESERVED_AT_TAIL", 8000
    )
    assert len(selected_span) <= 8000 and "MUST_BE_PRESERVED_AT_TAIL" in selected_span

    task_tail, claim_tail = _claim_new(dbs, scope, "nli tail span", allowed_models=["fake-gateway", "nli-a"])
    with dbs["model_gateway"].tx() as c:
        control.append_head(
            c,
            claim_tail,
            "user",
            {"kind": "user", "content": padding + tail_fact},
            writer="model_gateway",
            request_key=secrets.token_hex(32),
        )
    with TestClient(app) as client:
        assert (
            client.put(
                f"/v1/tasks/{task_tail}/context-policy",
                headers=headers,
                json={
                    "schemaVersion": "1.0",
                    "recipient": "prov_a",
                    "model": "fake-gateway",
                    "enabled": True,
                    "semanticVerifierMode": "LLM_GATED",
                },
            ).status_code
            == 200
        )

    gw_nli_a = ModelGateway(
        dbs["model_gateway"],
        {"prov_a": provider_a, "fake": FakeModelProvider()},
        {"fake-gateway": [Route("fake", "fake-1")], "nli-a": [Route("prov_a", "m-a")]},
        consent={(scope.tenant_id, "prov_a")},
        context_key=KeyRing.single(b"n" * 32),
        tombstone_log=tlog,
        nli_model="nli-a",
    )
    comp = gw_nli_a.compact_context(claim_tail, auto_if_needed=False)
    assert comp["compacted"] is True
    assert len(provider_a.seen_payloads) >= 1 and any(
        "MUST_BE_PRESERVED_AT_TAIL" in p or "INV-8842" in p for p in provider_a.seen_payloads
    )

    # 3. Revoking prov_a consent blocks BOTH compact_context preflight AND mid-flight NLI send race before hitting provider_a!
    from aiagent_context.models import SafeStop
    from aiagent_gateways.context_dispatch import ContextAdmission
    from aiagent_gateways.model_gateway import NliAdmission

    before_calls = len(provider_a.seen_payloads)
    # 3a. Direct mid-flight race: even after preflight succeeded on claim_tail, revoking prov_a stops verify_claim_with_llm!
    memory_writer.revoke_memory_consent(dbs["api"], scope, recipient="prov_a", tombstone_log=tlog)
    ctx_tail = ContextAdmission(dbs["model_gateway"], claim_tail, KeyRing.single(b"n" * 32), 1)
    with pytest.raises(SafeStop, match="RECIPIENT_CONSENT|SEMANTIC_VERIFIER_UNAVAILABLE"):
        _run(
            gw_nli_a.verify_claim_with_llm(
                claim_tail,
                "fake-gateway",
                "refunds are NOT permitted",
                tail_fact,
                nli=NliAdmission(
                    memory=ctx_tail.memory,
                    scope=ctx_tail.scope,
                    policy_recipient="prov_a",
                    expected_head=3,
                    expected_source_epoch=1,
                ),
            )
        )
    assert len(provider_a.seen_payloads) == before_calls

    # 3b. Fresh compact_context after revocation rejects cleanly at policy preflight with zero sends
    task_rev, claim_rev = _claim_new(dbs, scope, "nli revoked consent", allowed_models=["fake-gateway", "nli-a"])
    with dbs["model_gateway"].tx() as c:
        control.append_head(
            c,
            claim_rev,
            "user",
            {"kind": "user", "content": "Revoked check record."},
            writer="model_gateway",
            request_key=secrets.token_hex(32),
        )
    with TestClient(app) as client:
        assert (
            client.put(
                f"/v1/tasks/{task_rev}/context-policy",
                headers=headers,
                json={
                    "schemaVersion": "1.0",
                    "recipient": "prov_a",
                    "model": "fake-gateway",
                    "enabled": True,
                    "semanticVerifierMode": "LLM_GATED",
                },
            ).status_code
            == 200
        )
    rev_res = gw_nli_a.compact_context(claim_rev, auto_if_needed=False)
    assert rev_res["compacted"] is False and rev_res["reason"] == "MEMORY_CONSENT_REQUIRED"
    assert len(provider_a.seen_payloads) == before_calls


def test_browser_egress_cookie_401_takeover_auth_submit_outcome_captcha_and_concurrency(
    pg: PgEnv, dbs: dict[str, Database], tenant: dict[str, str], tmp_path: Path
) -> None:
    """Covers Review Checklist #4 (Cookie 401->200 across EgressProxy + /takeover/authenticate), #5 (POST submit & UNKNOWN), #8 (concurrency), #9 (401/CAPTCHA/login)."""
    _, _, srv_ctx, cli_ctx = _make_self_signed_tls(tmp_path, "portal.test")
    post_events: list[str] = []

    class CookiePortalHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_GET(self) -> None:
            cookie_hdr = self.headers.get("Cookie", "")
            if self.path == "/redir-to-dash":
                self.send_response(302)
                self.send_header("Set-Cookie", "sid=valid-cookie-123; Path=/; HttpOnly; Secure")
                self.send_header("Location", "/dashboard")
                self.end_headers()
                return
            if self.path == "/dashboard":
                if "sid=valid-cookie-123" not in cookie_hdr:
                    self.send_response(401)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"<html><body><h1>401 Auth Required</h1></body></html>")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h1>Owner Dashboard</h1>"
                    b"<form action='/submit-order' method='post'>"
                    b"<input id='note' name='note' type='text' value=''>"
                    b"<button data-testid='send-order' type='submit'>Place</button>"
                    b"</form></body></html>"
                )
                return
            if self.path == "/captcha-click":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h1>Challenge</h1><div class='g-recaptcha'>Verify</div>"
                    b"<button data-testid='go'>Next</button></body></html>"
                )
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Public Home</h1></body></html>")

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(n).decode("utf-8", "replace")
            if self.path == "/takeover-login":
                if "pw=OWNER_PASS_99" in raw_body:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Set-Cookie", "sid=valid-cookie-123; Path=/; HttpOnly; Secure")
                    self.end_headers()
                    self.wfile.write(b"<html><body><h1>Signed In Ok token sk-priv9999999999999</h1></body></html>")
                    return
                self.send_response(401)
                self.end_headers()
                return
            if self.path == "/submit-order":
                cookie_hdr = self.headers.get("Cookie", "")
                if "sid=valid-cookie-123" not in cookie_hdr:
                    self.send_response(401)
                    self.end_headers()
                    return
                post_events.append(raw_body)
                if "trigger_500" in raw_body:
                    self.send_response(500)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<html><body><h1>Order Submitted!</h1></body></html>")
                return
            self.send_response(404)
            self.end_headers()

    https_srv = ThreadingHTTPServer(("127.0.0.1", 0), CookiePortalHandler)
    https_srv.socket = srv_ctx.wrap_socket(https_srv.socket, server_side=True)
    threading.Thread(target=https_srv.serve_forever, daemon=True).start()
    port = https_srv.server_address[1]

    proxy_srv: EgressProxy | None = None
    stop_ev_holder: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
    try:
        ecfg = EgressConfig.from_json(
            json.dumps(
                {
                    "destinations": {
                        "web-fetch": {
                            "scheme": "https",
                            "host": "portal.test",
                            "port": port,
                            "methods": ["GET", "POST"],
                            "allow_private": ["127.0.0.1"],
                        }
                    }
                }
            ),
            production=False,
        )
        sentinel = Sentinel(dbs["sentinel"], ecfg, secrets.token_bytes(32))

        async def resolver(host: str, rport: int) -> list[str]:
            del rport
            return ["127.0.0.1"] if host == "portal.test" else []

        proxy_port_holder: list[int] = []
        ready = threading.Event()

        def run_proxy() -> None:
            async def main() -> None:
                nonlocal proxy_srv
                proxy_srv = EgressProxy(sentinel, ecfg, resolver=resolver, upstream_ssl=cli_ctx)
                p = await proxy_srv.start("127.0.0.1", 0)
                proxy_port_holder.append(p)
                ev = asyncio.Event()
                stop_ev_holder.append((asyncio.get_running_loop(), ev))
                ready.set()
                await ev.wait()
                await proxy_srv.close()

            asyncio.run(main())

        threading.Thread(target=run_proxy, daemon=True).start()
        assert ready.wait(timeout=10.0)
        proxy_url = f"http://127.0.0.1:{proxy_port_holder[0]}"

        ring = KeyRing.single(b"c" * 32, key_id="br-c1")
        worker = PlaywrightBrowserWorker()
        executor = BrowserSessionExecutor(
            dbs["tool_gateway"],
            allowed_web_hosts=frozenset({"portal.test"}),
            allow_https=True,
            allow_private_addrs=frozenset({"127.0.0.1"}),
            resolver=None,
            proxy=proxy_url,
            storage_key_ring=ring,
            worker=worker,
            require_proxy=True,
        )
        gw = ToolGateway(dbs["tool_gateway"], {"browser_fixture": executor}, sentinel=LocalSentinel(sentinel))

        async def takeover_http(
            url: str, method: str, hdrs: dict[str, str], body: bytes | None
        ) -> tuple[int, list[str], str]:
            import httpx

            async with httpx.AsyncClient(verify=cli_ctx, trust_env=False, follow_redirects=False) as cli:
                up = urlsplit(url)
                pinned = f"https://127.0.0.1:{port}{up.path or '/'}" + (f"?{up.query}" if up.query else "")
                h = dict(hdrs)
                h["Host"] = f"portal.test:{port}"
                r = await cli.request(
                    method, pinned, headers=h, content=body, extensions={"sni_hostname": "portal.test"}
                )
                return r.status_code, list(r.headers.get_list("set-cookie")), r.text

        app = create_app(dbs["api"], storage_key_ring=ring, takeover_http_fn=takeover_http)

        scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
        _, claim = _claim_new(dbs, scope, "cookie gated portal", profile="browser")
        headers = {"Authorization": f"Bearer {tenant['token']}"}

        try:
            # #9: Without cookie, /dashboard returns 401 -> NEEDS_LOGIN!
            dash_url = f"https://portal.test:{port}/dashboard"
            _propose(dbs, claim, "b_401", "browser_navigate", {"url": dash_url})
            out_401 = _run(gw.submit(claim, "b_401", "browser_navigate", {"url": dash_url}))
            assert out_401["status"] == "DONE"
            assert out_401["result"]["state"] == "NEEDS_LOGIN"
            assert out_401["result"]["takeoverReason"] == "NEEDS_LOGIN"
            sid = out_401["result"]["sessionId"]

            # #4: Owner starts takeover, authenticates interactively via /takeover/authenticate, completes takeover!
            with TestClient(app) as client:
                assert (
                    client.post(
                        f"/v1/browser/sessions/{sid}/takeover/start", headers=headers, json={"reason": "login"}
                    ).status_code
                    == 200
                )
                auth_r = client.post(
                    f"/v1/browser/sessions/{sid}/takeover/authenticate",
                    headers=headers,
                    json={
                        "url": f"https://portal.test:{port}/takeover-login",
                        "method": "POST",
                        "formData": {"pw": "OWNER_PASS_99"},
                    },
                )
                assert auth_r.status_code == 200, auth_r.text
                assert auth_r.json()["authenticated"] is True
                assert auth_r.json()["cookieCount"] == 1
                assert "sk-priv9999999999999" not in auth_r.json()["observation"]
                assert client.post(f"/v1/browser/sessions/{sid}/takeover/complete", headers=headers).status_code == 200

            # Now agent navigates /dashboard -> restored AES-GCM cookie sent through EgressProxy -> 200 OK Dashboard!
            _propose(dbs, claim, "b_200", "browser_navigate", {"url": dash_url})
            out_200 = _run(gw.submit(claim, "b_200", "browser_navigate", {"url": dash_url}))
            assert out_200["status"] == "DONE", out_200
            assert out_200["result"]["state"] == "ACTIVE"
            assert "Owner Dashboard" in out_200["result"]["observation"]

            # #5: Real form submit over HTTPS POST through Sentinel + EgressProxy (requires APPROVED record)
            sub_args = {
                "sessionId": sid,
                "kind": "test_id",
                "selector": "send-order",
                "inputs": [{"kind": "css", "selector": "#note", "value": "hello_world"}],
            }
            _propose(dbs, claim, "b_post", "browser_submit", sub_args)
            need_apr = _run(gw.submit(claim, "b_post", "browser_submit", sub_args))
            assert need_apr["status"] == "PENDING_APPROVAL", need_apr
            apr1 = dbs["owner"].one(
                "SELECT request_sha256, record_version FROM agent.approval_records WHERE tenant_id=%s AND id=%s",
                (scope.tenant_id, need_apr["approvalId"]),
            )
            assert apr1 is not None
            commands.decide_approval(
                dbs["api"], scope, need_apr["approvalId"], "APPROVED", apr1["request_sha256"], apr1["record_version"]
            )
            confirmed = _run(gw.submit(claim, "b_post", "browser_submit", sub_args))
            assert confirmed["status"] == "CONFIRMED", confirmed
            assert "Order Submitted!" in confirmed["result"]["after"]
            assert any("note=hello_world" in ev for ev in post_events)

            # #5: HTTP 500 during POST becomes UNKNOWN (never auto-retried)
            sub_500 = {
                "sessionId": sid,
                "kind": "test_id",
                "selector": "send-order",
                "inputs": [{"kind": "css", "selector": "#note", "value": "trigger_500"}],
            }
            # Re-navigate to /dashboard first so the form is on page again
            _propose(dbs, claim, "b_dash2", "browser_navigate", {"url": dash_url})
            assert _run(gw.submit(claim, "b_dash2", "browser_navigate", {"url": dash_url}))["status"] == "DONE"
            _propose(dbs, claim, "b_unk", "browser_submit", sub_500)
            need_apr2 = _run(gw.submit(claim, "b_unk", "browser_submit", sub_500))
            assert need_apr2["status"] == "PENDING_APPROVAL", need_apr2
            apr2 = dbs["owner"].one(
                "SELECT request_sha256, record_version FROM agent.approval_records WHERE tenant_id=%s AND id=%s",
                (scope.tenant_id, need_apr2["approvalId"]),
            )
            assert apr2 is not None
            commands.decide_approval(
                dbs["api"], scope, need_apr2["approvalId"], "APPROVED", apr2["request_sha256"], apr2["record_version"]
            )
            unk_out = _run(gw.submit(claim, "b_unk", "browser_submit", sub_500))
            assert unk_out["status"] == "UNKNOWN", unk_out
            # Retrying the same call_id after UNKNOWN must stay UNKNOWN (no duplicate POST!)
            hits_before = len(post_events)
            assert _run(gw.submit(claim, "b_unk", "browser_submit", sub_500))["status"] == "UNKNOWN"
            assert len(post_events) == hits_before

            # #9: Distinguish CAPTCHA_CHALLENGE on a fresh active session from NEEDS_LOGIN!
            _, claim_cap = _claim_new(dbs, scope, "captcha check portal", profile="browser")
            cap_url = f"https://portal.test:{port}/captcha-click"
            _propose(dbs, claim_cap, "b_cap_nav", "browser_navigate", {"url": cap_url})
            cap_nav = _run(gw.submit(claim_cap, "b_cap_nav", "browser_navigate", {"url": cap_url}))
            assert cap_nav["status"] == "DONE", cap_nav
            assert cap_nav["result"]["takeoverReason"] == "CAPTCHA_CHALLENGE"
            # Clicking on another session hitting captcha page preserves CAPTCHA_CHALLENGE too
            # P2-7: abort wipes observation & closes observation endpoint to 404
            with TestClient(app) as client:
                assert client.post(f"/v1/browser/sessions/{sid}/takeover/abort", headers=headers).status_code == 200
                assert client.get(f"/v1/browser/sessions/{sid}/observation", headers=headers).status_code == 404

            # #8: Concurrent two-tenant isolated sessions mix neither cookies nor observations!
            t2 = admin.provision_tenant(pg.owner)
            scope2 = Scope(t2["tenantId"], t2["userId"], t2["workspaceId"])
            _, c_t1 = _claim_new(dbs, scope, "concurrent t1", profile="browser")
            _, c_t2 = _claim_new(dbs, scope2, "concurrent t2", profile="browser")

            redir_url = f"https://portal.test:{port}/redir-to-dash"
            public_url = f"https://portal.test:{port}/"
            _propose(dbs, c_t1, "conc1", "browser_navigate", {"url": redir_url})
            _propose(dbs, c_t2, "conc2", "browser_navigate", {"url": public_url})

            async def run_both() -> tuple[dict[str, Any], dict[str, Any]]:
                return await asyncio.gather(
                    gw.submit(c_t1, "conc1", "browser_navigate", {"url": redir_url}),
                    gw.submit(c_t2, "conc2", "browser_navigate", {"url": public_url}),
                )

            o1, o2 = _run(run_both())
            assert o1["status"] == "DONE" and "Owner Dashboard" in o1["result"]["observation"]
            assert o2["status"] == "DONE" and "Public Home" in o2["result"]["observation"]
            # Tenant 2 session still does NOT have Tenant 1's cookie -> gets 401 on /dashboard!
            _propose(
                dbs, c_t2, "conc2_401", "browser_navigate", {"url": dash_url, "sessionId": o2["result"]["sessionId"]}
            )
            o2_dash = _run(
                gw.submit(
                    c_t2, "conc2_401", "browser_navigate", {"url": dash_url, "sessionId": o2["result"]["sessionId"]}
                )
            )
            assert o2_dash["result"]["state"] == "NEEDS_LOGIN"
        finally:
            executor.close()
    finally:
        if stop_ev_holder:
            lp, ev = stop_ev_holder[0]
            lp.call_soon_threadsafe(ev.set)
        https_srv.shutdown()
        https_srv.server_close()


def _make_takeover_tls_and_proxy(
    tmp_path: Path, handler_cls: type[BaseHTTPRequestHandler], pg_dbs: dict[str, Database]
) -> tuple[
    ThreadingHTTPServer,
    int,
    ssl.SSLContext,
    Sentinel,
    EgressProxy,
    str,
    list[tuple[asyncio.AbstractEventLoop, asyncio.Event]],
]:
    _, _, srv_ctx, cli_ctx = _make_self_signed_tls(tmp_path, "portal.test")
    https_srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    https_srv.socket = srv_ctx.wrap_socket(https_srv.socket, server_side=True)
    threading.Thread(target=https_srv.serve_forever, daemon=True).start()
    port = https_srv.server_address[1]

    ecfg = EgressConfig.from_json(
        json.dumps(
            {
                "destinations": {
                    "web-fetch": {
                        "scheme": "https",
                        "host": "portal.test",
                        "port": port,
                        "methods": ["GET", "POST"],
                        "allow_private": ["127.0.0.1"],
                    }
                }
            }
        ),
        production=False,
    )
    sentinel = Sentinel(pg_dbs["sentinel"], ecfg, secrets.token_bytes(32))

    async def resolver(host: str, rport: int) -> list[str]:
        del rport
        return ["127.0.0.1"] if host == "portal.test" else []

    proxy_srv_holder: list[EgressProxy] = []
    proxy_port_holder: list[int] = []
    stop_ev_holder: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
    ready = threading.Event()

    def run_proxy() -> None:
        async def main() -> None:
            ps = EgressProxy(sentinel, ecfg, resolver=resolver, upstream_ssl=cli_ctx)
            proxy_srv_holder.append(ps)
            p = await ps.start("127.0.0.1", 0)
            proxy_port_holder.append(p)
            ev = asyncio.Event()
            stop_ev_holder.append((asyncio.get_running_loop(), ev))
            ready.set()
            await ev.wait()
            await ps.close()

        asyncio.run(main())

    threading.Thread(target=run_proxy, daemon=True).start()
    assert ready.wait(timeout=10.0)
    return (
        https_srv,
        port,
        cli_ctx,
        sentinel,
        proxy_srv_holder[0],
        f"http://127.0.0.1:{proxy_port_holder[0]}",
        stop_ev_holder,
    )


def test_takeover_authenticate_real_egress_proxy_and_observation_scope(
    pg: PgEnv, dbs: dict[str, Database], tenant: dict[str, str], tmp_path: Path
) -> None:
    seen_proxy_auths: list[str] = []

    class AuthHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_GET(self) -> None:
            if self.path == "/dashboard":
                if "sid=proxy-cookie-7" not in self.headers.get("Cookie", ""):
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b"<html><body>Login needed</body></html>")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<html><body><h1>Authed Proxy Dashboard</h1></body></html>")
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n).decode("utf-8", "replace")
            if self.path == "/takeover-login" and "pw=PASS42" in raw:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Set-Cookie", "sid=proxy-cookie-7; Path=/; HttpOnly; Secure")
                self.end_headers()
                self.wfile.write(b"<html><body><h1>Logged in sk-secret99999999</h1></body></html>")
                return
            self.send_response(401)
            self.end_headers()

    https_srv, port, cli_ctx, sentinel, _, proxy_url, stop_ev_holder = _make_takeover_tls_and_proxy(
        tmp_path, AuthHandler, dbs
    )
    ring = KeyRing.single(b"k" * 32, key_id="p1a")
    worker = PlaywrightBrowserWorker()
    executor = BrowserSessionExecutor(
        dbs["tool_gateway"],
        allowed_web_hosts=frozenset({"portal.test"}),
        allow_https=True,
        allow_private_addrs=frozenset({"127.0.0.1"}),
        proxy=proxy_url,
        storage_key_ring=ring,
        worker=worker,
        require_proxy=True,
    )
    gw = ToolGateway(dbs["tool_gateway"], {"browser_fixture": executor}, sentinel=LocalSentinel(sentinel))
    app = create_app(
        dbs["api"],
        storage_key_ring=ring,
        sentinel=LocalSentinel(sentinel),
        egress_proxy=proxy_url,
        allowed_web_hosts=frozenset({"portal.test"}),
        takeover_verify_ssl=cli_ctx,
    )
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    _, claim = _claim_new(dbs, scope, "real proxy takeover", profile="browser")
    headers = {"Authorization": f"Bearer {tenant['token']}"}
    try:
        dash_url = f"https://portal.test:{port}/dashboard"
        _propose(dbs, claim, "p1a_nav", "browser_navigate", {"url": dash_url})
        out = _run(gw.submit(claim, "p1a_nav", "browser_navigate", {"url": dash_url}))
        assert out["result"]["state"] == "NEEDS_LOGIN"
        sid = out["result"]["sessionId"]

        with TestClient(app) as client:
            # Must fail before takeover/start switches mode to HUMAN
            pre_auth = client.post(
                f"/v1/browser/sessions/{sid}/takeover/authenticate",
                headers=headers,
                json={
                    "url": f"https://portal.test:{port}/takeover-login",
                    "method": "POST",
                    "formData": {"pw": "PASS42"},
                },
            )
            assert pre_auth.status_code in (403, 409)

            assert (
                client.post(
                    f"/v1/browser/sessions/{sid}/takeover/start", headers=headers, json={"reason": "login"}
                ).status_code
                == 200
            )
            # Disallowed host rejected by allowed_web_hosts before proxy
            bad_host = client.post(
                f"/v1/browser/sessions/{sid}/takeover/authenticate",
                headers=headers,
                json={"url": f"https://evil.test:{port}/takeover-login", "method": "POST"},
            )
            assert bad_host.status_code == 403

            # P2-2: Observation endpoint works cleanly with t.workspace_id SQL join while ACTIVE/PAUSED
            obs_ok = client.get(f"/v1/browser/sessions/{sid}/observation", headers=headers)
            assert obs_ok.status_code == 200, obs_ok.text

            # Real proxy-forwarded takeover authenticate with takeover_http_fn=None (no CONNECT!)
            auth_r = client.post(
                f"/v1/browser/sessions/{sid}/takeover/authenticate",
                headers=headers,
                json={
                    "url": f"https://portal.test:{port}/takeover-login",
                    "method": "POST",
                    "formData": {"pw": "PASS42"},
                },
            )
            assert auth_r.status_code == 200, auth_r.text
            assert auth_r.json()["authenticated"] is True
            assert auth_r.json()["cookieCount"] == 1
            assert "sk-secret99999999" not in auth_r.json()["observation"]

            # Direct CONNECT over EgressProxy remains blocked; single-use human_takeover permit cannot be minted while aborted
            assert client.post(f"/v1/browser/sessions/{sid}/takeover/complete", headers=headers).status_code == 200

        # Cookie works through EgressProxy afterwards
        _propose(dbs, claim, "p1a_nav2", "browser_navigate", {"url": dash_url, "sessionId": sid})
        out2 = _run(gw.submit(claim, "p1a_nav2", "browser_navigate", {"url": dash_url, "sessionId": sid}))
        assert out2["status"] == "DONE"
        assert "Authed Proxy Dashboard" in out2["result"]["observation"]

        # P1-A replay / tamper on EgressProxy directly + P2-2 workspace disable check
        import httpx

        with TestClient(app) as client:
            assert (
                client.post(
                    f"/v1/browser/sessions/{sid}/takeover/start", headers=headers, json={"reason": "login2"}
                ).status_code
                == 200
            )
            from hashlib import sha256 as _sha256

            from fastapi import HTTPException

            body_pw = b"pw=PASS42"
            perm = sentinel.authorize_human_takeover(
                tenant["tenantId"],
                tenant["userId"],
                tenant["workspaceId"],
                sid,
                "https",
                "portal.test",
                port,
                "POST",
                "/takeover-login",
                _sha256(body_pw).hexdigest(),
            )
            # Tampered path or tampered body with the same permit is denied BEFORE single-use consumption succeeds!
            with pytest.raises(HTTPException) as ei_tamper_path:
                _run(
                    _takeover_via_forward_proxy(
                        proxy_url=proxy_url,
                        permit=perm,
                        target_url=f"https://portal.test:{port}/other-action",
                        method="POST",
                        host_header=f"portal.test:{port}",
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        body=body_pw,
                    )
                )
            assert ei_tamper_path.value.status_code == 403
            with pytest.raises(HTTPException) as ei_tamper_body:
                _run(
                    _takeover_via_forward_proxy(
                        proxy_url=proxy_url,
                        permit=perm,
                        target_url=f"https://portal.test:{port}/takeover-login",
                        method="POST",
                        host_header=f"portal.test:{port}",
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        body=b"pw=TAMPERED",
                    )
                )
            assert ei_tamper_body.value.status_code == 403
            # HTTP CONNECT against EgressProxy is blocked; single-use forward-proxy permit works ONCE and replay is 403!
            with pytest.raises(httpx.ProxyError):
                _run(
                    httpx.AsyncClient(proxy=proxy_url, verify=False, trust_env=False).post(
                        f"https://portal.test:{port}/takeover-login",
                        headers={"Proxy-Authorization": f"Bearer {perm}"},
                        content=body_pw,
                    )
                )
            st1, _, _ = _run(
                _takeover_via_forward_proxy(
                    proxy_url=proxy_url,
                    permit=perm,
                    target_url=f"https://portal.test:{port}/takeover-login",
                    method="POST",
                    host_header=f"portal.test:{port}",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    body=body_pw,
                )
            )
            assert st1 == 200

            from fastapi import HTTPException

            with pytest.raises(HTTPException) as ei_replay:
                _run(
                    _takeover_via_forward_proxy(
                        proxy_url=proxy_url,
                        permit=perm,
                        target_url=f"https://portal.test:{port}/takeover-login",
                        method="POST",
                        host_header=f"portal.test:{port}",
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        body=b"pw=PASS42",
                    )
                )
            assert ei_replay.value.status_code == 403
            assert seen_proxy_auths == []

            with dbs["owner"].tx() as c:
                c.execute("UPDATE agent.workspaces SET disabled_at=now() WHERE id=%s", (tenant["workspaceId"],))
            obs_disabled = client.get(f"/v1/browser/sessions/{sid}/observation", headers=headers)
            assert obs_disabled.status_code in (401, 403)
    finally:
        executor.close()
        lp, ev = stop_ev_holder[0]
        lp.call_soon_threadsafe(ev.set)
        https_srv.shutdown()
        https_srv.server_close()


def test_browser_submit_post_dispatch_302_denied_render_crash_and_db_persist_unknown(
    dbs: dict[str, Database], tenant: dict[str, str], tmp_path: Path
) -> None:
    post_hits: list[str] = []

    class PostEdgeHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_GET(self) -> None:
            if self.path == "/form-302":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><form action='/post-302-evil' method='POST'>"
                    b"<input id='note' name='note' value=''/>"
                    b"<button data-testid='go' type='submit'>Go</button></form></body></html>"
                )
                return
            if self.path == "/form-big":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><form action='/post-big' method='POST'>"
                    b"<input id='note' name='note' value=''/>"
                    b"<button data-testid='go' type='submit'>Go</button></form></body></html>"
                )
                return
            if self.path == "/form-db":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><form action='/post-ok' method='POST'>"
                    b"<input id='note' name='note' value=''/>"
                    b"<button data-testid='go' type='submit'>Go</button></form></body></html>"
                )
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n).decode("utf-8", "replace")
            post_hits.append(f"{self.path}:{body}")
            if self.path == "/post-302-evil":
                self.send_response(302)
                self.send_header("Location", "https://evil.test/steal")
                self.end_headers()
                return
            if self.path == "/post-big":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<html><body>" + b"X" * 600_000 + b"</body></html>")
                return
            if self.path == "/post-ok":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<html><body><h1>Committed!</h1></body></html>")
                return
            self.send_response(404)
            self.end_headers()

    https_srv, port, cli_ctx, sentinel, _, proxy_url, stop_ev_holder = _make_takeover_tls_and_proxy(
        tmp_path, PostEdgeHandler, dbs
    )
    ring = KeyRing.single(b"p" * 32, key_id="p1b")
    worker = PlaywrightBrowserWorker()
    executor = BrowserSessionExecutor(
        dbs["tool_gateway"],
        allowed_web_hosts=frozenset({"portal.test"}),
        allow_https=True,
        allow_private_addrs=frozenset({"127.0.0.1"}),
        proxy=proxy_url,
        storage_key_ring=ring,
        worker=worker,
        require_proxy=True,
    )
    gw = ToolGateway(dbs["tool_gateway"], {"browser_fixture": executor}, sentinel=LocalSentinel(sentinel))
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])

    def _run_approved_submit(call_id: str, page_path: str) -> dict[str, Any]:
        _, cl = _claim_new(dbs, scope, f"post unknown {call_id}", profile="browser")
        url = f"https://portal.test:{port}{page_path}"
        _propose(dbs, cl, f"{call_id}_nav", "browser_navigate", {"url": url})
        nav = _run(gw.submit(cl, f"{call_id}_nav", "browser_navigate", {"url": url}))
        sid = nav["result"]["sessionId"]
        args = {
            "sessionId": sid,
            "kind": "test_id",
            "selector": "go",
            "inputs": [{"kind": "css", "selector": "#note", "value": "pay100"}],
        }
        _propose(dbs, cl, call_id, "browser_submit", args)
        pending = _run(gw.submit(cl, call_id, "browser_submit", args))
        assert pending["status"] == "PENDING_APPROVAL", pending
        apr = dbs["owner"].one(
            "SELECT request_sha256, record_version FROM agent.approval_records WHERE tenant_id=%s AND id=%s",
            (scope.tenant_id, pending["approvalId"]),
        )
        assert apr is not None
        commands.decide_approval(
            dbs["api"], scope, pending["approvalId"], "APPROVED", apr["request_sha256"], apr["record_version"]
        )
        res = _run(gw.submit(cl, call_id, "browser_submit", args))
        # Retrying same tool_call_id must NEVER re-send
        before_retry = len(post_hits)
        res_retry = _run(gw.submit(cl, call_id, "browser_submit", args))
        assert len(post_hits) == before_retry
        assert res_retry["status"] == "UNKNOWN"
        eff = dbs["owner"].one(
            "SELECT outcome_code FROM agent.side_effect_records s JOIN agent.tool_intents t ON t.id=s.tool_intent_id"
            " WHERE t.tenant_id=%s AND t.invocation_key=%s",
            (scope.tenant_id, call_id),
        )
        assert eff is not None and eff["outcome_code"] == "UNKNOWN"
        return res

    try:
        # 1) POST succeeded -> 302 to disallowed host -> UNKNOWN (never FAILED!)
        out_302 = _run_approved_submit("p1b_302", "/form-302")
        assert out_302["status"] == "UNKNOWN"
        assert sum(1 for h in post_hits if h.startswith("/post-302-evil:")) == 1

        # 2) POST succeeded -> oversized response rejected by render guard -> UNKNOWN
        out_big = _run_approved_submit("p1b_big", "/form-big")
        assert out_big["status"] == "UNKNOWN"
        assert sum(1 for h in post_hits if h.startswith("/post-big:")) == 1

        # 3) P2-4: POST succeeded -> DB _persist_session_state fails -> UNKNOWN (never FAILED!)
        orig_persist = executor._persist_session_state

        async def broken_persist(*a: Any, **kw: Any) -> None:
            del a, kw
            raise RuntimeError("db_persist_lost")

        _, cl_db = _claim_new(dbs, scope, "post db persist fail", profile="browser")
        _propose(dbs, cl_db, "p24_nav", "browser_navigate", {"url": f"https://portal.test:{port}/form-db"})
        nav_db = _run(gw.submit(cl_db, "p24_nav", "browser_navigate", {"url": f"https://portal.test:{port}/form-db"}))
        sid_db = nav_db["result"]["sessionId"]
        args_db = {
            "sessionId": sid_db,
            "kind": "test_id",
            "selector": "go",
            "inputs": [{"kind": "css", "selector": "#note", "value": "commit1"}],
        }
        _propose(dbs, cl_db, "p24_sub", "browser_submit", args_db)
        pend_db = _run(gw.submit(cl_db, "p24_sub", "browser_submit", args_db))
        apr_db = dbs["owner"].one(
            "SELECT request_sha256, record_version FROM agent.approval_records WHERE tenant_id=%s AND id=%s",
            (scope.tenant_id, pend_db["approvalId"]),
        )
        assert apr_db is not None
        commands.decide_approval(
            dbs["api"], scope, pend_db["approvalId"], "APPROVED", apr_db["request_sha256"], apr_db["record_version"]
        )
        executor._persist_session_state = broken_persist  # type: ignore[method-assign]
        try:
            out_db = _run(gw.submit(cl_db, "p24_sub", "browser_submit", args_db))
        finally:
            executor._persist_session_state = orig_persist  # type: ignore[method-assign]
        assert out_db["status"] == "UNKNOWN"
        eff_db = dbs["owner"].one(
            "SELECT outcome_code FROM agent.side_effect_records s JOIN agent.tool_intents t ON t.id=s.tool_intent_id"
            " WHERE t.tenant_id=%s AND t.invocation_key=%s",
            (scope.tenant_id, "p24_sub"),
        )
        assert eff_db is not None and eff_db["outcome_code"] == "UNKNOWN"
        assert sum(1 for h in post_hits if h.startswith("/post-ok:")) == 1
    finally:
        executor.close()
        lp, ev = stop_ev_holder[0]
        lp.call_soon_threadsafe(ev.set)
        https_srv.shutdown()
        https_srv.server_close()


def test_nli_verify_pinned_to_admitted_route_no_unconsented_fallback(
    pg: PgEnv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del pg
    from conftest import drop_database, make_database

    iso_pg = make_database("aiagent_nli_pin")
    dbs: dict[str, Database] = {k: iso_pg.db(k, max_size=8) for k in iso_pg.dsn}
    dbs["owner"] = Database(iso_pg.owner, max_size=4)
    tenant = admin.provision_tenant(iso_pg.owner)
    seen_a: list[str] = []
    seen_b: list[str] = []
    from aiagent_gateways.providers import Capability, FakeModelProvider

    tlog_path = tmp_path / "nli-pin-tomb.jsonl"
    tlog = memory_tombstones.TombstoneLog(tlog_path, integrity_key=b"p" * 32)
    tlog.initialize()
    monkeypatch.setenv("AIAGENT_MEMORY_TOMBSTONE_LOG", str(tlog_path))
    monkeypatch.setenv("AIAGENT_MEMORY_TOMBSTONE_HMAC_KEY", (b"p" * 32).hex())

    class StubProvider:
        def __init__(self, name: str, sink: list[str]) -> None:
            self.name = name
            self.sink = sink

        def capability(self, model: str) -> Capability:
            del model
            return Capability(False, True, 32_000, self.name, True)

        async def stream(
            self, model: str, system: str, messages: list[dict[str, Any]], tools: list[Any], *, stream: str = "main"
        ) -> Any:
            del model, system, tools, stream
            self.sink.append(messages[0]["content"])
            yield ProviderEvent("text", text='{"verdict":"SUPPORTED"}')
            yield ProviderEvent("usage", usage={"input_tokens": 10, "output_tokens": 4})
            yield ProviderEvent("stop")

    try:
        scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
        memory_writer.grant_memory_consent(dbs["api"], scope, recipient="prov_a", consent_basis="user_explicit")
        task_id, claim = _claim_new(
            dbs, scope, "nli route pin", profile="default", allowed_models=["fake-gateway", "nli-multi"]
        )
        with dbs["model_gateway"].tx() as c:
            control.append_head(
                c,
                claim,
                "user",
                {"kind": "user", "content": "Confidential order #441 total is $250."},
                writer="model_gateway",
                request_key=secrets.token_hex(32),
            )
        app = create_app(dbs["api"])
        with TestClient(app) as client:
            assert (
                client.put(
                    f"/v1/tasks/{task_id}/context-policy",
                    headers={"Authorization": f"Bearer {tenant['token']}"},
                    json={
                        "schemaVersion": "1.0",
                        "recipient": "prov_a",
                        "model": "fake-gateway",
                        "enabled": True,
                        "semanticVerifierMode": "LLM_GATED",
                    },
                ).status_code
                == 200
            )

        r_a = Route("prov_a", "m-a")
        r_b = Route("prov_b", "m-b")
        breaker = RouteBreaker(threshold=1, cooldown_s=60.0)
        mg = ModelGateway(
            dbs["model_gateway"],
            {
                "prov_a": StubProvider("prov_a", seen_a),
                "prov_b": StubProvider("prov_b", seen_b),
                "fake": FakeModelProvider(),
            },  # type: ignore[dict-item]
            {"fake-gateway": [Route("fake", "fake-1")], "nli-multi": [r_a, r_b]},
            consent={(scope.tenant_id, "prov_a"), (scope.tenant_id, "prov_b")},
            breaker=breaker,
            context_key=KeyRing.single(b"p" * 32),
            tombstone_log=tlog,
            nli_model="nli-multi",
        )
        orig_admit = mg._admit

        def admit_then_trip(req: Any, *args: Any, **kwargs: Any) -> Any:
            out = orig_admit(req, *args, **kwargs)
            if getattr(req, "stream", None) == "verify":
                # Breaker flips for r_a AFTER admission succeeds; verify stream must NOT fall back to r_b!
                breaker.record_failure(r_a)
            return out

        mg._admit = admit_then_trip  # type: ignore[method-assign]
        with pytest.raises(RpcError, match="SEMANTIC_VERIFIER_UNAVAILABLE"):
            mg.compact_context(claim, auto_if_needed=False)
        assert seen_b == []
        assert seen_a == []
    finally:
        for db in dbs.values():
            db.close()
        drop_database(iso_pg.name)


def test_browser_submit_dom_tamper_after_approval_rejected(
    dbs: dict[str, Database], tenant: dict[str, str], tmp_path: Path
) -> None:
    post_hits: list[str] = []

    class FormTamperHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_GET(self) -> None:
            if self.path == "/checkout":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><form id='pay' action='/submit-pay' method='POST'>"
                    b"<input type='hidden' id='amt' name='amount' value='10'/>"
                    b"<input id='note' name='note' value=''/>"
                    b"<button data-testid='submit-pay' type='submit'>Pay</button>"
                    b"</form></body></html>"
                )
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            post_hits.append(f"{self.path}:{self.rfile.read(n).decode('utf-8', 'replace')}")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body>Done</body></html>")

    https_srv, port, _, sentinel, _, proxy_url, stop_ev_holder = _make_takeover_tls_and_proxy(
        tmp_path, FormTamperHandler, dbs
    )
    ring = KeyRing.single(b"t" * 32, key_id="p1d")
    worker = PlaywrightBrowserWorker()
    executor = BrowserSessionExecutor(
        dbs["tool_gateway"],
        allowed_web_hosts=frozenset({"portal.test"}),
        allow_https=True,
        allow_private_addrs=frozenset({"127.0.0.1"}),
        proxy=proxy_url,
        storage_key_ring=ring,
        worker=worker,
        require_proxy=True,
    )
    gw = ToolGateway(dbs["tool_gateway"], {"browser_fixture": executor}, sentinel=LocalSentinel(sentinel))
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    try:
        _, cl = _claim_new(dbs, scope, "dom tamper test", profile="browser")
        url = f"https://portal.test:{port}/checkout"
        _propose(dbs, cl, "t_nav", "browser_navigate", {"url": url})
        nav = _run(gw.submit(cl, "t_nav", "browser_navigate", {"url": url}))
        sid = nav["result"]["sessionId"]

        # Case 1: Action URL tampered after approval
        args1 = {
            "sessionId": sid,
            "kind": "test_id",
            "selector": "submit-pay",
            "inputs": [{"kind": "css", "selector": "#note", "value": "coffee"}],
        }
        _propose(dbs, cl, "t_sub1", "browser_submit", args1)
        pend1 = _run(gw.submit(cl, "t_sub1", "browser_submit", args1))
        apr1 = dbs["owner"].one(
            "SELECT request_sha256, record_version, safe_summary FROM agent.approval_records WHERE tenant_id=%s AND id=%s",
            (scope.tenant_id, pend1["approvalId"]),
        )
        assert apr1 is not None and "/submit-pay" in apr1["safe_summary"]
        commands.decide_approval(
            dbs["api"], scope, pend1["approvalId"], "APPROVED", apr1["request_sha256"], apr1["record_version"]
        )

        # Mutate form action on live page after user approved!
        _run(
            executor._on_persistent_loop(
                worker._page(sid).evaluate("document.getElementById('pay').setAttribute('action', '/steal-pay')")
            )
        )
        with pytest.raises(RpcError) as ei1:
            _run(gw.submit(cl, "t_sub1", "browser_submit", args1))
        assert ei1.value.code == "INTENT_CONFLICT"
        assert post_hits == []

        # Case 2: Hidden input mutated on live page after approval (even with same action & method!)
        _propose(dbs, cl, "t_nav2", "browser_navigate", {"url": url, "sessionId": sid})
        _run(gw.submit(cl, "t_nav2", "browser_navigate", {"url": url, "sessionId": sid}))
        _propose(dbs, cl, "t_sub2", "browser_submit", args1)
        pend2 = _run(gw.submit(cl, "t_sub2", "browser_submit", args1))
        apr2 = dbs["owner"].one(
            "SELECT request_sha256, record_version FROM agent.approval_records WHERE tenant_id=%s AND id=%s",
            (scope.tenant_id, pend2["approvalId"]),
        )
        assert apr2 is not None
        commands.decide_approval(
            dbs["api"], scope, pend2["approvalId"], "APPROVED", apr2["request_sha256"], apr2["record_version"]
        )
        _run(executor._on_persistent_loop(worker._page(sid).evaluate("document.getElementById('amt').value = '9999'")))
        with pytest.raises(RpcError) as ei2:
            _run(gw.submit(cl, "t_sub2", "browser_submit", args1))
        assert ei2.value.code == "INTENT_CONFLICT"
        assert post_hits == []

        # Case 3: Direct dispatcher mismatch (bind_request passed stale bound_form -> BrowserError APPROVAL_BINDING_MISMATCH)
        orig_bind = executor.bind_request

        def stale_bind(tool: str, args: dict[str, Any], *, claim: Any = None) -> dict[str, Any] | None:
            del tool, args, claim
            return {
                "action": f"https://portal.test:{port}/submit-pay",
                "method": "POST",
                "origin": f"https://portal.test:{port}",
                "payloadDigest": "0" * 64,
            }

        executor.bind_request = stale_bind  # type: ignore[method-assign]
        try:
            _propose(dbs, cl, "t_sub3", "browser_submit", args1)
            pend3 = _run(gw.submit(cl, "t_sub3", "browser_submit", args1))
            apr3 = dbs["owner"].one(
                "SELECT request_sha256, record_version FROM agent.approval_records WHERE tenant_id=%s AND id=%s",
                (scope.tenant_id, pend3["approvalId"]),
            )
            assert apr3 is not None
            commands.decide_approval(
                dbs["api"], scope, pend3["approvalId"], "APPROVED", apr3["request_sha256"], apr3["record_version"]
            )
            out3 = _run(gw.submit(cl, "t_sub3", "browser_submit", args1))
        finally:
            executor.bind_request = orig_bind  # type: ignore[method-assign]
        assert out3["status"] == "FAILED"
        assert "APPROVAL_BINDING_MISMATCH" in str(out3["result"])
        assert post_hits == []
    finally:
        executor.close()
        lp, ev = stop_ev_holder[0]
        lp.call_soon_threadsafe(ev.set)
        https_srv.shutdown()
        https_srv.server_close()


def test_redirect_307_308_preserves_method_and_browser_click_anchor_navigation(
    dbs: dict[str, Database], tenant: dict[str, str], tmp_path: Path
) -> None:
    seen_requests: list[tuple[str, str, str]] = []

    class RedirectAndLinkHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_GET(self) -> None:
            seen_requests.append(("GET", self.path, ""))
            if self.path == "/home":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body>"
                    b"<a href='/next-step' data-testid='next-link'>Next Step</a>"
                    b"<a href='https://evil.test/out' data-testid='evil-link'>Evil</a>"
                    b"<form action='/post-307' method='POST'>"
                    b"<input id='note' name='note' value=''/>"
                    b"<button data-testid='go307' type='submit'>Go307</button></form>"
                    b"</body></html>"
                )
                return
            if self.path == "/next-step":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<html><body><h1>Arrived Next Step</h1></body></html>")
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n).decode("utf-8", "replace")
            seen_requests.append(("POST", self.path, body))
            if self.path == "/step1":
                self.send_response(307)
                self.send_header("Location", "/step2")
                self.end_headers()
                return
            if self.path == "/step2":
                self.send_response(308)
                self.send_header("Location", "/final-post")
                self.end_headers()
                return
            if self.path == "/final-post":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(f"POST_OK:{body}".encode())
                return
            if self.path == "/post-307":
                self.send_response(307)
                self.send_header("Location", "/next-step")
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

    https_srv, port, _, sentinel, _, proxy_url, stop_ev_holder = _make_takeover_tls_and_proxy(
        tmp_path, RedirectAndLinkHandler, dbs
    )
    ring = KeyRing.single(b"r" * 32, key_id="p213")
    worker = PlaywrightBrowserWorker()
    executor = BrowserSessionExecutor(
        dbs["tool_gateway"],
        allowed_web_hosts=frozenset({"portal.test"}),
        allow_https=True,
        allow_private_addrs=frozenset({"127.0.0.1"}),
        proxy=proxy_url,
        storage_key_ring=ring,
        worker=worker,
        require_proxy=True,
    )
    gw = ToolGateway(dbs["tool_gateway"], {"browser_fixture": executor}, sentinel=LocalSentinel(sentinel))
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    _, cl = _claim_new(dbs, scope, "307 308 and click nav", profile="browser")

    from aiagent_domain import sha256_hex, uuid7
    from psycopg.types.json import Jsonb

    hop_intent_id = uuid7()
    with dbs["owner"].tx() as c:
        c.execute(
            "INSERT INTO agent.tool_intents(tenant_id, id, task_id, run_id, tool_key, invocation_key,"
            " request_sha256, safe_arguments_json, risk_class, goal_revision, status_code, stream)"
            " VALUES (%s,%s,%s,%s,'http_fetch',%s,%s,%s,'read',%s,'SENDING','main')",
            (
                cl.tenant_id,
                hop_intent_id,
                cl.task_id,
                cl.run_id,
                f"hop-{hop_intent_id}",
                sha256_hex({"url": f"https://portal.test:{port}/step1"}),
                Jsonb({"url": f"https://portal.test:{port}/step1"}),
                cl.goal_revision,
            ),
        )

    async def permit_issuer(dest: Any) -> str:
        return sentinel.authorize(cl, hop_intent_id, dest.scheme, dest.host, dest.port, dest.method)

    try:
        # 1) safe_http_get preserves POST + body across 307 -> 308 -> 200 through EgressProxy
        p0 = sentinel.authorize(cl, hop_intent_id, "https", "portal.test", port, "POST")
        r = _run(
            safe_http_get(
                f"https://portal.test:{port}/step1",
                method="POST",
                request_body=b"payload=keepme",
                proxy=proxy_url,
                proxy_permit=p0,
                permit_issuer=permit_issuer,
                verify=False,
                require_proxy=True,
            )
        )
        assert r.body == b"POST_OK:payload=keepme"
        assert ("POST", "/step1", "payload=keepme") in seen_requests
        assert ("POST", "/step2", "payload=keepme") in seen_requests
        assert ("POST", "/final-post", "payload=keepme") in seen_requests

        # 2) browser_submit POST -> 307 halts safely as UNKNOWN (no GET downgrade, no double-POST)
        home_url = f"https://portal.test:{port}/home"
        _propose(dbs, cl, "p2_nav", "browser_navigate", {"url": home_url})
        nav = _run(gw.submit(cl, "p2_nav", "browser_navigate", {"url": home_url}))
        sid = nav["result"]["sessionId"]
        sub_args = {
            "sessionId": sid,
            "kind": "test_id",
            "selector": "go307",
            "inputs": [{"kind": "css", "selector": "#note", "value": "noredir"}],
        }
        _propose(dbs, cl, "p2_307", "browser_submit", sub_args)
        pend = _run(gw.submit(cl, "p2_307", "browser_submit", sub_args))
        apr = dbs["owner"].one(
            "SELECT request_sha256, record_version FROM agent.approval_records WHERE tenant_id=%s AND id=%s",
            (scope.tenant_id, pend["approvalId"]),
        )
        assert apr is not None
        commands.decide_approval(
            dbs["api"], scope, pend["approvalId"], "APPROVED", apr["request_sha256"], apr["record_version"]
        )
        before_req_count = len(seen_requests)
        res_307 = _run(gw.submit(cl, "p2_307", "browser_submit", sub_args))
        assert res_307["status"] == "UNKNOWN"
        assert [x for x in seen_requests[before_req_count:]] == [("POST", "/post-307", "note=noredir")]

        # 3) browser_click on <a href='/next-step'> navigates through EgressProxy & Sentinel!
        _propose(dbs, cl, "p2_nav2", "browser_navigate", {"url": home_url, "sessionId": sid})
        # Session was paused by UNKNOWN; resume first via takeover/complete or open a clean session on new claim
        _, cl2 = _claim_new(dbs, scope, "click nav clean", profile="browser")
        _propose(dbs, cl2, "p2_cnav", "browser_navigate", {"url": home_url})
        cnav = _run(gw.submit(cl2, "p2_cnav", "browser_navigate", {"url": home_url}))
        sid2 = cnav["result"]["sessionId"]
        _propose(
            dbs, cl2, "p2_click_bad", "browser_click", {"sessionId": sid2, "kind": "test_id", "selector": "evil-link"}
        )
        bad_click = _run(
            gw.submit(
                cl2, "p2_click_bad", "browser_click", {"sessionId": sid2, "kind": "test_id", "selector": "evil-link"}
            )
        )
        assert bad_click["status"] == "FAILED"
        assert "EGRESS_DENIED" in str(bad_click["result"])

        _propose(
            dbs, cl2, "p2_click_ok", "browser_click", {"sessionId": sid2, "kind": "test_id", "selector": "next-link"}
        )
        ok_click = _run(
            gw.submit(
                cl2, "p2_click_ok", "browser_click", {"sessionId": sid2, "kind": "test_id", "selector": "next-link"}
            )
        )
        assert ok_click["status"] == "CONFIRMED", ok_click
        assert "Arrived Next Step" in ok_click["result"]["after"]
    finally:
        executor.close()
        lp, ev = stop_ev_holder[0]
        lp.call_soon_threadsafe(ev.set)
        https_srv.shutdown()
        https_srv.server_close()


def test_takeover_production_sentinel_rpc_no_di_and_restart_replay(
    pg: PgEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-1, P2-1, P2-2, P2-3: Real Sentinel process + create_app() without DI + PG nonce durability + strict auth."""
    import os
    import socket
    import subprocess
    import sys
    import time
    from hashlib import sha256 as _sha256

    from aiagent_core.rpc import RpcClient
    from aiagent_gateways.executors import Destination
    from aiagent_gateways.sentinel_port import RpcSentinel

    _, _, srv_ctx, cli_ctx = _make_self_signed_tls(tmp_path, "portal.test")
    login_mode = {"mode": "ok"}

    class ProdTakeoverHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_GET(self) -> None:
            if self.path == "/dashboard":
                if "sid=prod-ck-1" in self.headers.get("Cookie", ""):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"<html><body><h1>Prod Authed Dashboard</h1></body></html>")
                    return
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"<html><body>Need login</body></html>")
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n).decode("utf-8", "replace")
            if self.path == "/login-302":
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()
                return
            if self.path == "/login-pw":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<html><body><form><input type='password' name='pw'/></form></body></html>")
                return
            if self.path == "/login-captcha":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<html><body><div class='g-recaptcha'>Verify</div></body></html>")
                return
            if self.path == "/login-ok" and "pw=GOOD99" in raw and login_mode["mode"] == "ok":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Set-Cookie", "sid=prod-ck-1; Path=/")
                self.end_headers()
                self.wfile.write(b"<html><body><h1>Signed In Prod</h1></body></html>")
                return
            self.send_response(401)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), ProdTakeoverHandler)
    srv.socket = srv_ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    http_port = srv.server_address[1]

    def _free_port() -> int:
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            return int(sk.getsockname()[1])

    def _wait_listen(p: int) -> None:
        end = time.time() + 20.0
        while time.time() < end:
            with socket.socket() as sk:
                if sk.connect_ex(("127.0.0.1", p)) == 0:
                    return
            time.sleep(0.05)
        raise TimeoutError(f"port {p} not listening")

    sentinel_port = _free_port()
    egress_port = _free_port()
    api_key = secrets.token_hex(32)
    tool_key = secrets.token_hex(32)
    permit_key = secrets.token_hex(32)
    artifact_key = secrets.token_hex(32)

    egress_cfg_file = tmp_path / "egress.json"
    egress_cfg_file.write_text(
        json.dumps(
            {
                "destinations": {
                    "web-fetch": {
                        "scheme": "http",
                        "host": "portal.test",
                        "port": http_port,
                        "methods": ["GET", "POST"],
                        "allow_private": ["127.0.0.1"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    # Wait: Test with plain HTTP upstream inside Sentinel subprocess (no self-signed cert override needed in main.py)
    srv.shutdown()
    srv.server_close()
    plain_srv = ThreadingHTTPServer(("127.0.0.1", http_port), ProdTakeoverHandler)
    threading.Thread(target=plain_srv.serve_forever, daemon=True).start()

    sent_env = {
        **os.environ,
        "AIAGENT_PROFILE": "test",
        "AIAGENT_SENTINEL_DSN": pg.dsn["sentinel"],
        "AIAGENT_SENTINEL_ENDPOINT": f"tcp:127.0.0.1:{sentinel_port}",
        "AIAGENT_SENTINEL_RPC_KEYS": f"tool_gateway={tool_key},api={api_key}",
        "AIAGENT_SENTINEL_PERMIT_KEY": permit_key,
        "AIAGENT_EGRESS_LISTEN": f"127.0.0.1:{egress_port}",
        "AIAGENT_EGRESS_CONFIG_FILE": str(egress_cfg_file),
    }

    def _spawn_sentinel() -> subprocess.Popen[bytes]:
        p = subprocess.Popen([sys.executable, "-m", "aiagent_sentinel.main"], env=sent_env)
        _wait_listen(sentinel_port)
        _wait_listen(egress_port)
        return p

    # Note: main.py's EgressProxy uses socket.getaddrinfo; test using /etc/hosts or portal.test via EgressConfig for 127.0.0.1!
    # Reconfigure target host to 127.0.0.1 with allow_private for the external Sentinel process (real TCP, no monkeypatch)
    egress_cfg_file.write_text(
        json.dumps(
            {
                "destinations": {
                    "web-fetch": {
                        "scheme": "http",
                        "host": "127.0.0.1",
                        "port": http_port,
                        "methods": ["GET", "POST"],
                        "allow_private": ["127.0.0.1"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    dbs = {k: pg.db(k, max_size=8) for k in pg.dsn}
    dbs["owner"] = Database(pg.owner, max_size=4)
    tenant = admin.provision_tenant(pg.owner)
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    _, cl = _claim_new(dbs, scope, "prod rpc takeover", profile="browser")

    # Create an active browser_session directly in DB + advance to PAUSED/HUMAN
    from aiagent_browser.pg_fence import PostgresFencePort
    from aiagent_domain import uuid7

    sid = uuid7()
    from aiagent_browser import Authority

    auth = Authority(
        cl.tenant_id,
        cl.owner_user_id,
        cl.task_id,
        cl.run_id,
        str(cl.goal_revision),
        str(cl.lease_epoch),
        cl.worker_ref,
    )
    _run(PostgresFencePort(dbs["tool_gateway"]).register(sid, auth))

    headers = {"Authorization": f"Bearer {tenant['token']}"}
    # API process environment NEVER receives AIAGENT_SENTINEL_PERMIT_KEY!
    monkeypatch.delenv("AIAGENT_SENTINEL_PERMIT_KEY", raising=False)
    monkeypatch.setenv("AIAGENT_API_DSN", pg.dsn["api"])
    monkeypatch.setenv("AIAGENT_SENTINEL_ENDPOINT", f"tcp:127.0.0.1:{sentinel_port}")
    monkeypatch.setenv("AIAGENT_API_SENTINEL_RPC_KEY", api_key)
    monkeypatch.setenv("AIAGENT_EGRESS_PROXY", f"http://127.0.0.1:{egress_port}")
    monkeypatch.setenv("AIAGENT_BROWSER_ALLOWED_WEB_HOSTS", "127.0.0.1")
    monkeypatch.setenv("AIAGENT_WEB_FETCH_ALLOW_PRIVATE", "127.0.0.1")
    monkeypatch.setenv("AIAGENT_CONTEXT_ARTIFACT_KEYS", json.dumps({"active": "k1", "keys": {"k1": artifact_key}}))

    # Construct create_app() with ZERO injected dependencies (mirrors main() production wiring!)
    app = create_app()
    sentinel_proc: subprocess.Popen[bytes] | None = None
    try:
        with TestClient(app) as client:
            assert (
                client.post(
                    f"/v1/browser/sessions/{sid}/takeover/start", headers=headers, json={"reason": "login"}
                ).status_code
                == 200
            )
            # 1) Sentinel down -> API fails closed with 503 SENTINEL_UNAVAILABLE
            down_r = client.post(
                f"/v1/browser/sessions/{sid}/takeover/authenticate",
                headers=headers,
                json={"url": f"http://127.0.0.1:{http_port}/login-ok", "method": "POST", "formData": {"pw": "GOOD99"}},
            )
            assert down_r.status_code == 503

        # Start real separate Sentinel OS process
        sentinel_proc = _spawn_sentinel()

        # 2) Verify tool_gateway RPC caller CANNOT invoke sentinel.authorize_human_takeover (ACL={'api'})
        wrong_caller_cli = RpcSentinel(
            RpcClient(f"tcp:127.0.0.1:{sentinel_port}", "tool_gateway", bytes.fromhex(tool_key))
        )
        with pytest.raises(RpcError):
            wrong_caller_cli.authorize_human_takeover(
                tenant["tenantId"],
                tenant["userId"],
                tenant["workspaceId"],
                sid,
                Destination("http", "127.0.0.1", http_port, "POST"),
                target_path="/login-ok",
                body_sha256=_sha256(b"pw=GOOD99").hexdigest(),
            )

        with TestClient(app) as client:
            # 3) P2-3 strict auth detection: 302 redirect / password re-prompt / captcha -> authenticated=False!
            r_302 = client.post(
                f"/v1/browser/sessions/{sid}/takeover/authenticate",
                headers=headers,
                json={"url": f"http://127.0.0.1:{http_port}/login-302", "method": "POST", "formData": {"pw": "GOOD99"}},
            )
            assert r_302.status_code == 200 and r_302.json()["authenticated"] is False

            r_pw = client.post(
                f"/v1/browser/sessions/{sid}/takeover/authenticate",
                headers=headers,
                json={"url": f"http://127.0.0.1:{http_port}/login-pw", "method": "POST", "formData": {"pw": "BAD"}},
            )
            assert r_pw.status_code == 200 and r_pw.json()["authenticated"] is False

            r_cap = client.post(
                f"/v1/browser/sessions/{sid}/takeover/authenticate",
                headers=headers,
                json={"url": f"http://127.0.0.1:{http_port}/login-captcha", "method": "POST", "formData": {}},
            )
            assert r_cap.status_code == 200 and r_cap.json()["authenticated"] is False

            # 4) Production RPC authenticate -> EgressProxy -> Set-Cookie persisted in AES-GCM encrypted storageState!
            # Without verifyUrl and no prior session current_url, authStatus MUST stay UNVERIFIED (authenticated=False, P2-3).
            r_unverified = client.post(
                f"/v1/browser/sessions/{sid}/takeover/authenticate",
                headers=headers,
                json={"url": f"http://127.0.0.1:{http_port}/login-ok", "method": "POST", "formData": {"pw": "GOOD99"}},
            )
            assert r_unverified.status_code == 200, r_unverified.text
            assert r_unverified.json()["authStatus"] == "UNVERIFIED"
            assert r_unverified.json()["authenticated"] is False
            assert r_unverified.json()["cookieCount"] == 1

            # With verifyUrl proving authenticated GET /dashboard succeeds -> VERIFIED & authenticated=True!
            r_ok = client.post(
                f"/v1/browser/sessions/{sid}/takeover/authenticate",
                headers=headers,
                json={
                    "url": f"http://127.0.0.1:{http_port}/login-ok",
                    "method": "POST",
                    "formData": {"pw": "GOOD99"},
                    "verifyUrl": f"http://127.0.0.1:{http_port}/dashboard",
                },
            )
            assert r_ok.status_code == 200, r_ok.text
            assert r_ok.json()["authStatus"] == "VERIFIED"
            assert r_ok.json()["authenticated"] is True
            assert r_ok.json()["cookieCount"] == 1

        # 5) P2-1 + P2-2: Mint permit via RpcSentinel, consume once through EgressProxy, restart Sentinel, replay -> 403!
        api_rpc_sentinel = RpcSentinel(RpcClient(f"tcp:127.0.0.1:{sentinel_port}", "api", bytes.fromhex(api_key)))
        body_bytes = b"pw=GOOD99"
        perm = api_rpc_sentinel.authorize_human_takeover(
            tenant["tenantId"],
            tenant["userId"],
            tenant["workspaceId"],
            sid,
            Destination("http", "127.0.0.1", http_port, "POST"),
            target_path="/login-ok",
            body_sha256=_sha256(body_bytes).hexdigest(),
        )
        # Path mismatch rejected by EgressProxy (P2-2)
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei_path:
            _run(
                _takeover_via_forward_proxy(
                    f"http://127.0.0.1:{egress_port}",
                    method="POST",
                    target_url=f"http://127.0.0.1:{http_port}/other",
                    host_header=f"127.0.0.1:{http_port}",
                    permit=perm,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    body=body_bytes,
                )
            )
        assert ei_path.value.status_code == 403

        # Valid single-use request succeeds
        st_once, _, _ = _run(
            _takeover_via_forward_proxy(
                f"http://127.0.0.1:{egress_port}",
                method="POST",
                target_url=f"http://127.0.0.1:{http_port}/login-ok",
                host_header=f"127.0.0.1:{http_port}",
                permit=perm,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                body=body_bytes,
            )
        )
        assert st_once == 200

        # Restart Sentinel process completely -> PostgreSQL durable nonce STILL blocks replay (P2-1)!
        sentinel_proc.terminate()
        sentinel_proc.wait(timeout=10)
        sentinel_proc = _spawn_sentinel()
        with pytest.raises(HTTPException) as ei_restart:
            _run(
                _takeover_via_forward_proxy(
                    f"http://127.0.0.1:{egress_port}",
                    method="POST",
                    target_url=f"http://127.0.0.1:{http_port}/login-ok",
                    host_header=f"127.0.0.1:{http_port}",
                    permit=perm,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    body=body_bytes,
                )
            )
        assert ei_restart.value.status_code == 403
    finally:
        if sentinel_proc is not None:
            sentinel_proc.terminate()
            sentinel_proc.wait(timeout=10)
        plain_srv.shutdown()
        plain_srv.server_close()
        for d in dbs.values():
            d.close()


def test_browser_submit_post_4xx_unknown_never_failed(
    dbs: dict[str, Database], tenant: dict[str, str], tmp_path: Path
) -> None:
    """P1-2: Server receiving POST and returning 409/422 records UNKNOWN (never FAILED) and prevents retry."""
    post_hits: list[str] = []

    class Post4xxHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_GET(self) -> None:
            if self.path in ("/f409", "/f422"):
                act = "/submit-409" if self.path == "/f409" else "/submit-422"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    f"<html><body><form action='{act}' method='POST'>"
                    "<input id='note' name='note' value=''/>"
                    "<button data-testid='go' type='submit'>Go</button></form></body></html>".encode()
                )
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            post_hits.append(f"{self.path}:{self.rfile.read(n).decode('utf-8', 'replace')}")
            self.send_response(409 if self.path == "/submit-409" else 422)
            self.end_headers()
            self.wfile.write(b"conflict_or_invalid_after_side_effect")

    https_srv, port, _, sentinel, _, proxy_url, stop_ev_holder = _make_takeover_tls_and_proxy(
        tmp_path, Post4xxHandler, dbs
    )
    ring = KeyRing.single(b"4" * 32, key_id="p12")
    worker = PlaywrightBrowserWorker()
    executor = BrowserSessionExecutor(
        dbs["tool_gateway"],
        allowed_web_hosts=frozenset({"portal.test"}),
        allow_https=True,
        allow_private_addrs=frozenset({"127.0.0.1"}),
        proxy=proxy_url,
        storage_key_ring=ring,
        worker=worker,
        require_proxy=True,
    )
    gw = ToolGateway(dbs["tool_gateway"], {"browser_fixture": executor}, sentinel=LocalSentinel(sentinel))
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    try:
        for cid, path in (("p12_409", "/f409"), ("p12_422", "/f422")):
            _, cl = _claim_new(dbs, scope, f"post 4xx {cid}", profile="browser")
            url = f"https://portal.test:{port}{path}"
            _propose(dbs, cl, f"{cid}_nav", "browser_navigate", {"url": url})
            nav = _run(gw.submit(cl, f"{cid}_nav", "browser_navigate", {"url": url}))
            sid = nav["result"]["sessionId"]
            args = {
                "sessionId": sid,
                "kind": "test_id",
                "selector": "go",
                "inputs": [{"kind": "css", "selector": "#note", "value": "charge"}],
            }
            _propose(dbs, cl, cid, "browser_submit", args)
            pend = _run(gw.submit(cl, cid, "browser_submit", args))
            apr = dbs["owner"].one(
                "SELECT request_sha256, record_version FROM agent.approval_records WHERE tenant_id=%s AND id=%s",
                (scope.tenant_id, pend["approvalId"]),
            )
            assert apr is not None
            commands.decide_approval(
                dbs["api"], scope, pend["approvalId"], "APPROVED", apr["request_sha256"], apr["record_version"]
            )
            before_n = len(post_hits)
            res = _run(gw.submit(cl, cid, "browser_submit", args))
            assert res["status"] == "UNKNOWN", res
            assert len(post_hits) == before_n + 1
            # Re-submitting same call_id never duplicates the POST
            res2 = _run(gw.submit(cl, cid, "browser_submit", args))
            assert res2["status"] == "UNKNOWN"
            assert len(post_hits) == before_n + 1
            eff = dbs["owner"].one(
                "SELECT outcome_code FROM agent.side_effect_records s JOIN agent.tool_intents t ON t.id=s.tool_intent_id"
                " WHERE t.tenant_id=%s AND t.invocation_key=%s",
                (scope.tenant_id, cid),
            )
            assert eff is not None and eff["outcome_code"] == "UNKNOWN"
    finally:
        executor.close()
        lp, ev = stop_ev_holder[0]
        lp.call_soon_threadsafe(ev.set)
        https_srv.shutdown()
        https_srv.server_close()


def test_chunked_response_bounds_split_malformed_and_disconnect(
    dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    """P1-3: Oversized chunk declarations, malformed chunk lines, split arrival, and mid-stream drops fail closed."""
    import socket

    from aiagent_gateways.executors import ExecutorUnknown
    from fastapi import HTTPException

    raw_mode = {"kind": "huge_decl", "bytes_sent": 0}

    # Raw socket mock proxy so we test exact wire-level chunked framing in _send_via_forward_proxy & _takeover_via_forward_proxy
    srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv_sock.bind(("127.0.0.1", 0))
    srv_sock.listen(16)
    pport = srv_sock.getsockname()[1]
    stop_flag = threading.Event()

    def serve_raw() -> None:
        import time

        while not stop_flag.is_set():
            srv_sock.settimeout(0.2)
            try:
                conn, _ = srv_sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                conn.recv(4096)
                kind = raw_mode["kind"]
                if kind == "huge_decl":
                    # Declares 16MB single chunk (0x1000000) without sending 16MB -> client must fail closed pre-read!
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                        b"1000000\r\nhello_only_partial"
                    )
                elif kind == "split_valid":
                    hdr = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                    conn.sendall(hdr)
                    for piece in (b"5", b"\r\nhe", b"llo", b"\r\n6\r\n ", b"world\r\n0\r\n\r\n"):
                        conn.sendall(piece)
                        time.sleep(0.01)
                elif kind == "bad_hex":
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\nZZZZ\r\nabc\r\n0\r\n\r\n"
                    )
                elif kind == "long_line":
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                        + b"1;"
                        + (b"x" * 700)
                        + b"\r\na\r\n0\r\n\r\n"
                    )
                elif kind == "disconnect":
                    conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n5\r\nhi")
                elif kind == "missing_zero":
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n2\r\nhi\r\n"
                    )
            finally:
                conn.close()

    t = threading.Thread(target=serve_raw, daemon=True)
    t.start()
    proxy_url = f"http://127.0.0.1:{pport}"
    try:
        # 1) Split valid chunked arrives smoothly in both transports
        raw_mode["kind"] = "split_valid"
        r_ok = _run(
            safe_http_get(
                "https://portal.test/chunk-ok",
                proxy=proxy_url,
                proxy_permit="p-dummy",
                max_bytes=2048,
                verify=False,
                require_proxy=True,
            )
        )
        assert r_ok.body == b"hello world"
        st_tk, _, body_tk = _run(
            _takeover_via_forward_proxy(
                proxy_url,
                method="GET",
                target_url="https://portal.test/chunk-ok",
                host_header="portal.test",
                permit="p-dummy",
                headers={},
                body=None,
            )
        )
        assert st_tk == 200 and body_tk == "hello world"

        # 2) Oversized declaration, malformed hex, oversized chunk line, and premature disconnects fail closed
        for bad_kind in ("huge_decl", "bad_hex", "long_line", "disconnect", "missing_zero"):
            raw_mode["kind"] = bad_kind
            with pytest.raises((ExecutorFailed, ExecutorUnknown)):
                _run(
                    safe_http_get(
                        "https://portal.test/chunk-bad",
                        proxy=proxy_url,
                        proxy_permit="p-dummy",
                        max_bytes=2048,
                        verify=False,
                        require_proxy=True,
                    )
                )
            with pytest.raises(HTTPException) as ei_tk:
                _run(
                    _takeover_via_forward_proxy(
                        proxy_url,
                        method="GET",
                        target_url="https://portal.test/chunk-bad",
                        host_header="portal.test",
                        permit="p-dummy",
                        headers={},
                        body=None,
                    )
                )
            assert ei_tk.value.status_code == 502
    finally:
        stop_flag.set()
        srv_sock.close()


def test_browser_preview_cross_session_ownership_non_mutating_and_timeout(
    pg: PgEnv, dbs: dict[str, Database], tenant: dict[str, str], tmp_path: Path
) -> None:
    """P1-4 & P2-4: Cross-tenant/task preview rejected without touching DOM; preview does not .fill(); timeout fails closed."""

    class FormPreviewHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><form action='/ok' method='POST'>"
                b"<input id='note' name='note' value='orig'/>"
                b"<button data-testid='go' type='submit'>Go</button></form></body></html>"
            )

    https_srv, port, _, sentinel, _, proxy_url, stop_ev_holder = _make_takeover_tls_and_proxy(
        tmp_path, FormPreviewHandler, dbs
    )
    ring = KeyRing.single(b"x" * 32, key_id="p14")
    worker = PlaywrightBrowserWorker()
    executor = BrowserSessionExecutor(
        dbs["tool_gateway"],
        allowed_web_hosts=frozenset({"portal.test"}),
        allow_https=True,
        allow_private_addrs=frozenset({"127.0.0.1"}),
        proxy=proxy_url,
        storage_key_ring=ring,
        worker=worker,
        require_proxy=True,
    )
    gw = ToolGateway(dbs["tool_gateway"], {"browser_fixture": executor}, sentinel=LocalSentinel(sentinel))
    scope_a = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    other_t = admin.provision_tenant(pg.owner)
    scope_b = Scope(other_t["tenantId"], other_t["userId"], other_t["workspaceId"])

    try:
        _, cl_a = _claim_new(dbs, scope_a, "owner task A", profile="browser")
        _, cl_a_other_task = _claim_new(dbs, scope_a, "same tenant task B", profile="browser")
        _, cl_b = _claim_new(dbs, scope_b, "cross tenant C", profile="browser")

        url = f"https://portal.test:{port}/"
        _propose(dbs, cl_a, "pv_nav", "browser_navigate", {"url": url})
        nav = _run(gw.submit(cl_a, "pv_nav", "browser_navigate", {"url": url}))
        sid_a = nav["result"]["sessionId"]

        # 1) Cross-task and cross-tenant bind_request previews are rejected BEFORE DOM access; input stays 'orig'
        hijack_args = {
            "sessionId": sid_a,
            "kind": "test_id",
            "selector": "go",
            "inputs": [{"kind": "css", "selector": "#note", "value": "ATTACKED"}],
        }
        _propose(dbs, cl_a_other_task, "pv_hijack1", "browser_submit", hijack_args)
        out_other_task = _run(gw.submit(cl_a_other_task, "pv_hijack1", "browser_submit", hijack_args))
        assert out_other_task["status"] == "FAILED"
        assert "SESSION_NOT_FOUND" in str(out_other_task["result"])

        _propose(dbs, cl_b, "pv_hijack2", "browser_submit", hijack_args)
        out_cross_tenant = _run(gw.submit(cl_b, "pv_hijack2", "browser_submit", hijack_args))
        assert out_cross_tenant["status"] == "FAILED"
        assert "SESSION_NOT_FOUND" in str(out_cross_tenant["result"])

        # 2) Even for legitimate owner A, PENDING_APPROVAL preview MUST NOT mutate DOM #note.value!
        own_args = {
            "sessionId": sid_a,
            "kind": "test_id",
            "selector": "go",
            "inputs": [{"kind": "css", "selector": "#note", "value": "preview_only"}],
        }
        _propose(dbs, cl_a, "pv_own", "browser_submit", own_args)
        pend = _run(gw.submit(cl_a, "pv_own", "browser_submit", own_args))
        assert pend["status"] == "PENDING_APPROVAL"
        dom_val = _run(
            executor._on_persistent_loop(worker._page(sid_a).evaluate("document.getElementById('note').value"))
        )
        assert dom_val == "orig"

        # 3) P2-4: Stopped worker loop, missing claim, cancelled, and timeout map cleanly to ExecutorFailed codes
        with pytest.raises(ExecutorFailed, match="AUTHORITY_REQUIRED"):
            executor.bind_request("browser_submit", own_args)

        import concurrent.futures

        class _FakeFuture:
            def __init__(self, err: BaseException) -> None:
                self._err = err
                self.cancelled = False

            def result(self, timeout: float | None = None) -> Any:
                del timeout
                raise self._err

            def cancel(self) -> bool:
                self.cancelled = True
                return True

        fake_to = _FakeFuture(concurrent.futures.TimeoutError())
        fake_cancel = _FakeFuture(concurrent.futures.CancelledError())
        orig_rct = asyncio.run_coroutine_threadsafe
        cur_fake: list[Any] = [fake_to]

        def _patch_rct(coro: Any, loop: Any) -> Any:
            del loop
            coro.close()
            return cur_fake[0]

        asyncio.run_coroutine_threadsafe = _patch_rct  # type: ignore[assignment]
        try:
            with pytest.raises(ExecutorFailed, match="PREVIEW_TIMEOUT"):
                executor.bind_request("browser_submit", own_args, claim=cl_a)
            assert fake_to.cancelled is True

            cur_fake[0] = fake_cancel
            with pytest.raises(ExecutorFailed, match="PREVIEW_CANCELLED"):
                executor.bind_request("browser_submit", own_args, claim=cl_a)
        finally:
            asyncio.run_coroutine_threadsafe = orig_rct  # type: ignore[assignment]

        # Test closed executor fails closed with SESSION_NOT_FOUND / BROWSER_UNAVAILABLE
        executor.close()
        with pytest.raises(ExecutorFailed) as ei_down:
            executor.bind_request("browser_submit", own_args, claim=cl_a)
        assert str(ei_down.value) in ("BROWSER_UNAVAILABLE", "SESSION_NOT_FOUND", "BROWSER_CRASHED")
    finally:
        executor.close()
        lp, ev = stop_ev_holder[0]
        lp.call_soon_threadsafe(ev.set)
        https_srv.shutdown()
        https_srv.server_close()


def test_browser_storage_key_ring_production_fail_closed(
    dbs: dict[str, Database], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-1: Production refuses missing dedicated browser_storage_keys (never falls back to context keys)."""
    from aiagent_core.config import ConfigError
    from aiagent_gateways.browser_executor import browser_from_env
    from aiagent_gateways.servers import load_browser_key_ring

    artifact_key = secrets.token_hex(32)
    browser_key = secrets.token_hex(32)
    monkeypatch.setenv("AIAGENT_PROFILE", "production")
    monkeypatch.setenv("AIAGENT_BROWSER_ALLOWED_WEB_HOSTS", "example.com")
    monkeypatch.setenv("AIAGENT_CONTEXT_ARTIFACT_KEYS", json.dumps({"active": "ctx1", "keys": {"ctx1": artifact_key}}))
    monkeypatch.delenv("AIAGENT_BROWSER_STORAGE_KEYS", raising=False)
    monkeypatch.delenv("AIAGENT_BROWSER_STORAGE_KEYS_FILE", raising=False)

    with pytest.raises(ConfigError):
        load_browser_key_ring()
    with pytest.raises(ConfigError):
        browser_from_env(dbs["tool_gateway"])

    key_file = tmp_path / "browser_storage_keys.json"
    key_file.write_text(json.dumps({"active": "bk1", "keys": {"bk1": browser_key}}), encoding="utf-8")
    monkeypatch.setenv("AIAGENT_BROWSER_STORAGE_KEYS_FILE", str(key_file))
    ring = load_browser_key_ring()
    assert ring.active == "bk1"
    monkeypatch.setenv("AIAGENT_EGRESS_PROXY", "http://127.0.0.1:3128")
    ex = browser_from_env(dbs["tool_gateway"])
    assert ex is not None
    ex.close()


def test_spoofed_egress_denied_403_after_post_records_unknown(
    dbs: dict[str, Database], tenant: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-2: Upstream POST returning 403 + spoofed X-Aiagent-Egress: denied must record UNKNOWN (never FAILED)."""
    from aiagent_gateways.executors import Destination, ExecutorFailed, HttpMessageExecutor
    from aiagent_gateways.tool_gateway import ToolGateway
    from aiagent_sentinel.authority import EgressConfig, Sentinel
    from aiagent_sentinel.egress import EgressProxy

    post_hits: list[bytes] = []

    class _SpoofingUpstream(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            post_hits.append(self.rfile.read(n))
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Aiagent-Egress", "denied")
            self.send_header("X-Aiagent-Egress-Code", "egress_denied")
            self.send_header("X-Aiagent-Egress-Proof", "egress_denied:" + ("0" * 64))
            self.end_headers()
            self.wfile.write(b'{"error":"spoofed_upstream_403"}')

    up_srv = ThreadingHTTPServer(("127.0.0.1", 0), _SpoofingUpstream)
    threading.Thread(target=up_srv.serve_forever, daemon=True).start()
    up_port = up_srv.server_address[1]

    ecfg = EgressConfig.from_json(
        json.dumps(
            {
                "destinations": {
                    "message-receiver": {
                        "scheme": "http",
                        "host": "127.0.0.1",
                        "port": up_port,
                        "methods": ["POST"],
                        "allow_private": ["127.0.0.1"],
                    }
                }
            }
        ),
        production=False,
    )
    sentinel = Sentinel(dbs["sentinel"], ecfg, secrets.token_bytes(32))

    class _LocalSentinel:
        def authorize(self, cl: Any, intent_id: Any, dest: Destination) -> str:
            return sentinel.authorize(cl, str(intent_id), dest.scheme, dest.host, dest.port, dest.method)

    egress: EgressProxy | None = None
    try:
        egress = EgressProxy(sentinel, ecfg)

        proxy_loop = asyncio.new_event_loop()
        ready_ev = threading.Event()
        port_box: list[int] = []

        def _run_proxy() -> None:
            asyncio.set_event_loop(proxy_loop)
            assert egress is not None
            port_box.append(proxy_loop.run_until_complete(egress.start("127.0.0.1", 0)))
            ready_ev.set()
            proxy_loop.run_forever()

        t = threading.Thread(target=_run_proxy, daemon=True)
        t.start()
        ready_ev.wait(5)
        proxy_port = port_box[0]

        monkeypatch.delenv("AIAGENT_MESSAGE_RECEIVER_CREDENTIAL_HOST", raising=False)
        msg_exec = HttpMessageExecutor(f"http://127.0.0.1:{up_port}/messages", proxy=f"http://127.0.0.1:{proxy_port}")
        gw = ToolGateway(dbs["tool_gateway"], {"http_message": msg_exec}, sentinel=_LocalSentinel())  # type: ignore[arg-type]

        scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
        _, cl = _claim_new(dbs, scope, "spoof 403 post")

        # Approve irreversible send_message first
        args = {"to": "alice", "body": "hello"}
        _propose(dbs, cl, "tc-spoof-1", "send_message", args)
        p1 = _run(gw.submit(cl, "tc-spoof-1", "send_message", args))
        assert p1["status"] == "PENDING_APPROVAL", p1
        with dbs["owner"].tx(tenant=cl.tenant_id) as c:
            c.execute(
                "UPDATE agent.approval_records SET decision_code='APPROVED', decided_by_json=%s::jsonb,"
                " decided_at=now(), record_version=record_version+1"
                " WHERE tenant_id=%s AND id=%s",
                (json.dumps({"id": cl.owner_user_id}), cl.tenant_id, p1["approvalId"]),
            )

        res = _run(gw.submit(cl, "tc-spoof-1", "send_message", args))
        assert res["status"] == "UNKNOWN", res
        assert len(post_hits) == 1

        # Retry MUST return UNKNOWN idempotently without hitting upstream again
        res2 = _run(gw.submit(cl, "tc-spoof-1", "send_message", args))
        assert res2["status"] == "UNKNOWN"
        assert len(post_hits) == 1

        # Contrast with genuine Sentinel pre-send denial through EgressProxy (valid HMAC proof) -> ExecutorFailed
        # Any PermitSent with disallowed destination hits EgressProxy._deny, which signs X-Aiagent-Egress-Proof
        # with sha256(permit), so HttpMessageExecutor must classify it as ExecutorFailed (never ExecutorUnknown).
        probe_perm = "tenant.permit.9999999999.sig"
        mismatch_exec = HttpMessageExecutor(
            "http://wrong.example.test/messages", proxy=f"http://127.0.0.1:{proxy_port}"
        )
        with pytest.raises(ExecutorFailed, match="egress_denied"):
            _run(mismatch_exec.execute(f"{cl.tenant_id}:ek-deny", args, probe_perm))
        assert len(post_hits) == 1
    finally:
        if egress is not None:
            proxy_loop.call_soon_threadsafe(proxy_loop.stop)
        up_srv.shutdown()
        up_srv.server_close()


def test_takeover_nonce_expiry_boundary_exact(
    dbs: dict[str, Database], tenant: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-3: Takeover nonce fails closed at exact t == expires and retains consumed/expired nonce > 1s."""
    import hashlib
    import time

    import aiagent_sentinel.authority as auth_mod
    from aiagent_browser import Authority
    from aiagent_browser.pg_fence import PostgresFencePort
    from aiagent_domain import uuid7
    from aiagent_sentinel.authority import EgressConfig, EgressDenied, Sentinel

    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    _, cl = _claim_new(dbs, scope, "nonce boundary test", profile="browser")
    sid = uuid7()
    auth = Authority(
        cl.tenant_id,
        cl.owner_user_id,
        cl.task_id,
        cl.run_id,
        str(cl.goal_revision),
        str(cl.lease_epoch),
        cl.worker_ref,
    )
    _run(PostgresFencePort(dbs["tool_gateway"]).register(sid, auth))
    with dbs["api"].tx(tenant=cl.tenant_id) as c:
        row = c.execute(
            "SELECT * FROM agent.browser_session_takeover_start(%s,%s,%s,%s)",
            (cl.tenant_id, sid, cl.owner_user_id, "login"),
        ).fetchone()
        assert row is not None

    sentinel = Sentinel(
        dbs["sentinel"],
        EgressConfig.from_json(
            json.dumps(
                {
                    "destinations": {
                        "web-fetch": {
                            "scheme": "https",
                            "host": "portal.example",
                            "port": 443,
                            "methods": ["POST"],
                        }
                    }
                }
            ),
            production=False,
        ),
        secrets.token_bytes(32),
    )
    body_sha = hashlib.sha256(b"x=1").hexdigest()
    monkeypatch.setattr(auth_mod, "PERMIT_TTL_S", 1)
    permit = sentinel.authorize_human_takeover(
        tenant["tenantId"],
        tenant["userId"],
        tenant["workspaceId"],
        sid,
        "https",
        "portal.example",
        443,
        "POST",
        target_path="/login",
        body_sha256=body_sha,
    )
    _, _, _, nonce, expires_s, _ = permit.split(".")
    expires = int(expires_s)

    # Exactly at t == expires -> MUST fail closed (no 1-second boundary acceptance)
    monkeypatch.setattr(auth_mod.time, "time", lambda: float(expires))
    with pytest.raises(EgressDenied, match="PERMIT_EXPIRED"):
        sentinel.verify_human_takeover(
            permit,
            "https",
            "portal.example",
            443,
            "POST",
            "/login",
            body_sha256=body_sha,
        )
    monkeypatch.undo()

    # Wait until real DB clock also passes expires + 0.25s, verify rejected
    while time.time() < expires + 0.25:
        time.sleep(0.05)
    with pytest.raises(EgressDenied, match="PERMIT_EXPIRED"):
        sentinel.verify_human_takeover(
            permit,
            "https",
            "portal.example",
            443,
            "POST",
            "/login",
            body_sha256=body_sha,
        )

    # Valid fresh permit consumed once and retained in human_takeover_nonces -> replay fails
    permit_ok = sentinel.authorize_human_takeover(
        tenant["tenantId"],
        tenant["userId"],
        tenant["workspaceId"],
        sid,
        "https",
        "portal.example",
        443,
        "POST",
        target_path="/login",
        body_sha256=body_sha,
    )
    nonce_ok = permit_ok.split(".")[3]
    sentinel.verify_human_takeover(
        permit_ok,
        "https",
        "portal.example",
        443,
        "POST",
        "/login",
        body_sha256=body_sha,
    )
    with pytest.raises(EgressDenied, match="PERMIT_INVALID_AT_SEND"):
        sentinel.verify_human_takeover(
            permit_ok,
            "https",
            "portal.example",
            443,
            "POST",
            "/login",
            body_sha256=body_sha,
        )
    with dbs["sentinel"].tx() as c:
        row_n = c.execute(
            "SELECT count(*) AS n FROM agent.human_takeover_nonces WHERE nonce=%s", (nonce_ok,)
        ).fetchone()
        assert row_n is not None and int(row_n["n"]) == 1
        del nonce


def test_chunked_proxy_incomplete_trailer_eof_rejected() -> None:
    """P2-1: Chunked HTTP terminating at 0\\r\\n + immediate EOF (without final \\r\\n) must fail closed."""
    import contextlib

    from aiagent_api.app import _takeover_via_forward_proxy
    from aiagent_gateways.executors import ExecutorUnknown
    from aiagent_gateways.web_fetch import _send_via_forward_proxy, parse_safe_http_url
    from fastapi import HTTPException

    parsed_fetch = parse_safe_http_url("http://example.test/data")
    send_complete = [False]

    async def _run_proxy_test(payload: bytes) -> None:
        async def _handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            with contextlib.suppress(Exception):
                await r.readuntil(b"\r\n\r\n")
                w.write(payload)
                await w.drain()
            w.close()

        srv = await asyncio.start_server(_handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        proxy_url = f"http://127.0.0.1:{port}"
        try:
            if not send_complete[0]:
                with pytest.raises(ExecutorUnknown, match="(chunked|IncompleteReadError)"):
                    await _send_via_forward_proxy(
                        proxy_url,
                        method="GET",
                        parsed=parsed_fetch,
                        permit="p",
                        extra_headers={},
                        request_body=None,
                        max_bytes=65536,
                        timeout_s=5.0,
                    )
                with pytest.raises(HTTPException) as ei:
                    await _takeover_via_forward_proxy(
                        proxy_url,
                        method="POST",
                        target_url="http://example.test/login",
                        host_header="example.test",
                        permit="p",
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        body=b"a=1",
                    )
                assert ei.value.status_code == 502
            else:
                st1, _, body1, _ = await _send_via_forward_proxy(
                    proxy_url,
                    method="GET",
                    parsed=parsed_fetch,
                    permit="p",
                    extra_headers={},
                    request_body=None,
                    max_bytes=65536,
                    timeout_s=5.0,
                )
                assert st1 == 200 and body1 == b"ok"
                st2, _, body2 = await _takeover_via_forward_proxy(
                    proxy_url,
                    method="POST",
                    target_url="http://example.test/login",
                    host_header="example.test",
                    permit="p",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    body=b"a=1",
                )
                assert st2 == 200 and body2 == "ok"
        finally:
            srv.close()
            await srv.wait_closed()

    bad = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: text/plain\r\n\r\n2\r\nok\r\n0\r\n"
    _run(_run_proxy_test(bad))
    send_complete[0] = True
    good = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: text/plain\r\n\r\n2\r\nok\r\n0\r\n\r\n"
    _run(_run_proxy_test(good))


def test_preview_submit_requires_non_mutating_worker_signature(
    dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    """P2-2: Workers without apply_inputs parameter in extract_form_submission must fail closed without falling back."""
    from aiagent_browser.contracts import Authority
    from aiagent_gateways.browser_executor import BrowserSessionExecutor
    from aiagent_gateways.executors import ExecutorFailed

    called = {"extract": False}

    class _LegacyWorker:
        async def start(self, session_id: str) -> None:
            del session_id

        async def render(self, session_id: str, html: str) -> str | None:
            del session_id, html
            return None

        async def observe(self, session_id: str) -> str:
            del session_id
            return "Entry"

        async def click(self, session_id: str, target: Any, *, timeout_ms: int = 5000) -> str:
            del session_id, target, timeout_ms
            return "After"

        async def submit(self, session_id: str, target: Any, inputs: Any, *, timeout_ms: int = 5000) -> str:
            del session_id, target, inputs, timeout_ms
            return "Submitted"

        async def extract_form_submission(
            self, session_id: str, target: Any, inputs: Any, *, current_url: str | None = None
        ) -> dict[str, Any]:
            del session_id, target, inputs, current_url
            called["extract"] = True
            return {"hasForm": True, "action": "https://alpha.test/submit", "method": "POST", "fields": []}

        async def close(self, session_id: str) -> None:
            del session_id

    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    _, cl = _claim_new(dbs, scope, "legacy worker check", profile="browser")
    ex = BrowserSessionExecutor(
        dbs["tool_gateway"],
        allowed_web_hosts=frozenset({"alpha.test"}),
        allow_https=True,
        worker=_LegacyWorker(),  # type: ignore[arg-type]
    )
    try:
        auth = Authority(
            cl.tenant_id,
            cl.owner_user_id,
            cl.task_id,
            cl.run_id,
            str(cl.goal_revision),
            str(cl.lease_epoch),
            cl.worker_ref,
        )
        view = _run(ex._on_persistent_loop(ex._service.create(auth)))
        sid = view.session_id
        ex._service._entries[sid].current_url = "https://alpha.test/form"
        with pytest.raises(ExecutorFailed, match="NON_MUTATING_PREVIEW_REQUIRED"):
            ex.bind_request("browser_submit", {"sessionId": sid, "kind": "test_id", "selector": "go"}, claim=cl)
        assert called["extract"] is False
    finally:
        ex.close()


def test_takeover_cookie_scoping_origin_path_secure_and_public_unverified(
    dbs: dict[str, Database], tenant: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-1 & P2: Enforce RFC 6265 Domain/Path/Secure cookie scoping, block cross-origin verifyUrl, and refuse public-200 VERIFIED."""
    from aiagent_api.app import _matching_cookie_header, _parse_set_cookie_records, create_app
    from aiagent_browser import Authority
    from aiagent_browser.pg_fence import PostgresFencePort
    from aiagent_domain import uuid7

    # 1) Direct RFC 6265 Cookie matching unit verification (Host-only, Subdomain, Path boundary, Secure, Expired)
    recs = _parse_set_cookie_records(
        "https://portal-a.test/app/login",
        [
            "c_host=v1; Path=/; HttpOnly",
            "c_dom=v2; Domain=portal-a.test; Path=/app",
            "c_sec=v3; Domain=portal-a.test; Path=/app; Secure",
            "c_admin=v4; Path=/admin",
            "c_exp=v5; Path=/; Max-Age=0",
        ],
    )
    assert _matching_cookie_header(recs, "https://portal-b.test/app/dash") == ""
    assert _matching_cookie_header(recs, "https://evilportal-a.test/app/dash") == ""
    assert _matching_cookie_header(recs, "https://sub.portal-a.test/app/dash") == "c_dom=v2; c_sec=v3"
    assert _matching_cookie_header(recs, "http://sub.portal-a.test/app/dash") == "c_dom=v2"
    assert _matching_cookie_header(recs, "https://portal-a.test/app2") == "c_host=v1"
    assert _matching_cookie_header(recs, "https://portal-a.test/app/dash") == "c_host=v1; c_dom=v2; c_sec=v3"

    # 2) Full HTTP API enforcement with TWO allowed hosts (portal-a.test & portal-b.test)
    _, _, srv_ctx, cli_ctx = _make_self_signed_tls(tmp_path, "portal-a.test")
    b_hits: list[dict[str, Any]] = []
    a_verify_cookies: list[str] = []

    class _PortalAHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_GET(self) -> None:
            ck = self.headers.get("Cookie", "")
            if self.path == "/app/dash":
                a_verify_cookies.append(ck)
                if "c_host=v1" in ck and "c_app=v2" in ck and "c_sec=v3" in ck:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"<html><body>Member Dashboard</body></html>")
                    return
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"<html><body>Auth required</body></html>")
                return
            if self.path == "/app/public":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<html><body>Public marketing page</body></html>")
                return
            if self.path == "/app/soft-gate":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                if "c_host=v1" in ck:
                    self.wfile.write(b"<html><body>MemberOnly Welcome</body></html>")
                else:
                    self.wfile.write(b"<html><body>Guest Preview</body></html>")
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n).decode("utf-8", "replace")
            if self.path == "/app/login" and "pw=GOOD99" in raw:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Set-Cookie", "c_host=v1; Path=/; HttpOnly")
                self.send_header("Set-Cookie", "c_app=v2; Path=/app; HttpOnly")
                self.send_header("Set-Cookie", "c_sec=v3; Path=/app; Secure")
                self.send_header("Set-Cookie", "c_admin=v4; Path=/admin")
                self.end_headers()
                self.wfile.write(b"<html><body>Login accepted</body></html>")
                return
            self.send_response(401)
            self.end_headers()

    class _PortalBHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_GET(self) -> None:
            b_hits.append({"path": self.path, "cookie": self.headers.get("Cookie", "")})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html><body>Collector</body></html>")

    srv_a = ThreadingHTTPServer(("127.0.0.1", 0), _PortalAHandler)
    srv_a.socket = srv_ctx.wrap_socket(srv_a.socket, server_side=True)
    srv_b = ThreadingHTTPServer(("127.0.0.1", 0), _PortalBHandler)
    srv_b.socket = srv_ctx.wrap_socket(srv_b.socket, server_side=True)
    threading.Thread(target=srv_a.serve_forever, daemon=True).start()
    threading.Thread(target=srv_b.serve_forever, daemon=True).start()
    port_a = srv_a.server_address[1]
    port_b = srv_b.server_address[1]

    async def _resolve(host: str, port: int) -> list[str]:
        del port
        return ["127.0.0.1"] if host in ("portal-a.test", "portal-b.test") else []

    from aiagent_sentinel.authority import EgressConfig, Sentinel

    ecfg_a = EgressConfig.from_json(
        json.dumps(
            {
                "destinations": {
                    "web-fetch-a": {
                        "scheme": "https",
                        "host": "portal-a.test",
                        "port": port_a,
                        "methods": ["GET", "POST"],
                        "allow_private": ["127.0.0.1"],
                    },
                    "web-fetch-b": {
                        "scheme": "https",
                        "host": "portal-b.test",
                        "port": port_b,
                        "methods": ["GET", "POST"],
                        "allow_private": ["127.0.0.1"],
                    },
                }
            }
        ),
        production=False,
    )
    monkeypatch.setenv("AIAGENT_WEB_FETCH_ALLOW_PRIVATE", "127.0.0.1")
    sentinel = Sentinel(dbs["sentinel"], ecfg_a, secrets.token_bytes(32))

    ring = KeyRing.single(b"k" * 32, key_id="p11")
    app = create_app(
        dbs["api"],
        storage_key_ring=ring,
        sentinel=LocalSentinel(sentinel),
        egress_proxy="",
        allowed_web_hosts=frozenset({"portal-a.test", "portal-b.test"}),
        takeover_resolver=_resolve,
        takeover_verify_ssl=cli_ctx,
    )
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    _, cl = _claim_new(dbs, scope, "cookie scoping", profile="browser")
    sid = uuid7()
    auth = Authority(
        cl.tenant_id,
        cl.owner_user_id,
        cl.task_id,
        cl.run_id,
        str(cl.goal_revision),
        str(cl.lease_epoch),
        cl.worker_ref,
    )
    _run(PostgresFencePort(dbs["tool_gateway"]).register(sid, auth))
    headers = {"Authorization": f"Bearer {tenant['token']}"}

    try:
        with TestClient(app) as client:
            assert (
                client.post(
                    f"/v1/browser/sessions/{sid}/takeover/start", headers=headers, json={"reason": "login"}
                ).status_code
                == 200
            )
            # a) Cross-origin allowed host verifyUrl MUST be blocked before sending ANY cookies!
            r_cross = client.post(
                f"/v1/browser/sessions/{sid}/takeover/authenticate",
                headers=headers,
                json={
                    "url": f"https://portal-a.test:{port_a}/app/login",
                    "method": "POST",
                    "formData": {"pw": "GOOD99"},
                    "verifyUrl": f"https://portal-b.test:{port_b}/collect",
                },
            )
            assert r_cross.status_code == 422, r_cross.text
            assert b_hits == []

            # b) HTTPS -> HTTP downgrade in verifyUrl MUST be blocked!
            r_down = client.post(
                f"/v1/browser/sessions/{sid}/takeover/authenticate",
                headers=headers,
                json={
                    "url": f"https://portal-a.test:{port_a}/app/login",
                    "method": "POST",
                    "formData": {"pw": "GOOD99"},
                    "verifyUrl": f"http://portal-a.test:{port_a}/app/dash",
                },
            )
            assert r_down.status_code == 422, r_down.text

            # c) P2: Public HTTP 200 page (same response unauthenticated & authenticated) MUST stay UNVERIFIED!
            r_pub = client.post(
                f"/v1/browser/sessions/{sid}/takeover/authenticate",
                headers=headers,
                json={
                    "url": f"https://portal-a.test:{port_a}/app/login",
                    "method": "POST",
                    "formData": {"pw": "GOOD99"},
                    "verifyUrl": f"https://portal-a.test:{port_a}/app/public",
                },
            )
            assert r_pub.status_code == 200, r_pub.text
            assert r_pub.json()["authStatus"] == "UNVERIFIED"
            assert r_pub.json()["authenticated"] is False

            # d) P2: Soft-gate public 200 with explicit verifyContains transition proves VERIFIED!
            r_soft = client.post(
                f"/v1/browser/sessions/{sid}/takeover/authenticate",
                headers=headers,
                json={
                    "url": f"https://portal-a.test:{port_a}/app/login",
                    "method": "POST",
                    "formData": {"pw": "GOOD99"},
                    "verifyUrl": f"https://portal-a.test:{port_a}/app/soft-gate",
                    "verifyContains": "MemberOnly",
                },
            )
            assert r_soft.status_code == 200, r_soft.text
            assert r_soft.json()["authStatus"] == "VERIFIED"
            assert r_soft.json()["authenticated"] is True

            # e) Protected 401 -> 200 verification ONLY sends c_host, c_app, c_sec (never c_admin!)
            a_verify_cookies.clear()
            r_ok = client.post(
                f"/v1/browser/sessions/{sid}/takeover/authenticate",
                headers=headers,
                json={
                    "url": f"https://portal-a.test:{port_a}/app/login",
                    "method": "POST",
                    "formData": {"pw": "GOOD99"},
                    "verifyUrl": f"https://portal-a.test:{port_a}/app/dash",
                },
            )
            assert r_ok.status_code == 200, r_ok.text
            assert r_ok.json()["authStatus"] == "VERIFIED"
            assert r_ok.json()["authenticated"] is True
            # First GET is baseline unauthenticated (""), second GET has matching cookies only (no c_admin)
            assert a_verify_cookies[0] == ""
            assert "c_host=v1" in a_verify_cookies[1]
            assert "c_app=v2" in a_verify_cookies[1]
            assert "c_sec=v3" in a_verify_cookies[1]
            assert "c_admin" not in a_verify_cookies[1]
    finally:
        srv_a.shutdown()
        srv_a.server_close()
        srv_b.shutdown()
        srv_b.server_close()


def test_send_message_post_4xx_records_unknown_no_retry(
    dbs: dict[str, Database], tenant: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-2: Irreversible send_message POST receiving HTTP 400/409/422 must record UNKNOWN (never FAILED) without retry."""
    from aiagent_gateways.executors import Destination, HttpMessageExecutor
    from aiagent_gateways.tool_gateway import ToolGateway
    from aiagent_sentinel.authority import EgressConfig, Sentinel
    from aiagent_sentinel.egress import EgressProxy

    for status_code in (400, 409, 422):
        hits: list[bytes] = []

        class _ErrUpstream(BaseHTTPRequestHandler):
            code = status_code

            def log_message(self, fmt: str, *args: Any) -> None:
                del fmt, args

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length", "0"))
                hits.append(self.rfile.read(n))
                self.send_response(self.code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"accepted_then_4xx"}')

        srv = ThreadingHTTPServer(("127.0.0.1", 0), _ErrUpstream)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        up_port = srv.server_address[1]
        ecfg = EgressConfig.from_json(
            json.dumps(
                {
                    "destinations": {
                        "message-receiver": {
                            "scheme": "http",
                            "host": "127.0.0.1",
                            "port": up_port,
                            "methods": ["POST"],
                            "allow_private": ["127.0.0.1"],
                        }
                    }
                }
            ),
            production=False,
        )
        sentinel = Sentinel(dbs["sentinel"], ecfg, secrets.token_bytes(32))

        class _LocalSentinel:
            def authorize(self, cl: Any, intent_id: Any, dest: Destination) -> str:
                return sentinel.authorize(cl, str(intent_id), dest.scheme, dest.host, dest.port, dest.method)

        egress = EgressProxy(sentinel, ecfg)
        proxy_loop = asyncio.new_event_loop()
        ready_ev = threading.Event()
        port_box: list[int] = []

        def _run_proxy() -> None:
            asyncio.set_event_loop(proxy_loop)
            port_box.append(proxy_loop.run_until_complete(egress.start("127.0.0.1", 0)))
            ready_ev.set()
            proxy_loop.run_forever()

        threading.Thread(target=_run_proxy, daemon=True).start()
        ready_ev.wait(5)
        try:
            monkeypatch.delenv("AIAGENT_MESSAGE_RECEIVER_CREDENTIAL_HOST", raising=False)
            msg_exec = HttpMessageExecutor(
                f"http://127.0.0.1:{up_port}/messages", proxy=f"http://127.0.0.1:{port_box[0]}"
            )
            gw = ToolGateway(dbs["tool_gateway"], {"http_message": msg_exec}, sentinel=_LocalSentinel())  # type: ignore[arg-type]
            scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
            _, cl = _claim_new(dbs, scope, f"msg 4xx {status_code}")
            args = {"to": "alice", "body": f"msg-{status_code}"}
            _propose(dbs, cl, f"tc-4xx-{status_code}", "send_message", args)
            p = _run(gw.submit(cl, f"tc-4xx-{status_code}", "send_message", args))
            assert p["status"] == "PENDING_APPROVAL", p
            with dbs["owner"].tx(tenant=cl.tenant_id) as c:
                c.execute(
                    "UPDATE agent.approval_records SET decision_code='APPROVED', decided_by_json=%s::jsonb,"
                    " decided_at=now(), record_version=record_version+1"
                    " WHERE tenant_id=%s AND id=%s",
                    (json.dumps({"id": cl.owner_user_id}), cl.tenant_id, p["approvalId"]),
                )
            res = _run(gw.submit(cl, f"tc-4xx-{status_code}", "send_message", args))
            assert res["status"] == "UNKNOWN", (status_code, res)
            assert len(hits) == 1

            # Duplicate retry MUST remain UNKNOWN without re-POSTing
            res2 = _run(gw.submit(cl, f"tc-4xx-{status_code}", "send_message", args))
            assert res2["status"] == "UNKNOWN"
            assert len(hits) == 1
        finally:
            proxy_loop.call_soon_threadsafe(proxy_loop.stop)
            srv.shutdown()
            srv.server_close()


def test_send_message_https_through_sentinel_without_connect_and_reconcile(
    dbs: dict[str, Database], tenant: dict[str, str], tmp_path: Path
) -> None:
    """P1-3: HTTPS send_message & reconciliation use single-request forward-proxy (TLS+SNI in Sentinel, no CONNECT)."""
    from aiagent_core.config import Secret
    from aiagent_gateways.executors import Destination, HttpMessageExecutor
    from aiagent_gateways.tool_gateway import ToolGateway
    from aiagent_sentinel.authority import EgressConfig, Sentinel
    from aiagent_sentinel.egress import EgressProxy

    _, _, srv_ctx, cli_ctx = _make_self_signed_tls(tmp_path, "receiver.test")
    seen_posts: list[dict[str, Any]] = []
    receipts: dict[str, str] = {}

    class _TlsReceiverHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_POST(self) -> None:
            if self.path == "/messages-drop":
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                idem = self.headers.get("Idempotency-Key", "")
                seen_posts.append({"body": body, "idem": idem, "auth": self.headers.get("Authorization", "")})
                receipts[idem] = "rcpt_tls_drop_" + str(len(seen_posts))
                self.close_connection = True
                self.connection.close()
                return
            if self.path != "/messages":
                self.send_response(404)
                self.end_headers()
                return
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            idem = self.headers.get("Idempotency-Key", "")
            auth_hdr = self.headers.get("Authorization", "")
            seen_posts.append({"body": body, "idem": idem, "auth": auth_hdr})
            rcpt = "rcpt_tls_" + str(len(seen_posts))
            receipts[idem] = rcpt
            out = json.dumps({"receipt": rcpt}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self) -> None:
            if self.path.startswith("/receipts/"):
                key = self.path[len("/receipts/") :]
                if key in receipts:
                    out = json.dumps({"receipt": receipts[key]}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
            self.send_response(404)
            self.end_headers()

    tls_srv = ThreadingHTTPServer(("127.0.0.1", 0), _TlsReceiverHandler)
    tls_srv.socket = srv_ctx.wrap_socket(tls_srv.socket, server_side=True)
    threading.Thread(target=tls_srv.serve_forever, daemon=True).start()
    tls_port = tls_srv.server_address[1]

    ecfg = EgressConfig.from_json(
        json.dumps(
            {
                "destinations": {
                    "message-receiver": {
                        "scheme": "https",
                        "host": "receiver.test",
                        "port": tls_port,
                        "methods": ["POST", "GET"],
                        "allow_private": ["127.0.0.1"],
                    }
                }
            }
        ),
        production=False,
    )
    sentinel = Sentinel(dbs["sentinel"], ecfg, secrets.token_bytes(32))

    class _LocalSentinel:
        def authorize(self, cl: Any, intent_id: Any, dest: Destination) -> str:
            return sentinel.authorize(cl, str(intent_id), dest.scheme, dest.host, dest.port, dest.method)

        def authorize_reconcile(self, tenant_id: str, intent_id: str, dest: Destination) -> str:
            return sentinel.authorize_reconcile(tenant_id, intent_id, dest.scheme, dest.host, dest.port)

    async def _local_resolver(host: str, port: int) -> list[str]:
        del port
        return ["127.0.0.1"] if host == "receiver.test" else []

    egress = EgressProxy(sentinel, ecfg, resolver=_local_resolver, upstream_ssl=cli_ctx)
    proxy_loop = asyncio.new_event_loop()
    ready_ev = threading.Event()
    port_box: list[int] = []

    def _run_proxy() -> None:
        asyncio.set_event_loop(proxy_loop)
        port_box.append(proxy_loop.run_until_complete(egress.start("127.0.0.1", 0)))
        ready_ev.set()
        proxy_loop.run_forever()

    threading.Thread(target=_run_proxy, daemon=True).start()
    ready_ev.wait(5)
    try:
        proxy_url = f"http://127.0.0.1:{port_box[0]}"
        msg_exec = HttpMessageExecutor(
            f"https://receiver.test:{tls_port}/messages",
            proxy=proxy_url,
            credential=Secret("recv-tls-secret"),
            credential_recipient="receiver.test",
        )
        gw = ToolGateway(dbs["tool_gateway"], {"http_message": msg_exec}, sentinel=_LocalSentinel())  # type: ignore[arg-type]
        scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
        _, cl = _claim_new(dbs, scope, "https send message")
        args = {"to": "alice", "body": "tls hello"}
        _propose(dbs, cl, "tc-tls-msg", "send_message", args)
        p = _run(gw.submit(cl, "tc-tls-msg", "send_message", args))
        assert p["status"] == "PENDING_APPROVAL", p
        with dbs["owner"].tx(tenant=cl.tenant_id) as c:
            c.execute(
                "UPDATE agent.approval_records SET decision_code='APPROVED', decided_by_json=%s::jsonb,"
                " decided_at=now(), record_version=record_version+1"
                " WHERE tenant_id=%s AND id=%s",
                (json.dumps({"id": cl.owner_user_id}), cl.tenant_id, p["approvalId"]),
            )
        res = _run(gw.submit(cl, "tc-tls-msg", "send_message", args))
        assert res["status"] == "CONFIRMED", res
        assert res["result"]["receipt"] == "rcpt_tls_1"
        assert len(seen_posts) == 1
        assert seen_posts[0]["body"] == {"to": "alice", "body": "tls hello"}
        assert seen_posts[0]["auth"] == "Bearer recv-tls-secret"

        # Also prove HTTPS reconciliation GET /receipts/... works via forward-proxy without CONNECT
        drop_exec = HttpMessageExecutor(
            f"https://receiver.test:{tls_port}/messages-drop",
            proxy=proxy_url,
            credential=Secret("recv-tls-secret"),
            credential_recipient="receiver.test",
        )
        drop_gw = ToolGateway(dbs["tool_gateway"], {"http_message": drop_exec}, sentinel=_LocalSentinel())  # type: ignore[arg-type]
        _, cl_drop = _claim_new(dbs, scope, "https reconcile message")
        args_drop = {"to": "alice", "body": "tls reconcile"}
        _propose(dbs, cl_drop, "tc-tls-drop", "send_message", args_drop)
        p_drop = _run(drop_gw.submit(cl_drop, "tc-tls-drop", "send_message", args_drop))
        with dbs["owner"].tx(tenant=cl_drop.tenant_id) as c:
            c.execute(
                "UPDATE agent.approval_records SET decision_code='APPROVED', decided_by_json=%s::jsonb,"
                " decided_at=now(), record_version=record_version+1"
                " WHERE tenant_id=%s AND id=%s",
                (json.dumps({"id": cl_drop.owner_user_id}), cl_drop.tenant_id, p_drop["approvalId"]),
            )
        res_unk = _run(drop_gw.submit(cl_drop, "tc-tls-drop", "send_message", args_drop))
        assert res_unk["status"] == "UNKNOWN", res_unk
        idem_drop = seen_posts[-1]["idem"]
        drop_intent_id = idem_drop.split(":", 1)[1]
        get_perm = sentinel.authorize_reconcile(cl_drop.tenant_id, drop_intent_id, "https", "receiver.test", tls_port)
        rec = _run(drop_exec.query(idem_drop, get_perm))
        assert rec == {"sent": True, "receipt": receipts[idem_drop]}
        reconciled = _run(drop_gw.reconcile(cl_drop.tenant_id, drop_intent_id))
        assert reconciled == {"status": "CONFIRMED"}, reconciled
    finally:
        proxy_loop.call_soon_threadsafe(proxy_loop.stop)
        tls_srv.shutdown()
        tls_srv.server_close()


def test_takeover_storage_state_expiry_samesite_host_domain_and_prepost_verify_url(
    dbs: dict[str, Database], tenant: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-1, P2-1 & P2-2: Pre-POST verifyUrl validation, expires/sameSite storageState persistence, and host-only vs Domain scoping in real Chromium."""
    import time

    from aiagent_api.app import create_app
    from aiagent_browser import Authority
    from aiagent_browser.pg_fence import PostgresFencePort
    from aiagent_domain import uuid7
    from aiagent_gateways.browser_executor import _storage_aad
    from aiagent_sentinel.authority import EgressConfig, Sentinel

    _, _, srv_ctx, cli_ctx = _make_self_signed_tls(tmp_path, "example.test")
    login_post_hits: list[str] = []
    verify_hits: list[str] = []

    class _PortalHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_GET(self) -> None:
            ck = self.headers.get("Cookie", "")
            verify_hits.append(f"{self.path}:{ck}")
            if self.path == "/dash":
                if "h_only=hv" in ck and "d_sub=dv" in ck and "short_strict=s1" in ck:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"<html><body>Authenticated Portal</body></html>")
                    return
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n).decode("utf-8", "replace")
            login_post_hits.append(raw)
            if self.path == "/login" and "pw=GOOD1" in raw:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Set-Cookie", "expired_now=dead; Path=/; Max-Age=0; Secure")
                self.send_header("Set-Cookie", "past_exp=dead2; Path=/; Expires=Wed, 21 Oct 2015 07:28:00 GMT; Secure")
                self.send_header("Set-Cookie", "short_strict=s1; Path=/; Max-Age=2; SameSite=Strict; Secure; HttpOnly")
                self.send_header("Set-Cookie", "long_lax=s2; Path=/; Max-Age=120; SameSite=lax; Secure")
                self.send_header("Set-Cookie", "invalid_ss=s3; Path=/; SameSite=bogus; Secure")
                self.send_header("Set-Cookie", "bad_none=s4; Path=/; SameSite=None")
                self.send_header("Set-Cookie", "h_only=hv; Path=/; Secure; HttpOnly")
                self.send_header("Set-Cookie", "d_sub=dv; Domain=.example.test; Path=/; Secure; HttpOnly")
                self.end_headers()
                self.wfile.write(b"<html><body>Signed In</body></html>")
                return
            self.send_response(401)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _PortalHandler)
    srv.socket = srv_ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    async def _resolve(host: str, port_num: int) -> list[str]:
        del port_num
        return ["127.0.0.1"] if host in ("example.test", "other.test") else []

    ecfg = EgressConfig.from_json(
        json.dumps(
            {
                "destinations": {
                    "portal": {
                        "scheme": "https",
                        "host": "example.test",
                        "port": port,
                        "methods": ["GET", "POST"],
                        "allow_private": ["127.0.0.1"],
                    }
                }
            }
        ),
        production=False,
    )
    monkeypatch.setenv("AIAGENT_WEB_FETCH_ALLOW_PRIVATE", "127.0.0.1")
    sentinel = Sentinel(dbs["sentinel"], ecfg, secrets.token_bytes(32))
    ring = KeyRing.single(b"s" * 32, key_id="p11-ck")
    app = create_app(
        dbs["api"],
        storage_key_ring=ring,
        sentinel=LocalSentinel(sentinel),
        egress_proxy="",
        allowed_web_hosts=frozenset({"example.test", "other.test"}),
        takeover_resolver=_resolve,
        takeover_verify_ssl=cli_ctx,
    )
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    _, cl = _claim_new(dbs, scope, "storageState cookie fidelity", profile="browser")
    sid = uuid7()
    auth = Authority(
        cl.tenant_id,
        cl.owner_user_id,
        cl.task_id,
        cl.run_id,
        str(cl.goal_revision),
        str(cl.lease_epoch),
        cl.worker_ref,
    )
    _run(PostgresFencePort(dbs["tool_gateway"]).register(sid, auth))
    headers = {"Authorization": f"Bearer {tenant['token']}"}

    try:
        with TestClient(app) as client:
            assert (
                client.post(
                    f"/v1/browser/sessions/{sid}/takeover/start", headers=headers, json={"reason": "login"}
                ).status_code
                == 200
            )

            # P2-2 & malformed URL guard: Invalid / cross-origin / downgraded verifyUrl or malformed login url MUST fail 422 BEFORE sending login POST!
            for bad_verify in (
                f"https://other.test:{port}/collect",
                f"http://example.test:{port}/dash",
                "not-a-valid-url",
                "https://example.test:abc/dash",
                "https://example.test:99999/dash",
                "https://[invalid/dash",
            ):
                r_bad = client.post(
                    f"/v1/browser/sessions/{sid}/takeover/authenticate",
                    headers=headers,
                    json={
                        "url": f"https://example.test:{port}/login",
                        "method": "POST",
                        "formData": {"pw": "GOOD1"},
                        "verifyUrl": bad_verify,
                    },
                )
                assert r_bad.status_code == 422, r_bad.text
            for bad_login in (
                "https://example.test:abc/login",
                "https://example.test:99999/login",
                "https://[invalid/login",
            ):
                r_bad_url = client.post(
                    f"/v1/browser/sessions/{sid}/takeover/authenticate",
                    headers=headers,
                    json={
                        "url": bad_login,
                        "method": "POST",
                        "formData": {"pw": "GOOD1"},
                    },
                )
                assert r_bad_url.status_code == 422, r_bad_url.text
                assert r_bad_url.json()["detail"]["error"] == "INVALID_TARGET_URL"
            assert login_post_hits == []
            assert verify_hits == []

            # Valid login + verifyUrl
            r_ok = client.post(
                f"/v1/browser/sessions/{sid}/takeover/authenticate",
                headers=headers,
                json={
                    "url": f"https://example.test:{port}/login",
                    "method": "POST",
                    "formData": {"pw": "GOOD1"},
                    "verifyUrl": f"https://example.test:{port}/dash",
                },
            )
            assert r_ok.status_code == 200, r_ok.text
            assert r_ok.json()["authStatus"] == "VERIFIED"
            assert r_ok.json()["authenticated"] is True
            assert len(login_post_hits) == 1

            r_comp = client.post(f"/v1/browser/sessions/{sid}/takeover/complete", headers=headers)
            assert r_comp.status_code == 200, r_comp.text

        # Decrypt persisted AES-GCM storageState via FencePort & verify fields
        enc = _run(PostgresFencePort(dbs["tool_gateway"]).fetch_storage_state(sid, auth, mode="AGENT"))
        assert enc is not None
        plain = ring.cipher(enc["key_id"]).decrypt(enc["nonce"], enc["ciphertext"], _storage_aad(auth, sid))
        storage_state = json.loads(plain.decode("utf-8"))
        by_name = {c["name"]: c for c in storage_state["cookies"]}

        assert "expired_now" not in by_name
        assert "past_exp" not in by_name
        assert "bad_none" not in by_name

        assert by_name["short_strict"]["sameSite"] == "Strict"
        assert isinstance(by_name["short_strict"]["expires"], (int, float))
        assert by_name["long_lax"]["sameSite"] == "Lax"
        assert float(by_name["long_lax"]["expires"]) > time.time() + 60.0
        assert "sameSite" not in by_name["invalid_ss"]

        # P2-1: Host-only cookie has bare domain; Domain cookie keeps leading dot
        assert by_name["h_only"]["domain"] == "example.test"
        assert by_name["d_sub"]["domain"] == ".example.test"

        # Real Chromium restoration check
        async def _verify_chromium() -> None:
            worker = PlaywrightBrowserWorker()
            try:
                await worker.restore_storage_state(sid, storage_state)
                root_ck = await worker.cookies_for_url(sid, f"https://example.test:{port}/")
                assert "short_strict=s1" in root_ck
                assert "long_lax=s2" in root_ck
                assert "invalid_ss=s3" in root_ck
                assert "h_only=hv" in root_ck
                assert "d_sub=dv" in root_ck

                sub_ck = await worker.cookies_for_url(sid, f"https://sub.example.test:{port}/")
                assert "d_sub=dv" in sub_ck
                assert "h_only=" not in sub_ck

                evil_ck = await worker.cookies_for_url(sid, f"https://evil-example.test:{port}/")
                assert evil_ck == ""

                exported = await worker.export_storage_state(sid)
                exp_by_name = {c["name"]: c for c in exported["cookies"]}
                assert exp_by_name["short_strict"]["sameSite"] == "Strict"
                assert float(exp_by_name["short_strict"]["expires"]) > 0.0

                # Wait past short_strict Max-Age=2 and verify Chromium rejects expired cookie on restore
                await asyncio.sleep(2.4)
                await worker.restore_storage_state(sid, storage_state)
                root_after = await worker.cookies_for_url(sid, f"https://example.test:{port}/")
                assert "short_strict=" not in root_after
                assert "long_lax=s2" in root_after
            finally:
                await worker.shutdown()

        _run(_verify_chromium())
    finally:
        srv.shutdown()
        srv.server_close()
