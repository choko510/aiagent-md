"""How the Tool Gateway reaches Sentinel: authenticated RPC in deployment, a direct object in unit tests."""

from __future__ import annotations

import asyncio
from typing import Any

from aiagent_core.control import Claim
from aiagent_core.rpc import RpcClient

from .executors import Destination


class RpcSentinel:
    def __init__(self, client: RpcClient) -> None:
        self.client = client

    def authorize(self, claim: Claim, intent_id: str, dest: Destination) -> str:
        res = asyncio.run(self.client.call("sentinel.authorize", {
            "claim": claim.to_wire(), "intentId": intent_id, "scheme": dest.scheme, "host": dest.host,
            "port": dest.port, "method": dest.method}))
        return str(res["permit"])

    def authorize_reconcile(self, tenant_id: str, intent_id: str, dest: Destination) -> str:
        res = asyncio.run(self.client.call("sentinel.authorize_reconcile", {
            "tenantId": tenant_id, "intentId": intent_id, "scheme": dest.scheme, "host": dest.host,
            "port": dest.port}))
        return str(res["permit"])


class LocalSentinel:
    def __init__(self, sentinel: Any) -> None:
        self.s = sentinel

    def authorize(self, claim: Claim, intent_id: str, dest: Destination) -> str:
        return str(self.s.authorize(claim, intent_id, dest.scheme, dest.host, dest.port, dest.method))

    def authorize_reconcile(self, tenant_id: str, intent_id: str, dest: Destination) -> str:
        return str(self.s.authorize_reconcile(tenant_id, intent_id, dest.scheme, dest.host, dest.port))
