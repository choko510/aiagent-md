"""Executor behind the existing Tool Gateway.

The Gateway owns admission, approval, the attempt row and the side-effect ledger. This executor receives
only already-admitted arguments plus the Sentinel permit it issued for the exact destination. It re-checks
the immutable ToolRef, the pinned schema digest and the argument digest, performs one MCP call, and maps
every ambiguity to `ExecutorUnknown` so the Gateway records UNKNOWN and never auto-retries.

`destination_for_tool` cannot work with the current Gateway call order (destination is resolved before the
admitted identity is placed in `executor.context`). Use `destination_for_entry` from an integration layer
that resolves the tenant-scoped entry first; see docs/03-tools/mcp-adapter-implementation.md.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from aiagent_mcp_adapter.canonical import digest
from aiagent_mcp_adapter.config import AdapterLimits, ProviderConfig
from aiagent_mcp_adapter.contracts import (
    CallBinding,
    CallRequest,
    NormalizedResult,
    RegistrySnapshot,
    ToolEntry,
    ToolRef,
)
from aiagent_mcp_adapter.errors import McpError
from aiagent_mcp_adapter.normalization import normalize_result, result_fingerprint
from aiagent_mcp_adapter.registry_snapshot import SnapshotStore, ref_from_entry
from aiagent_mcp_adapter.session_manager import SessionManager
from aiagent_mcp_adapter.telemetry import TelemetrySink

from .executors import Destination, ExecutorFailed, ExecutorUnknown, valid_receipt


@dataclass(frozen=True)
class McpCallContext:
    effect_key: str
    tool_ref: ToolRef
    run_id: str
    intent_id: str
    arguments_digest: str


@dataclass
class McpToolExecutor:
    store: SnapshotStore
    sessions: SessionManager
    providers: dict[str, ProviderConfig]
    limits: AdapterLimits
    telemetry: TelemetrySink
    context: dict[str, McpCallContext] = field(default_factory=dict)

    def destination(self) -> Destination | None:
        return None

    def destination_for_tool(self, tool: str, args: dict[str, Any], **_: Any) -> Destination:
        raise ExecutorFailed("mcp_destination_requires_admitted_identity")

    def destination_for_entry(self, entry: ToolEntry) -> Destination:
        provider = self._provider(entry.provider_id)
        origin = provider.endpoint_origin
        return Destination(origin.scheme, origin.host, origin.port, "POST")

    async def prepare(
        self,
        *,
        effect_key: str,
        snapshot: RegistrySnapshot,
        entry: ToolEntry,
        run_id: str,
        intent_id: str,
        arguments_digest: str,
    ) -> McpCallContext:
        key = (snapshot.tenant_id, snapshot.owner_user_id, snapshot.provider_id)
        if (entry.tenant_id, entry.owner_user_id, entry.provider_id) != key:
            raise ExecutorFailed("mcp_scope_mismatch")
        await self.store.pin_run(snapshot, run_id=run_id)
        ctx = McpCallContext(
            effect_key=effect_key,
            tool_ref=ref_from_entry(entry),
            run_id=run_id,
            intent_id=intent_id,
            arguments_digest=arguments_digest,
        )
        self.context[effect_key] = ctx
        return ctx

    async def execute(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        ctx = self.context.pop(effect_key, None)
        if ctx is None:
            raise ExecutorFailed("mcp_binding_missing")
        if not permit:
            raise ExecutorFailed("mcp_permit_missing")
        try:
            entry = self.store.resolve_pinned(ctx.run_id, ctx.tool_ref)
            provider = self._provider(entry.provider_id)
        except McpError as exc:
            raise ExecutorFailed(str(exc.code)) from exc
        if provider.tenant_id != entry.tenant_id or provider.owner_user_id != entry.owner_user_id:
            raise ExecutorFailed("mcp_provider_scope_mismatch")
        if not isinstance(args, dict):
            raise ExecutorFailed("mcp_arguments_not_object")
        try:
            if digest(args) != ctx.arguments_digest:
                raise ExecutorFailed("mcp_arguments_digest_mismatch")
            binding = CallBinding(
                tool_ref=ctx.tool_ref,
                run_id=ctx.run_id,
                intent_id=ctx.intent_id,
                permit=permit,
                arguments_digest=digest(args),
                risk_class=entry.risk_class,
                deadline_epoch_s=time.time() + self.limits.call_timeout_s,
            )
            request = CallRequest(binding=binding, arguments=args)
            request.check_arguments_digest()
        except McpError as exc:
            raise ExecutorFailed(str(exc.code)) from exc

        async def on_list_changed() -> None:
            self.telemetry.emit("mcp.tools.list_changed", {"provider_id": provider.provider_id})

        try:
            async with self.sessions.call_slot(provider.provider_id):
                async with self.sessions.session(
                    provider, on_list_changed=on_list_changed, run_id=ctx.run_id, permit=permit
                ) as session:
                    raw = await session.call_tool(entry.source_name, args, timeout_s=self.limits.call_timeout_s)
            result = normalize_result(
                raw,
                entry=entry,
                limits=self.limits,
                output_schema=entry.output_schema,
            )
        except McpError as exc:
            self.telemetry.emit(
                "mcp.call.failed",
                {
                    "provider_id": provider.provider_id,
                    "alias": entry.alias,
                    "error_code": str(exc.code),
                    "retryable": exc.retryable,
                    "effect_uncertain": exc.effect_uncertain,
                },
            )
            if entry.risk_class != "read" and exc.effect_uncertain:
                raise ExecutorUnknown(str(exc.code)) from exc
            raise ExecutorFailed(str(exc.code)) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.telemetry.emit(
                "mcp.call.failed",
                {"provider_id": provider.provider_id, "alias": entry.alias, "error_code": "unclassified"},
            )
            raise ExecutorUnknown("mcp_unclassified_failure") from exc

        payload = self._payload(entry, result)
        self.telemetry.emit(
            "mcp.call.completed",
            {
                "provider_id": provider.provider_id,
                "alias": entry.alias,
                "status": result.status,
                "effect_uncertain": False,
            },
        )
        return payload

    async def query(self, effect_key: str, permit: str | None = None) -> dict[str, Any] | None:
        # MCP has no generic receipt lookup; guessing would be an unsafe retry. Operators reconcile via
        # server-specific evidence recorded outside this adapter.
        return None

    def _provider(self, provider_id: str) -> ProviderConfig:
        provider = self.providers.get(provider_id)
        if provider is None:
            raise ExecutorFailed("mcp_provider_unknown")
        return provider

    def _payload(self, entry: ToolEntry, result: NormalizedResult) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mcp": {
                "alias": entry.alias,
                "tool_id": entry.tool_id,
                "status": result.status,
                "text": result.text,
                "structured": result.structured,
                "blocks": [b.model_dump(mode="json", exclude_none=True) for b in result.blocks],
                "truncated": result.truncated,
                "notes": list(result.notes),
            },
            "result_digest": result_fingerprint(result),
        }
        receipt = _server_receipt(result)
        if receipt is not None:
            payload["receipt"] = receipt
        return payload


def _server_receipt(result: NormalizedResult) -> str | None:
    if not isinstance(result.structured, dict):
        return None
    candidate = result.structured.get("receipt")
    return candidate if isinstance(candidate, str) and valid_receipt(candidate) else None
