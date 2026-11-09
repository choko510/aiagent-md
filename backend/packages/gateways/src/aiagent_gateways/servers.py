"""Process entry points: `python -m aiagent_gateways.servers model|tool`.

Each gateway is its own OS process with its own DB login (least privilege), its own RPC keys and its own secrets.
Model Gateway holds provider keys; Tool Gateway holds receiver credentials; neither is reachable without a per-caller
RPC key. Production start-up fails closed when a safety prerequisite is missing.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from typing import Any

from aiagent_core import config
from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_core.logging import register_secret, setup
from aiagent_core.migrate import check_compatible
from aiagent_core.rpc import Endpoint, RpcClient, RpcServer, parse_keys
from aiagent_core.service import Lifecycle
from aiagent_domain.rpcwire import RpcError

from .executors import from_env as executors_from_env
from .model_gateway import ModelGateway, Route, SealedModelRequest
from .providers import AnthropicProvider, FakeModelProvider
from .sentinel_port import RpcSentinel
from .tool_gateway import ToolGateway


def build_model_server(db: Database, keys: dict[str, bytes], endpoint: str, gw: ModelGateway) -> RpcServer:
    srv = RpcServer("model_gateway", Endpoint(endpoint), keys)

    async def complete(caller: str, body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        req = SealedModelRequest.from_wire(body)
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def on_delta(text: str) -> None:
            await queue.put({"delta": text, "provisional": True})

        async def run() -> dict[str, Any]:
            try:
                return await gw.complete(req, on_delta if body.get("streamDeltas") is True else None)
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())
        while (item := await queue.get()) is not None:
            yield item
        yield {**(await task), "__final__": True}

    srv.register("model.complete", complete, {"runtime"}, streaming=True)
    return srv


def build_tool_server(keys: dict[str, bytes], endpoint: str, gw: ToolGateway) -> RpcServer:
    srv = RpcServer("tool_gateway", Endpoint(endpoint), keys)

    def claim_of(body: dict[str, Any]) -> Claim:
        try:
            return Claim.from_wire(body["claim"])
        except Exception:
            raise RpcError("BAD_REQUEST", "claim") from None

    async def submit(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        args = body.get("args")
        if not isinstance(args, dict) or not isinstance(body.get("toolCallId"), str) or not isinstance(body.get("tool"), str):
            raise RpcError("BAD_REQUEST")
        return await gw.submit(claim_of(body), body["toolCallId"], body["tool"], args)

    async def record_internal(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(gw.record_internal, claim_of(body), str(body["toolCallId"]), str(body["tool"]),
                                       str(body["content"]))

    async def reconcile(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        return await gw.reconcile(str(body["tenantId"]), str(body["intentId"]))

    srv.register("tool.submit", submit, {"runtime"})
    srv.register("tool.record_internal", record_internal, {"runtime"})
    srv.register("tool.reconcile", reconcile, {"ops"})
    return srv


async def _serve(name: str, srv: RpcServer, db: Database, health_port: int | None) -> None:
    life = Lifecycle(name, health_port=health_port, health_host=os.environ.get("AIAGENT_HEALTH_HOST", "127.0.0.1"))

    async def db_ready() -> tuple[bool, str]:
        def check() -> tuple[bool, str]:
            with db.conn() as c:
                check_compatible(c)
            return True, "ok"
        return await asyncio.to_thread(check)

    life.checks["database"] = db_ready
    life.install_signals()
    await srv.start()
    await life.start_health()
    await life.stopping.wait()
    await srv.close(grace_s=float(os.environ.get("AIAGENT_DRAIN_S", "20")))
    await life.close()
    db.close()


def main_model() -> None:
    setup("model_gateway")
    keys = parse_keys(config.secret("AIAGENT_MODEL_GATEWAY_RPC_KEYS").reveal())  # type: ignore[union-attr]
    dsn = config.secret("AIAGENT_MODEL_GATEWAY_DSN").reveal()  # type: ignore[union-attr]
    providers: dict[str, Any] = {"fake": FakeModelProvider()}
    routes: dict[str, list[Route]] = {"fake-gateway": [Route("fake", "fake-1")]}
    anth = config.secret("AIAGENT_ANTHROPIC_API_KEY", required=False)
    secrets = []
    if anth is not None:
        register_secret(anth.reveal())
        secrets.append(anth.reveal())
        providers["anthropic"] = AnthropicProvider(anth, os.environ.get("AIAGENT_EGRESS_PROXY_MODEL"))
        routes["claude-sonnet-5-5"] = [Route("anthropic", "claude-sonnet-5-5"), Route("anthropic", "claude-haiku-5-5")]
    if config.profile() == "production" and providers.keys() == {"fake"}:
        print("model_gateway: no real provider configured (FakeModel only) - refusing to start in production",
              file=sys.stderr)
        raise SystemExit(2)
    consent = {tuple(x.split(":", 1)) for x in os.environ.get("AIAGENT_MODEL_CONSENT", "").split(",") if ":" in x}
    db = Database(dsn, max_size=8, name="model_gateway")
    gw = ModelGateway(db, providers, routes, consent=consent, secrets=secrets)  # type: ignore[arg-type]
    srv = build_model_server(db, keys, config.env("AIAGENT_MODEL_GATEWAY_ENDPOINT"), gw)
    asyncio.run(_serve("model_gateway", srv, db, config.env_int("AIAGENT_HEALTH_PORT", 0) or None))


def main_tool() -> None:
    setup("tool_gateway")
    keys = parse_keys(config.secret("AIAGENT_TOOL_GATEWAY_RPC_KEYS").reveal())  # type: ignore[union-attr]
    dsn = config.secret("AIAGENT_TOOL_GATEWAY_DSN").reveal()  # type: ignore[union-attr]
    sentinel = None
    if os.environ.get("AIAGENT_SENTINEL_ENDPOINT"):
        skey = bytes.fromhex(config.secret("AIAGENT_SENTINEL_CLIENT_KEY").reveal())  # type: ignore[union-attr]
        sentinel = RpcSentinel(RpcClient(os.environ["AIAGENT_SENTINEL_ENDPOINT"], "tool_gateway", skey))
    config.require_production_safety("tool_gateway", {
        "Sentinel endpoint configured": sentinel is not None,
        "egress proxy configured": bool(os.environ.get("AIAGENT_EGRESS_PROXY")),
    })
    db = Database(dsn, max_size=8, name="tool_gateway")
    gw = ToolGateway(db, executors_from_env(), sentinel=sentinel)
    srv = build_tool_server(keys, config.env("AIAGENT_TOOL_GATEWAY_ENDPOINT"), gw)
    asyncio.run(_serve("tool_gateway", srv, db, config.env_int("AIAGENT_HEALTH_PORT", 0) or None))


if __name__ == "__main__":
    {"model": main_model, "tool": main_tool}[sys.argv[1]]()
