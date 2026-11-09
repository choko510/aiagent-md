"""Sentinel process: permit authority RPC + Trusted Egress proxy. `python -m aiagent_sentinel.main`.

Production deploys the authority and the egress transport in the same trusted network namespace with the ONLY
outbound route (deploy/compose: `egress` network; deploy/nftables). Fails closed in production without a permit key,
an egress config, or with any `allow_private` destination.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from aiagent_core import config
from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_core.logging import setup
from aiagent_core.rpc import Endpoint, RpcServer, parse_keys
from aiagent_core.service import Lifecycle
from aiagent_domain.rpcwire import RpcError

from .authority import Sentinel, load_config
from .egress import EgressProxy


def build_server(keys: dict[str, bytes], endpoint: str, s: Sentinel) -> RpcServer:
    srv = RpcServer("sentinel", Endpoint(endpoint), keys)

    async def authorize(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            claim = Claim.from_wire(body["claim"])
            args = (str(body["intentId"]), str(body["scheme"]), str(body["host"]), int(body["port"]), str(body["method"]))
        except Exception:
            raise RpcError("BAD_REQUEST") from None
        return {"permit": await asyncio.to_thread(s.authorize, claim, *args)}

    async def authorize_reconcile(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"permit": await asyncio.to_thread(s.authorize_reconcile, str(body["tenantId"]), str(body["intentId"]),
                                                  str(body["scheme"]), str(body["host"]), int(body["port"]))}

    srv.register("sentinel.authorize", authorize, {"tool_gateway"})
    srv.register("sentinel.authorize_reconcile", authorize_reconcile, {"tool_gateway"})
    return srv


async def serve() -> None:
    setup("sentinel")
    cfg = load_config()
    config.require_production_safety("sentinel", {"egress destinations configured": bool(cfg.destinations)})
    key = bytes.fromhex(config.secret("AIAGENT_SENTINEL_PERMIT_KEY").reveal())  # type: ignore[union-attr]
    db = Database(config.secret("AIAGENT_SENTINEL_DSN").reveal(), max_size=8, name="sentinel")  # type: ignore[union-attr]
    s = Sentinel(db, cfg, key)
    srv = build_server(parse_keys(config.secret("AIAGENT_SENTINEL_RPC_KEYS").reveal()),  # type: ignore[union-attr]
                       config.env("AIAGENT_SENTINEL_ENDPOINT"), s)
    proxy = EgressProxy(s, cfg)
    life = Lifecycle("sentinel", health_port=config.env_int("AIAGENT_HEALTH_PORT", 0) or None,
                     health_host=os.environ.get("AIAGENT_HEALTH_HOST", "127.0.0.1"))
    life.install_signals()
    await srv.start()
    host, _, port = config.env("AIAGENT_EGRESS_LISTEN", "127.0.0.1:3128").rpartition(":")
    await proxy.start(host, int(port))
    await life.start_health()
    await life.stopping.wait()
    await proxy.close()
    await srv.close()
    await life.close()
    db.close()


if __name__ == "__main__":
    asyncio.run(serve())
