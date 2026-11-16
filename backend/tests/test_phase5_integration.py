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

import pytest
from aiagent_api.app import create_app
from aiagent_browser import PlaywrightBrowserWorker
from aiagent_core import admin, commands, control, memory_tombstones, memory_writer
from aiagent_core.commands import Scope
from aiagent_core.config import Secret
from aiagent_core.db import Database
from aiagent_gateways.browser_executor import BrowserSessionExecutor
from aiagent_gateways.context_keys import KeyRing
from aiagent_gateways.executors import ExecutorFailed
from aiagent_gateways.model_gateway import ModelGateway, Route, RouteBreaker, RouteRates, SealedModelRequest
from aiagent_gateways.providers import GeminiProvider, OpenAIProvider, ProviderError, ProviderEvent
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
