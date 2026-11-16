"""Tool executors behind the Tool Gateway. An executor receives already-admitted, schema-valid arguments and a stable
effect key (= idempotency key towards the remote side). It must classify failures honestly:
  ExecutorFailed  - the effect certainly did not happen (safe to report FAILED)
  ExecutorUnknown - the request may have reached the remote side (ledger UNKNOWN; never auto-retried)
"""

from __future__ import annotations

import hmac
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from aiagent_core.config import Secret


class ExecutorFailed(Exception):
    pass


class ExecutorUnknown(Exception):
    pass


def is_authenticated_proxy_denial(status_code: int, headers: Mapping[str, str], permit: str | None) -> bool:
    """Verify HMAC proof on 403 x-aiagent-egress: denied so spoofed upstream 403s never look like pre-send denials."""
    if status_code != 403 or not permit:
        return False
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    if lowered.get("x-aiagent-egress") != "denied":
        return False
    proof = lowered.get("x-aiagent-egress-proof", "")
    code_part, sep, mac_part = proof.partition(":")
    if not sep or not code_part or not mac_part:
        return False
    key = sha256(permit.encode("utf-8")).digest()
    expected = hmac.new(key, f"DENIED:{code_part}".encode(), sha256).hexdigest()
    return hmac.compare_digest(mac_part, expected)


def valid_receipt(value: Any) -> bool:
    """Reject empty, truncated or malformed evidence before confirming an irreversible effect."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 120
        and value == value.strip()
        and all(ord(ch) >= 32 and ord(ch) != 127 for ch in value)
    )


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
    """Sends a message to a receiver over HTTP/HTTPS *through the Trusted Egress proxy* (single forward-proxy
    requests with a Sentinel permit, never CONNECT tunnels). The receiver credential is injected here only when
    the destination equals its bound recipient host."""

    def __init__(
        self,
        url: str,
        *,
        proxy: str | None,
        credential: Secret | None = None,
        credential_recipient: str | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.url, self.proxy, self.timeout_s = url, proxy, timeout_s
        self.credential, self.credential_recipient = credential, credential_recipient
        p = urlsplit(url)
        self._dest = Destination(
            p.scheme, (p.hostname or "").lower(), p.port or (443 if p.scheme == "https" else 80), "POST"
        )

    def destination(self) -> Destination | None:
        return self._dest

    def _client(self, permit: str | None) -> httpx.AsyncClient:
        del permit
        return httpx.AsyncClient(proxy=None, timeout=self.timeout_s, trust_env=False)

    def _headers(self, effect_key: str) -> dict[str, str]:
        h = {"Idempotency-Key": effect_key}
        if self.credential is not None and self.credential_recipient == self._dest.host:
            h["Authorization"] = "Bearer " + self.credential.reveal()
        return h

    async def execute(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        from aiagent_core import config

        from .web_fetch import _send_via_forward_proxy, parse_safe_http_url

        if self.proxy is None and config.profile() == "production":
            raise ExecutorFailed("egress_proxy_required")

        payload = json.dumps(
            {"to": args["to"], "body": args["body"]}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        headers = {**self._headers(effect_key), "Content-Type": "application/json"}

        if self.proxy is not None:
            parsed = parse_safe_http_url(self.url)
            status_code, _, raw_body, _ = await _send_via_forward_proxy(
                self.proxy,
                method="POST",
                parsed=parsed,
                permit=permit or "",
                extra_headers=headers,
                request_body=payload,
                max_bytes=65_536,
                timeout_s=self.timeout_s,
            )
        else:
            try:
                async with self._client(permit) as client:
                    r = await client.post(self.url, content=payload, headers=headers)
                status_code = r.status_code
                raw_body = r.content[:65_536]
            except httpx.ConnectError as e:
                raise ExecutorFailed("connect") from e  # nothing was sent: connection never established
            except httpx.HTTPError as e:
                raise ExecutorUnknown(type(e).__name__) from e  # request may have been delivered

        # Any response >= 400 after sending an irreversible POST without a verified pre-send Sentinel
        # denial leaves external side-effect state uncertain -> UNKNOWN (never FAILED / auto-retried).
        if status_code >= 400:
            raise ExecutorUnknown(f"http_{status_code}")
        try:
            body_obj = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise ExecutorUnknown("invalid_receipt") from e
        receipt = body_obj.get("receipt") if isinstance(body_obj, dict) else None
        if not isinstance(receipt, str) or not receipt:
            raise ExecutorUnknown("no_receipt")
        return {"receipt": receipt[:120]}

    async def query(self, effect_key: str, permit: str | None = None) -> dict[str, Any] | None:
        """Reconciliation: ask the receiver whether an effect with this idempotency key exists."""
        from .web_fetch import _send_via_forward_proxy, parse_safe_http_url

        base = self.url.rsplit("/", 1)[0]
        query_url = f"{base}/receipts/{effect_key}"
        if self.proxy is not None:
            parsed = parse_safe_http_url(query_url)
            status_code, _, raw_body, _ = await _send_via_forward_proxy(
                self.proxy,
                method="GET",
                parsed=parsed,
                permit=permit or "",
                extra_headers=self._headers(effect_key),
                request_body=None,
                max_bytes=65_536,
                timeout_s=self.timeout_s,
            )
            body_text = raw_body.decode("utf-8", "replace")
        else:
            async with self._client(permit) as client:
                r = await client.get(query_url, headers=self._headers(effect_key))
            status_code = r.status_code
            body_text = r.text
        # 404 is not proof of non-delivery: the receiver may have restarted,
        # forgotten its in-memory idempotency store, or expired its receipts.
        # No durable, strongly consistent negative-proof protocol is configured.
        if status_code != 200:
            return None
        try:
            data = json.loads(body_text)
        except ValueError:
            return None
        if not isinstance(data, dict) or not valid_receipt(data.get("receipt")):
            return None
        return {"sent": True, "receipt": data["receipt"]}


class SandboxCellExecutor:
    """`sandbox_exec` -> Execution Supervisor in the Untrusted Execution VM. The gateway signs a single-use grant
    bound to the admitted argv/cwd/run/lease/intent; the supervisor (no DB, no secrets, no network) verifies it. A lost
    connection after the request was delivered is UNKNOWN (the command may have run)."""

    def __init__(
        self,
        endpoint: str,
        rpc_key: bytes,
        grant_key: bytes,
        *,
        wall_ms: int = 60_000,
        memory_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        self.endpoint, self.rpc_key, self.grant_key = endpoint, rpc_key, grant_key
        self.wall_ms, self.memory_bytes = wall_ms, memory_bytes
        self.context: dict[str, Any] = {}

    def destination(self) -> Destination | None:
        return None  # no Internet egress: the cell is offline; only this scoped RPC exists

    async def execute(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        import time
        import uuid

        from aiagent_core.rpc import RpcClient
        from aiagent_domain.rpcwire import RpcError
        from aiagent_sandbox.grant import Grant, sign

        ctx = self.context.get(effect_key)
        if ctx is None:
            raise ExecutorFailed("no admission context")
        g = Grant(
            uuid.uuid4().hex,
            ctx["tenant_id"],
            ctx["task_id"],
            ctx["run_id"],
            ctx["intent_id"],
            ctx["lease_epoch"],
            tuple(args["argv"]),
            args["cwdRelative"],
            self.wall_ms,
            self.memory_bytes,
            int(time.time()) + 120,
        )
        client = RpcClient(self.endpoint, "tool_gateway", self.rpc_key, timeout_s=self.wall_ms / 1000 + 30)
        try:
            res = await client.call(
                "exec.run", {"grant": sign(self.grant_key, g), "argv": args["argv"], "cwdRelative": args["cwdRelative"]}
            )
        except RpcError as e:
            raise ExecutorFailed(e.code) from e  # refused before execution (grant/auth)
        except ConnectionRefusedError as e:
            raise ExecutorFailed("cell unavailable") from e
        except (OSError, EOFError, TimeoutError) as e:
            raise ExecutorUnknown(type(e).__name__) from e
        if res.get("status") == "REFUSED":
            raise ExecutorFailed(str(res.get("reason")))
        return {
            "exitCode": res["exitCode"],
            "timedOut": res["timedOut"],
            "stdout": res["stdout"][:8000],
            "stderr": res["stderr"][:4000],
        }

    async def query(self, effect_key: str, permit: str | None = None) -> dict[str, Any] | None:
        return None


def from_env(db: Any = None) -> dict[str, Executor]:
    from aiagent_core import config
    from aiagent_core.config import secret

    from .browser_executor import browser_from_env
    from .web_fetch import HttpFetchExecutor, SearchRouterExecutor

    proxy_url = os.environ.get("AIAGENT_EGRESS_PROXY")
    allow_priv: frozenset[str] = frozenset()
    if config.profile() != "production" and os.environ.get("AIAGENT_WEB_FETCH_ALLOW_PRIVATE"):
        allow_priv = frozenset(x.strip() for x in os.environ["AIAGENT_WEB_FETCH_ALLOW_PRIVATE"].split(",") if x.strip())

    search_url = os.environ.get("AIAGENT_SEARCH_URL")
    if search_url:
        search_exec: Executor = SearchRouterExecutor(
            search_url,
            backend_kind=os.environ.get("AIAGENT_SEARCH_BACKEND", "searxng"),
            api_key=secret("AIAGENT_SEARCH_API_KEY", required=False),
            proxy=proxy_url,
            allow_private_addrs=allow_priv,
        )
    else:
        search_exec = MockSearchExecutor()

    out: dict[str, Executor] = {
        "mock_search": search_exec,
        "http_fetch": HttpFetchExecutor(proxy=proxy_url, allow_private_addrs=allow_priv),
    }
    bf = browser_from_env(db)
    if bf is not None:
        out["browser_fixture"] = bf
    url = os.environ.get("AIAGENT_MESSAGE_RECEIVER_URL")
    if url:
        out["http_message"] = HttpMessageExecutor(
            url,
            proxy=proxy_url,
            credential=secret("AIAGENT_MESSAGE_RECEIVER_TOKEN", required=False),
            credential_recipient=os.environ.get("AIAGENT_MESSAGE_RECEIVER_CREDENTIAL_HOST"),
        )
    cell = os.environ.get("AIAGENT_SANDBOX_ENDPOINT")
    if cell:
        rpc = secret("AIAGENT_SANDBOX_RPC_KEY")
        grant = secret("AIAGENT_SANDBOX_GRANT_KEY")
        assert rpc is not None and grant is not None
        out["sandbox_cell"] = SandboxCellExecutor(cell, bytes.fromhex(rpc.reveal()), bytes.fromhex(grant.reveal()))
    return out
