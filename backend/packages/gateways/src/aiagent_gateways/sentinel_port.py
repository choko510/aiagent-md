"""How the Tool Gateway reaches Sentinel: authenticated RPC in deployment, a direct object in unit tests."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from typing import Any

from aiagent_core.control import Claim
from aiagent_core.rpc import RpcClient

from .executors import Destination

_EMPTY_BODY_SHA256 = sha256(b"").hexdigest()


class RpcSentinel:
    def __init__(self, client: RpcClient) -> None:
        self.client = client

    def authorize(self, claim: Claim, intent_id: str, dest: Destination) -> str:
        res = asyncio.run(
            self.client.call(
                "sentinel.authorize",
                {
                    "claim": claim.to_wire(),
                    "intentId": intent_id,
                    "scheme": dest.scheme,
                    "host": dest.host,
                    "port": dest.port,
                    "method": dest.method,
                },
            )
        )
        return str(res["permit"])

    def authorize_reconcile(self, tenant_id: str, intent_id: str, dest: Destination) -> str:
        res = asyncio.run(
            self.client.call(
                "sentinel.authorize_reconcile",
                {
                    "tenantId": tenant_id,
                    "intentId": intent_id,
                    "scheme": dest.scheme,
                    "host": dest.host,
                    "port": dest.port,
                },
            )
        )
        return str(res["permit"])

    def authorize_human_takeover(
        self,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        dest: Destination,
        *,
        target_path: str = "/",
        body_sha256: str | None = None,
    ) -> str:
        res = asyncio.run(
            self.client.call(
                "sentinel.authorize_human_takeover",
                {
                    "tenantId": tenant_id,
                    "userId": user_id,
                    "workspaceId": workspace_id,
                    "sessionId": session_id,
                    "scheme": dest.scheme,
                    "host": dest.host,
                    "port": dest.port,
                    "method": dest.method,
                    "targetPath": target_path,
                    "bodySha256": body_sha256 if body_sha256 is not None else _EMPTY_BODY_SHA256,
                },
            )
        )
        return str(res["permit"])


class LocalSentinel:
    def __init__(self, sentinel: Any) -> None:
        self.s = sentinel

    def authorize(self, claim: Claim, intent_id: str, dest: Destination) -> str:
        return str(self.s.authorize(claim, intent_id, dest.scheme, dest.host, dest.port, dest.method))

    def authorize_reconcile(self, tenant_id: str, intent_id: str, dest: Destination) -> str:
        return str(self.s.authorize_reconcile(tenant_id, intent_id, dest.scheme, dest.host, dest.port))

    def authorize_human_takeover(
        self,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        dest: Destination,
        *,
        target_path: str = "/",
        body_sha256: str | None = None,
    ) -> str:
        return str(
            self.s.authorize_human_takeover(
                tenant_id,
                user_id,
                workspace_id,
                session_id,
                dest.scheme,
                dest.host,
                dest.port,
                dest.method,
                target_path,
                body_sha256 if body_sha256 is not None else _EMPTY_BODY_SHA256,
            )
        )
