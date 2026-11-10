"""STEP 3-E: Sentinel permits and the Trusted Egress proxy (SSRF, DNS rebinding, permit binding, approval revocation).
The proxy is exercised for real over TCP; DNS answers are injected through the resolver hook."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import secrets

import httpx
import pytest
from aiagent_core import commands, control
from aiagent_core.commands import Scope
from aiagent_core.db import Database
from aiagent_domain.rpcwire import RpcError
from aiagent_sentinel.authority import EgressConfig, Sentinel
from aiagent_sentinel.egress import EgressProxy
from aiagent_sentinel.netpolicy import EgressDenied, is_public, normalize_host, vet_addresses
from conftest import PgEnv


# ---------------------------------------------------------------- pure address policy
@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "255.255.255.255",
        "198.18.0.1",
        "::1",
        "::",
        "fe80::1",
        "fd00::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "64:ff9b::a9fe:a9fe",
        "2002:7f00:1::1",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",
        "ff02::1",
        "2001:db8::1",
    ],
)
def test_private_and_special_addresses_blocked(ip: str) -> None:
    assert not is_public(ipaddress.ip_address(ip))


@pytest.mark.parametrize("ip", ["93.184.216.34", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_allowed(ip: str) -> None:
    assert is_public(ipaddress.ip_address(ip))


@pytest.mark.parametrize(
    "host",
    [
        "user@evil.com",
        "evil.com.",
        "2130706433",
        "0x7f000001",
        "evil.com/x",
        "ex%61mple.com",
        "exämple.com",
        "a..b",
        "-bad.com",
        "a b",
    ],
)
def test_bad_hostnames_rejected(host: str) -> None:
    with pytest.raises(EgressDenied):
        normalize_host(host)


def test_one_private_answer_rejects_whole_destination() -> None:
    with pytest.raises(EgressDenied, match="PRIVATE"):
        vet_addresses(["93.184.216.34", "10.0.0.5"])
    assert vet_addresses(["127.0.0.1"], allow_private=frozenset({"127.0.0.1"})) == ["127.0.0.1"]
    with pytest.raises(EgressDenied):
        vet_addresses([])


def test_allow_private_forbidden_in_production() -> None:
    cfg = {
        "destinations": {
            "x": {
                "scheme": "http",
                "host": "127.0.0.1",
                "port": 80,
                "methods": ["POST"],
                "allow_private": ["127.0.0.1"],
            }
        }
    }
    with pytest.raises(ValueError, match="production"):
        EgressConfig.from_json(json.dumps(cfg), production=True)


# ---------------------------------------------------------------- proxy over real TCP + DB permits
class Upstream:
    def __init__(self) -> None:
        self.hits: list[bytes] = []

    async def start(self) -> int:
        async def h(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            data = await r.readuntil(b"\r\n\r\n")
            n = int(
                next(
                    (ln.split(b":")[1] for ln in data.split(b"\r\n") if ln.lower().startswith(b"content-length")), b"0"
                )
            )
            body = await r.readexactly(n) if n else b""
            self.hits.append(data + body)
            out = b'{"receipt":"r1"}'
            w.write(b"HTTP/1.1 200 OK\r\ncontent-length: %d\r\n\r\n%s" % (len(out), out))
            await w.drain()
            w.close()

        self.srv = await asyncio.start_server(h, "127.0.0.1", 0)
        return int(self.srv.sockets[0].getsockname()[1])


@pytest.fixture()
def sentinel_env(pg: PgEnv, dbs: dict[str, Database], tenant: dict[str, str]) -> dict:
    """A run with an APPROVED send_message intent in SENDING (as after the Tool Gateway's final admission)."""
    s = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    ids = commands.submit_task(dbs["api"], s, "notify")
    claim = control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "w1")
    intent, apr = None, None
    with dbs["owner"].tx() as c:
        from aiagent_domain import uuid7

        intent, apr = uuid7(), uuid7()
        c.execute(
            "INSERT INTO agent.tool_intents(tenant_id, id, task_id, run_id, tool_key, invocation_key, request_sha256,"
            " safe_arguments_json, risk_class, goal_revision, status_code) VALUES (%s,%s,%s,%s,'send_message','c1',"
            " repeat('a',64), '{}', 'external_irreversible', 1, 'SENDING')",
            (tenant["tenantId"], intent, ids["taskId"], ids["runId"]),
        )
        c.execute(
            "INSERT INTO agent.approval_records(tenant_id, id, tool_intent_id, task_id, run_id, owner_user_id, tool_key,"
            " request_sha256, goal_revision, safe_summary, decision_code, expires_at) VALUES"
            " (%s,%s,%s,%s,%s,%s,'send_message',repeat('a',64),1,'x','APPROVED', now() + interval '1h')",
            (tenant["tenantId"], apr, intent, ids["taskId"], ids["runId"], tenant["userId"]),
        )
    return {"claim": claim, "intent": intent, "approval": apr, "ids": ids}


async def _proxy_case(
    pg: PgEnv,
    env: dict,
    *,
    resolver_answers: list[str] | None = None,
    mutate: str | None = None,
    reuse: bool = False,
    other_port: bool = False,
    connect: bool = False,
    no_auth: bool = False,
) -> tuple[int, list[bytes], str]:
    up = Upstream()
    port = await up.start()
    cfg = EgressConfig.from_json(
        json.dumps(
            {
                "destinations": {
                    "message-receiver": {
                        "scheme": "http",
                        "host": "receiver.test",
                        "port": port,
                        "methods": ["POST", "GET"],
                        "allow_private": ["127.0.0.1"],
                    }
                }
            }
        ),
        production=False,
    )
    db = Database(pg.dsn["sentinel"], max_size=4)
    sen = Sentinel(db, cfg, secrets.token_bytes(32))

    async def resolver(host: str, p: int) -> list[str]:
        return resolver_answers if resolver_answers is not None else ["127.0.0.1"]

    proxy = EgressProxy(sen, cfg, resolver=resolver)
    pport = await proxy.start("127.0.0.1", 0)
    permit = await asyncio.to_thread(sen.authorize, env["claim"], env["intent"], "http", "receiver.test", port, "POST")
    owner = Database(pg.owner, max_size=1)
    if mutate == "revoke":
        owner.one(
            "UPDATE agent.approval_records SET decision_code='REVOKED' WHERE id=%s RETURNING 1", (env["approval"],)
        )
    if mutate == "cancel":
        owner.one("UPDATE agent.runs SET cancel_requested=true WHERE id=%s RETURNING 1", (env["ids"]["runId"],))
    status, body = 0, ""
    target_port = port + 1 if other_port else port
    headers = {} if no_auth else {"Proxy-Authorization": f"Aiagent tool_gateway:{permit}"}
    try:
        async with httpx.AsyncClient(
            proxy=httpx.Proxy(f"http://127.0.0.1:{pport}", headers=headers), timeout=10, trust_env=False
        ) as c:
            for _ in range(2 if reuse else 1):
                url = f"{'https' if connect else 'http'}://receiver.test:{target_port}/messages"
                try:
                    r = await c.post(url, json={"to": "a", "body": "b"})
                    status, body = r.status_code, r.text
                except httpx.HTTPError as e:
                    status, body = -1, type(e).__name__
    finally:
        await proxy.close()
        up.srv.close()
        db.close()
        owner.close()
    return status, up.hits, body


def run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


@pytest.mark.pg
def test_permit_allows_bound_destination_once(pg: PgEnv, sentinel_env: dict) -> None:
    status, hits, _ = run(_proxy_case(pg, sentinel_env))
    assert status == 200 and len(hits) == 1
    assert b"proxy-authorization" not in hits[0].lower()  # permit never forwarded upstream


@pytest.mark.pg
def test_permit_single_use(pg: PgEnv, sentinel_env: dict) -> None:
    status, hits, body = run(_proxy_case(pg, sentinel_env, reuse=True))
    assert len(hits) == 1 and status == 403 and "PERMIT_INVALID_AT_SEND" in body


@pytest.mark.pg
@pytest.mark.parametrize("mutate", ["revoke", "cancel"])
def test_revocation_or_cancel_after_permit_blocks_send(pg: PgEnv, sentinel_env: dict, mutate: str) -> None:
    status, hits, body = run(_proxy_case(pg, sentinel_env, mutate=mutate))
    assert status == 403 and hits == [] and "PERMIT_INVALID_AT_SEND" in body


@pytest.mark.pg
def test_permit_bound_to_destination(pg: PgEnv, sentinel_env: dict) -> None:
    status, hits, body = run(_proxy_case(pg, sentinel_env, other_port=True))
    assert status == 403 and hits == [] and "DESTINATION_NOT_ALLOWED" in body


@pytest.mark.pg
def test_dns_rebinding_to_private_denied(pg: PgEnv, sentinel_env: dict) -> None:
    status, hits, body = run(_proxy_case(pg, sentinel_env, resolver_answers=["93.184.216.34", "10.0.0.7"]))
    assert status == 403 and hits == [] and "PRIVATE_ADDRESS" in body


@pytest.mark.pg
def test_metadata_ip_denied(pg: PgEnv, sentinel_env: dict) -> None:
    status, hits, body = run(_proxy_case(pg, sentinel_env, resolver_answers=["169.254.169.254"]))
    assert status == 403 and hits == []


@pytest.mark.pg
def test_unauthenticated_and_tunnel_denied(pg: PgEnv, sentinel_env: dict) -> None:
    status, hits, _ = run(_proxy_case(pg, sentinel_env, no_auth=True))
    assert status == 403 and hits == []
    status, hits, _ = run(_proxy_case(pg, sentinel_env, connect=True))  # CONNECT to an http-only destination
    assert status != 200 and hits == []


@pytest.mark.pg
def test_sentinel_refuses_unadmitted_or_stale(pg: PgEnv, dbs: dict[str, Database], sentinel_env: dict) -> None:
    cfg = EgressConfig.from_json(
        json.dumps(
            {
                "destinations": {
                    "message-receiver": {"scheme": "http", "host": "receiver.test", "port": 9, "methods": ["POST"]}
                }
            }
        ),
        production=False,
    )
    sen = Sentinel(dbs["sentinel"], cfg, secrets.token_bytes(32))
    with pytest.raises(RpcError, match="DESTINATION_NOT_ALLOWED"):
        sen.authorize(sentinel_env["claim"], sentinel_env["intent"], "http", "evil.test", 9, "POST")
    dbs["owner"].one(
        "UPDATE agent.tool_intents SET status_code='PENDING_APPROVAL' WHERE id=%s RETURNING 1",
        (sentinel_env["intent"],),
    )
    with pytest.raises(RpcError, match="INTENT_NOT_ADMITTED"):
        sen.authorize(sentinel_env["claim"], sentinel_env["intent"], "http", "receiver.test", 9, "POST")
    dbs["owner"].one(
        "UPDATE agent.runs SET lease_expires_at = now() - interval '1s' WHERE id=%s RETURNING 1",
        (sentinel_env["ids"]["runId"],),
    )
    control.claim_run(dbs["runtime"], sentinel_env["claim"].tenant_id, sentinel_env["ids"]["runId"], "w2")
    with pytest.raises(RpcError, match="FENCED"):  # stale worker's claim
        sen.authorize(sentinel_env["claim"], sentinel_env["intent"], "http", "receiver.test", 9, "POST")


# ---------------------------------------------------------------- Model Gateway provider path (standing destination)
SSE = (
    'data: {"type":"message_start","message":{"usage":{"input_tokens":11}}}\n\n'
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"secret plan"}}\n\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}\n\n'
    'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"tu1","name":"search"}}\n\n'
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"query\\":"}}\n\n'
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\\"w\\"}"}}\n\n'
    'data: {"type":"content_block_stop","index":1}\n\n'
    'data: {"type":"message_delta","usage":{"output_tokens":7}}\n\n'
    'data: {"type":"message_stop"}\n\n'
)


async def _provider_case(token: str) -> tuple[list, list[bytes]]:
    from aiagent_core.config import Secret
    from aiagent_gateways.providers import AnthropicProvider, ProviderError

    hits: list[bytes] = []

    async def h(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        head = await r.readuntil(b"\r\n\r\n")
        n = int(next(ln.split(b":")[1] for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-length")))
        hits.append(head + await r.readexactly(n))
        body = SSE.encode()
        w.write(
            b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ncontent-length: %d\r\n\r\n%s" % (len(body), body)
        )
        await w.drain()
        w.close()

    up = await asyncio.start_server(h, "127.0.0.1", 0)
    port = up.sockets[0].getsockname()[1]
    good = "m" * 40
    cfg = EgressConfig.from_json(
        json.dumps(
            {
                "destinations": {
                    "anthropic": {
                        "scheme": "http",
                        "host": "provider.test",
                        "port": port,
                        "methods": ["POST"],
                        "identities": ["model_gateway"],
                        "needs_permit": False,
                        "allow_private": ["127.0.0.1"],
                    }
                }
            }
        ),
        production=False,
    )
    cfg.static_tokens["model_gateway"] = good

    class NoDb:
        def receipt(self, *a: object) -> None:
            return None

    async def resolver(host: str, p: int) -> list[str]:
        return ["127.0.0.1"]

    proxy = EgressProxy(NoDb(), cfg, resolver=resolver)  # type: ignore[arg-type]
    pport = await proxy.start("127.0.0.1", 0)
    prov = AnthropicProvider(
        Secret("sk-test-key-000000"),
        f"http://127.0.0.1:{pport}",
        proxy_token=Secret(token),
        url=f"http://provider.test:{port}/v1/messages",
    )
    events: list = []
    try:
        async for ev in prov.stream("m", "sys", [{"kind": "user", "content": "hi"}], []):
            events.append(ev)
    except ProviderError as e:
        events.append(("error", e.code))
    finally:
        await proxy.close()
        up.close()
    return events, hits


def test_model_provider_streams_through_egress_with_identity() -> None:
    events, hits = run(_provider_case("m" * 40))
    kinds = [e.type for e in events]
    assert "text" in kinds and "reasoning" in kinds and "tool_end" in kinds and kinds[-1] == "stop"
    assert any(e.type == "usage" and e.usage.get("input_tokens") == 11 for e in events)
    assert len(hits) == 1 and b"x-api-key: sk-test-key" in hits[0].lower()  # key reaches the provider only
    assert b"proxy-authorization" not in hits[0].lower()


def test_model_provider_wrong_proxy_identity_refused() -> None:
    events, hits = run(_provider_case("wrong-token-" + "x" * 30))
    assert hits == [] and events == [("error", "HTTP_403")]
