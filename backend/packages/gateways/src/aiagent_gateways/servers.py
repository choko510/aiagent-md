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

from aiagent_core import config, memory_tombstones
from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_core.logging import register_secret, setup
from aiagent_core.migrate import check_compatible
from aiagent_core.rpc import Endpoint, RpcClient, RpcServer, parse_keys
from aiagent_core.service import Lifecycle
from aiagent_domain.rpcwire import RpcError

from .context_ingest import MemoryIngest, ingest_handler
from .context_keys import KeyRing
from .executors import from_env as executors_from_env
from .model_gateway import ModelGateway, SealedModelRequest
from .providers import AnthropicProvider, FakeModelProvider, GeminiProvider, OpenAIProvider
from .router import Route
from .sentinel_port import RpcSentinel
from .tool_gateway import ToolGateway


def build_model_server(
    db: Database,
    keys: dict[str, bytes],
    endpoint: str,
    gw: ModelGateway,
    *,
    ingest: MemoryIngest | None = None,
) -> RpcServer:
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

    async def context_eligible(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            claim = Claim.from_wire(body["claim"])
        except (KeyError, ValueError, TypeError):
            raise RpcError("BAD_REQUEST") from None
        return await asyncio.to_thread(gw.context_eligible, claim)

    srv.register("model.context_eligible", context_eligible, {"runtime"})

    async def context_compact(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            claim = Claim.from_wire(body["claim"])
            auto_if_needed = bool(body.get("autoIfNeeded", False))
            threshold_chars = int(body.get("thresholdChars", 4096))
        except (KeyError, ValueError, TypeError):
            raise RpcError("BAD_REQUEST") from None
        return await asyncio.to_thread(
            gw.compact_context, claim, auto_if_needed=auto_if_needed, threshold_chars=threshold_chars
        )

    srv.register("model.context_compact", context_compact, {"runtime"})

    async def complete_context(caller: str, body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        try:
            claim = Claim.from_wire(body["claim"])
            value = body["contextHead"]
            if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or str(int(value)) != value:
                raise ValueError("invalid head")
            head = int(value)
            if not 1 <= head <= 9223372036854775807:
                raise ValueError("invalid head")
        except (KeyError, TypeError, ValueError):
            raise RpcError("BAD_REQUEST") from None
        yield {**(await gw.complete_context(claim, head)), "__final__": True}

    srv.register("model.context", complete_context, {"runtime"}, streaming=True)

    if ingest is not None:
        srv.register("memory.artifact.put", ingest_handler(ingest), {"api"}, streaming=False)
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
        if (
            not isinstance(args, dict)
            or not isinstance(body.get("toolCallId"), str)
            or not isinstance(body.get("tool"), str)
        ):
            raise RpcError("BAD_REQUEST")
        return await gw.submit(claim_of(body), body["toolCallId"], body["tool"], args)

    async def submit_development_git(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        args, tool_call_id = body.get("args"), body.get("toolCallId")
        if not isinstance(args, dict) or not isinstance(tool_call_id, str):
            raise RpcError("BAD_REQUEST")
        return await gw.submit_development_git(claim_of(body), tool_call_id, args)

    async def record_internal(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(
            gw.record_internal, claim_of(body), str(body["toolCallId"]), str(body["tool"]), str(body["content"])
        )

    async def reconcile(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        owner_id = str(body["ownerUserId"]) if caller == "api" else None
        if caller == "api" and not owner_id:
            raise RpcError("BAD_REQUEST")
        return await gw.reconcile(str(body["tenantId"]), str(body["intentId"]), owner_user_id=owner_id)

    async def dev_pr_dispatch(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        del caller
        args = body.get("args")
        if not isinstance(args, dict):
            raise RpcError("BAD_REQUEST")
        return await gw.dispatch_approved_dev_pr(
            tenant_id=str(body["tenantId"]),
            owner_user_id=str(body["ownerUserId"]),
            workspace_id=str(body["workspaceId"]),
            intent_id=str(body["intentId"]),
            expected_args=args,
        )

    srv.register("tool.submit", submit, {"runtime"})
    if gw._development_git_enabled:
        key = keys.get("development")
        if key is None or len(key) < 32 or any(value == key for name, value in keys.items() if name != "development"):
            raise ValueError("development Git RPC requires a separate caller key")
        srv.register("tool.development_git_commit", submit_development_git, {"development"})
    srv.register("tool.record_internal", record_internal, {"runtime"})
    srv.register("tool.reconcile", reconcile, {"ops", "api"})
    srv.register("tool.dev_pr_dispatch", dev_pr_dispatch, {"api"})
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


def load_key_ring() -> KeyRing:
    """Deployment key ring from a mounted file; the single test key is a compatibility input only."""
    path = os.environ.get("AIAGENT_CONTEXT_ARTIFACT_KEYS_FILE")
    if path:
        try:
            ring = KeyRing.from_file(path)
        except (OSError, ValueError) as exc:
            raise config.ConfigError("invalid Context artifact key ring") from exc
        return ring
    if config.profile() == "production":
        raise config.ConfigError("AIAGENT_CONTEXT_ARTIFACT_KEYS_FILE is required in production")
    single = config.secret("AIAGENT_CONTEXT_ARTIFACT_KEY", required=False)
    if single is None:
        raise config.ConfigError("no Context artifact key ring configured")
    try:
        key = bytes.fromhex(single.reveal())
    except ValueError as exc:
        raise config.ConfigError("invalid Context artifact key") from exc
    try:
        return KeyRing.single(key)
    except ValueError as exc:
        raise config.ConfigError("invalid Context artifact key") from exc


def load_browser_key_ring() -> KeyRing:
    """Dedicated AES-GCM key ring for browser storageState shared strictly between API and Tool Gateway."""
    path = os.environ.get("AIAGENT_BROWSER_STORAGE_KEYS_FILE")
    if path:
        try:
            return KeyRing.from_file(path)
        except (OSError, ValueError) as exc:
            raise config.ConfigError("invalid browser storage key ring") from exc
    raw_json = config.secret("AIAGENT_BROWSER_STORAGE_KEYS", required=False)
    if raw_json is not None:
        try:
            return KeyRing.from_json(raw_json.reveal())
        except ValueError as exc:
            raise config.ConfigError("invalid browser storage key ring") from exc
    if config.profile() == "production":
        raise config.ConfigError("AIAGENT_BROWSER_STORAGE_KEYS_FILE is required in production")
    return load_key_ring()


def load_model_config_from_env() -> tuple[dict[str, Any], dict[str, list[Route]], list[str]]:
    import json

    providers: dict[str, Any] = {"fake": FakeModelProvider()}
    routes: dict[str, list[Route]] = {"fake-gateway": [Route("fake", "fake-1")]}
    proxy_url = os.environ.get("AIAGENT_EGRESS_PROXY_MODEL")
    proxy_token = config.secret("AIAGENT_EGRESS_MODEL_TOKEN", required=False)
    secrets: list[str] = []

    def require_model_proxy(label: str) -> None:
        if config.profile() == "production" and not proxy_url:
            raise config.ConfigError(f"AIAGENT_EGRESS_PROXY_MODEL is required in production ({label})")

    anth = config.secret("AIAGENT_ANTHROPIC_API_KEY", required=False)
    if anth is not None:
        register_secret(anth.reveal())
        secrets.append(anth.reveal())
        require_model_proxy("anthropic")
        providers["anthropic"] = AnthropicProvider(
            anth,
            proxy_url,
            proxy_token=proxy_token,
            url=os.environ.get("AIAGENT_ANTHROPIC_URL"),  # provider-compatible local test server only
            timeout_s=float(os.environ.get("AIAGENT_ANTHROPIC_TIMEOUT_S", "120")),
        )
        routes["claude-sonnet-5-5"] = [Route("anthropic", "claude-sonnet-5-5"), Route("anthropic", "claude-haiku-5-5")]

    openai_key = config.secret("AIAGENT_OPENAI_API_KEY", required=False)
    if openai_key is not None:
        register_secret(openai_key.reveal())
        secrets.append(openai_key.reveal())
        require_model_proxy("openai")
        providers["openai"] = OpenAIProvider(
            openai_key,
            proxy_url,
            proxy_token=proxy_token,
            url=os.environ.get("AIAGENT_OPENAI_URL"),
            timeout_s=float(os.environ.get("AIAGENT_OPENAI_TIMEOUT_S", "120")),
        )
        for m in [
            x.strip() for x in os.environ.get("AIAGENT_OPENAI_MODELS", "gpt-4o,gpt-4o-mini").split(",") if x.strip()
        ]:
            routes[m] = [Route("openai", m)]

    compat_url = os.environ.get("AIAGENT_OPENAI_COMPAT_URL")
    if compat_url:
        from urllib.parse import urlsplit

        compat_key = config.secret("AIAGENT_OPENAI_COMPAT_API_KEY", required=False)
        if compat_key is not None:
            register_secret(compat_key.reveal())
            secrets.append(compat_key.reveal())
        compat_external = os.environ.get("AIAGENT_OPENAI_COMPAT_EXTERNAL", "1") != "0"
        if not compat_external:
            compat_host = (urlsplit(compat_url).hostname or "").lower()
            if compat_host not in ("127.0.0.1", "::1", "localhost"):
                raise config.ConfigError("AIAGENT_OPENAI_COMPAT_EXTERNAL=0 is restricted to local loopback endpoints")
        else:
            require_model_proxy("openai_compat")
        providers["openai_compat"] = OpenAIProvider(
            compat_key,
            proxy_url if compat_external else None,
            proxy_token=proxy_token if compat_external else None,
            url=compat_url,
            provider_name="openai_compat",
            external_recipient=compat_external,
            timeout_s=float(os.environ.get("AIAGENT_OPENAI_COMPAT_TIMEOUT_S", "120")),
        )
        for m in [
            x.strip() for x in os.environ.get("AIAGENT_OPENAI_COMPAT_MODELS", "local-llm").split(",") if x.strip()
        ]:
            routes[m] = [Route("openai_compat", m)]

    gemini_key = config.secret("AIAGENT_GEMINI_API_KEY", required=False)
    if gemini_key is not None:
        register_secret(gemini_key.reveal())
        secrets.append(gemini_key.reveal())
        require_model_proxy("gemini")
        providers["gemini"] = GeminiProvider(
            gemini_key,
            proxy_url,
            proxy_token=proxy_token,
            url_template=os.environ.get("AIAGENT_GEMINI_URL_TEMPLATE"),
            timeout_s=float(os.environ.get("AIAGENT_GEMINI_TIMEOUT_S", "120")),
        )
        for m in [
            x.strip()
            for x in os.environ.get("AIAGENT_GEMINI_MODELS", "gemini-2.5-flash,gemini-2.5-pro").split(",")
            if x.strip()
        ]:
            routes[m] = [Route("gemini", m)]

    custom_routes_raw = os.environ.get("AIAGENT_MODEL_ROUTES_JSON")
    if custom_routes_raw:
        try:
            parsed_routes = json.loads(custom_routes_raw)
            if not isinstance(parsed_routes, dict):
                raise ValueError("AIAGENT_MODEL_ROUTES_JSON must be a JSON object")
            for model_key, items in parsed_routes.items():
                if not isinstance(items, list) or not items:
                    raise ValueError(f"invalid route list for {model_key}")
                routes[str(model_key)] = [
                    Route(str(it["provider"]), str(it["model"]), int(it.get("weight", 1))) for it in items
                ]
        except (ValueError, KeyError, TypeError) as exc:
            raise config.ConfigError("invalid AIAGENT_MODEL_ROUTES_JSON") from exc
    return providers, routes, secrets


def main_model() -> None:
    setup("model_gateway")
    config.require_isolation("model_gateway")
    keys = parse_keys(config.secret("AIAGENT_MODEL_GATEWAY_RPC_KEYS").reveal())  # type: ignore[union-attr]
    dsn = config.secret("AIAGENT_MODEL_GATEWAY_DSN").reveal()  # type: ignore[union-attr]
    import json

    providers, routes, secrets = load_model_config_from_env()

    if config.profile() == "production" and providers.keys() == {"fake"}:
        print(
            "model_gateway: no real provider configured (FakeModel only) - refusing to start in production",
            file=sys.stderr,
        )
        raise SystemExit(2)
    # Static recipient consent is a deployment fact (data-processing agreement); the
    # per-user grant is separate and is re-checked in the database on every admission.
    consent = {
        (a, b) for x in os.environ.get("AIAGENT_MODEL_CONSENT", "").split(",") if ":" in x for a, b in [x.split(":", 1)]
    }
    db = Database(dsn, max_size=8, name="model_gateway")
    ring = load_key_ring()
    for key in ring.keys.values():
        register_secret(key.hex())
    context_key = ring
    # The external tombstone log is a Memory egress authority, not only a restore
    # input. Without it the gateway still starts (ordinary model calls keep
    # working) but every Context/Memory send is denied.
    tombstone_log = None
    try:
        tombstone_log = memory_tombstones.log_from_env()
    except (memory_tombstones.TombstoneError, config.ConfigError) as exc:
        if config.profile() == "production":
            print(f"model_gateway: Memory egress disabled: {type(exc).__name__}", file=sys.stderr)
    route_rates: dict[str, Any] = {}
    raw_rates = os.environ.get("AIAGENT_MODEL_RATES_JSON")
    if raw_rates:
        from .router import RouteRates

        try:
            parsed_rates = json.loads(raw_rates)
            if not isinstance(parsed_rates, dict):
                raise ValueError("AIAGENT_MODEL_RATES_JSON must be a JSON object")
            for mk, rspec in parsed_rates.items():
                if isinstance(rspec, dict):
                    route_rates[str(mk)] = RouteRates(
                        input_micro_usd_per_1m=int(rspec["input_micro_usd_per_1m"]),
                        output_micro_usd_per_1m=int(rspec["output_micro_usd_per_1m"]),
                    )
        except Exception as exc:
            raise config.ConfigError(f"invalid AIAGENT_MODEL_RATES_JSON: {exc}") from exc
    gw = ModelGateway(  # type: ignore[arg-type]
        db,
        providers,
        routes,
        consent=consent,
        secrets=secrets,
        context_key=context_key,
        tombstone_log=tombstone_log,
        nli_model=os.environ.get("AIAGENT_NLI_MODEL"),
        route_rates=route_rates,
    )
    gw.recover_orphans(float(config.env_int("AIAGENT_ATTEMPT_ORPHAN_S", 600)))
    srv = build_model_server(
        db,
        keys,
        config.env("AIAGENT_MODEL_GATEWAY_ENDPOINT"),
        gw,
        ingest=MemoryIngest(db, ring, max_bytes=config.env_int("AIAGENT_MEMORY_SOURCE_MAX_BYTES", 2_000_000)),
    )
    asyncio.run(_serve("model_gateway", srv, db, config.env_int("AIAGENT_HEALTH_PORT", 0) or None))


def main_tool() -> None:
    setup("tool_gateway")
    config.require_isolation("tool_gateway")
    keys = parse_keys(config.secret("AIAGENT_TOOL_GATEWAY_RPC_KEYS").reveal())  # type: ignore[union-attr]
    dsn = config.secret("AIAGENT_TOOL_GATEWAY_DSN").reveal()  # type: ignore[union-attr]
    sentinel = None
    if os.environ.get("AIAGENT_SENTINEL_ENDPOINT"):
        skey = bytes.fromhex(config.secret("AIAGENT_SENTINEL_CLIENT_KEY").reveal())  # type: ignore[union-attr]
        sentinel = RpcSentinel(RpcClient(os.environ["AIAGENT_SENTINEL_ENDPOINT"], "tool_gateway", skey))
    browser_ring_ok = False
    if config.profile() == "production":
        try:
            ring = load_browser_key_ring()
            for key in ring.keys.values():
                register_secret(key.hex())
            browser_ring_ok = True
        except config.ConfigError:
            browser_ring_ok = False
    config.require_production_safety(
        "tool_gateway",
        {
            "Sentinel endpoint configured": sentinel is not None,
            "egress proxy configured": bool(os.environ.get("AIAGENT_EGRESS_PROXY")),
            "browser storage key ring configured": browser_ring_ok,
        },
    )
    db = Database(dsn, max_size=8, name="tool_gateway")
    gw = ToolGateway(db, executors_from_env(db=db), sentinel=sentinel)
    srv = build_tool_server(keys, config.env("AIAGENT_TOOL_GATEWAY_ENDPOINT"), gw)
    asyncio.run(_serve("tool_gateway", srv, db, config.env_int("AIAGENT_HEALTH_PORT", 0) or None))


if __name__ == "__main__":
    {"model": main_model, "tool": main_tool}[sys.argv[1]]()
