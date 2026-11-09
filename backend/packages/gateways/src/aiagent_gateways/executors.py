"""Tool executors behind the Tool Gateway. An executor receives already-admitted, schema-valid arguments and a stable
effect key (= idempotency key towards the remote side). It must classify failures honestly:
  ExecutorFailed  - the effect certainly did not happen (safe to report FAILED)
  ExecutorUnknown - the request may have reached the remote side (ledger UNKNOWN; never auto-retried)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from aiagent_core.config import Secret


class ExecutorFailed(Exception):
    pass


class ExecutorUnknown(Exception):
    pass


@dataclass(frozen=True)
class Destination:
    scheme: str
    host: str
    port: int
    method: str


class Executor(Protocol):
    def destination(self) -> Destination | None: ...
    async def execute(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]: ...
    async def query(self, effect_key: str, permit: str | None = None) -> dict[str, Any] | None: ...


class MockSearchExecutor:
    """Read-only, deterministic, no network."""

    def destination(self) -> Destination | None:
        return None

    async def execute(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        return {"result": f"widget price is 42 (query={args['query'][:100]})"}

    async def query(self, effect_key: str, permit: str | None = None) -> dict[str, Any] | None:
        return None


class HttpMessageExecutor:
    """Sends a message to a receiver over HTTP *through the Trusted Egress proxy* (with a Sentinel permit). The
    receiver credential is injected here only when the destination equals its bound recipient host."""

    def __init__(self, url: str, *, proxy: str | None, credential: Secret | None = None,
                 credential_recipient: str | None = None, timeout_s: float = 10.0) -> None:
        self.url, self.proxy, self.timeout_s = url, proxy, timeout_s
        self.credential, self.credential_recipient = credential, credential_recipient
        p = urlsplit(url)
        self._dest = Destination(p.scheme, (p.hostname or "").lower(), p.port or (443 if p.scheme == "https" else 80),
                                 "POST")

    def destination(self) -> Destination | None:
        return self._dest

    def _client(self, permit: str | None) -> httpx.AsyncClient:
        proxy = None
        if self.proxy:
            proxy = httpx.Proxy(self.proxy, headers={"Proxy-Authorization": f"Aiagent tool_gateway:{permit or ''}"})
        return httpx.AsyncClient(proxy=proxy, timeout=self.timeout_s, trust_env=False)

    def _headers(self, effect_key: str) -> dict[str, str]:
        h = {"Idempotency-Key": effect_key}
        if self.credential is not None and self.credential_recipient == self._dest.host:
            h["Authorization"] = "Bearer " + self.credential.reveal()
        return h

    async def execute(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        try:
            async with self._client(permit) as client:
                r = await client.post(self.url, json={"to": args["to"], "body": args["body"]},
                                      headers=self._headers(effect_key))
        except httpx.ConnectError as e:
            raise ExecutorFailed("connect") from e  # nothing was sent: connection never established
        except httpx.HTTPError as e:
            raise ExecutorUnknown(type(e).__name__) from e  # request may have been delivered
        if r.status_code == 403 and r.headers.get("x-aiagent-egress") == "denied":
            raise ExecutorFailed("egress_denied")  # Sentinel refused before forwarding
        if r.status_code >= 500 or r.status_code == 408:
            raise ExecutorUnknown(f"http_{r.status_code}")
        if r.status_code >= 400:
            raise ExecutorFailed(f"http_{r.status_code}")
        receipt = r.json().get("receipt")
        if not isinstance(receipt, str) or not receipt:
            raise ExecutorUnknown("no_receipt")
        return {"receipt": receipt[:120]}

    async def query(self, effect_key: str, permit: str | None = None) -> dict[str, Any] | None:
        """Reconciliation: ask the receiver whether an effect with this idempotency key exists."""
        base = self.url.rsplit("/", 1)[0]
        async with self._client(permit) as client:
            r = await client.get(f"{base}/receipts/{effect_key}", headers=self._headers(effect_key))
        if r.status_code == 404:
            return {"sent": False}
        if r.status_code == 200:
            return {"sent": True, "receipt": str(r.json().get("receipt", ""))[:120]}
        return None


def from_env() -> dict[str, Executor]:
    from aiagent_core.config import secret

    out: dict[str, Executor] = {"mock_search": MockSearchExecutor()}
    url = os.environ.get("AIAGENT_MESSAGE_RECEIVER_URL")
    if url:
        out["http_message"] = HttpMessageExecutor(
            url, proxy=os.environ.get("AIAGENT_EGRESS_PROXY"),
            credential=secret("AIAGENT_MESSAGE_RECEIVER_TOKEN", required=False),
            credential_recipient=os.environ.get("AIAGENT_MESSAGE_RECEIVER_CREDENTIAL_HOST"),
        )
    return out
