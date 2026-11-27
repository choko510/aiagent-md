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
from aiagent_mcp_adapter.errors import ErrorCode, McpError
from aiagent_mcp_adapter.normalization import normalize_result, result_fingerprint
from aiagent_mcp_adapter.registry_snapshot import SnapshotKey, SnapshotStore, ref_from_entry
from aiagent_mcp_adapter.schema_validation import clone_arguments, compile_schema, validate_arguments
from aiagent_mcp_adapter.session_manager import SessionManager
from aiagent_mcp_adapter.telemetry import TelemetrySink

from .executors import Destination, ExecutorFailed, ExecutorUnknown


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
        try:
            pinned = snapshot.entry_by_tool_id(entry.tool_id)
        except McpError as exc:
            raise ExecutorFailed(f"{exc.code}:{exc.detail}") from exc
        if pinned != entry:
            raise ExecutorFailed("mcp_entry_not_in_snapshot")
        try:
            provider = self._provider(entry.provider_id)
        except McpError as exc:
            raise ExecutorFailed(f"{exc.code}:{exc.detail}") from exc
        if entry.source_name not in provider.allowed_tools:
            raise ExecutorFailed("mcp_tool_not_allowlisted")
        if provider.tenant_id != entry.tenant_id or provider.owner_user_id != entry.owner_user_id:
            raise ExecutorFailed("mcp_provider_scope_mismatch")
        if provider.policy_revision != entry.policy_revision:
            raise ExecutorFailed("mcp_policy_revision_changed")
        if provider.credential_version != entry.credential_version:
            raise ExecutorFailed("mcp_credential_version_changed")
        if snapshot.endpoint_fingerprint != provider.endpoint_fingerprint:
            raise ExecutorFailed("mcp_endpoint_fingerprint_changed")
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
            self._validate_current(provider, entry, ctx)
            # Copy before any await: the caller's dict is untrusted mutable input, and the digest
            # must cover exactly the values that reach the wire.
            copied = clone_arguments(args, max_bytes=self.limits.max_request_bytes)
            if digest(copied) != ctx.arguments_digest:
                raise McpError(ErrorCode.BINDING_MISMATCH, "arguments_digest_mismatch")
            compiled = compile_schema(entry.input_schema, self.limits, where="input")
            if compiled.digest != entry.input_digest:
                raise McpError(ErrorCode.SCHEMA_INVALID, "pinned_input_digest_mismatch")
            validate_arguments(compiled, copied)
            deadline_epoch_s = time.time() + self.limits.call_timeout_s
            binding = CallBinding(
                tool_ref=ctx.tool_ref,
                run_id=ctx.run_id,
                intent_id=ctx.intent_id,
                permit=permit,
                arguments_digest=digest(copied),
                risk_class=entry.risk_class,
                deadline_epoch_s=deadline_epoch_s,
            )
            request = CallRequest(binding=binding, arguments=copied)
            request.check_arguments_digest()
        except McpError as exc:
            raise ExecutorFailed(f"{exc.code}:{exc.detail}") from exc

        async def before_effect_send() -> None:
            # Re-validate the exact values at the send boundary, then fence revocation/TTL/revisions.
            # This is the last adapter-controlled check before the single effect POST.
            validate_arguments(compiled, copied)
            await self._before_effect_send(ctx, deadline_epoch_s)

        async def on_list_changed() -> None:
            # A notification invalidates every pin for this provider. The run must refresh its
            # snapshot; continuing on an old ref is strictly forbidden.
            await self.store.invalidate_pins(
                SnapshotKey(provider.tenant_id, provider.owner_user_id, provider.provider_id)
            )
            self.telemetry.emit("mcp.tools.list_changed", {"provider_id": provider.provider_id})

        try:
            async with self.sessions.call_slot(provider.provider_id):
                async with self.sessions.session(
                    provider,
                    on_list_changed=on_list_changed,
                    run_id=ctx.run_id,
                    permit=permit,
                    effect_tool=entry.source_name,
                    effect_arguments_digest=ctx.arguments_digest,
                    before_effect_send=before_effect_send,
                    before_open=lambda: self._before_effect_send(ctx, deadline_epoch_s),
                    expected_credential_version=entry.credential_version,
                    deadline_epoch_s=deadline_epoch_s,
                ) as session:
                    remaining = max(0.001, deadline_epoch_s - time.time())
                    raw = await session.call_tool(entry.source_name, copied, timeout_s=remaining)
            result = normalize_result(
                raw,
                entry=entry,
                limits=self.limits,
                output_schema=entry.output_schema,
            )
            settlement = self._settlement_error(entry, result)
            if settlement is not None:
                raise settlement
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
            if entry.risk_class != "read" and (exc.effect_uncertain or exc.code in _CALL_UNCERTAIN_CODES):
                raise ExecutorUnknown(f"{exc.code}:{exc.detail}") from exc
            raise ExecutorFailed(f"{exc.code}:{exc.detail}") from exc
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

    async def _before_effect_send(self, ctx: McpCallContext, deadline_epoch_s: float) -> None:
        """Fence the exact tools/call send against revocation, TTL, policy and endpoint changes."""
        if time.time() >= deadline_epoch_s:
            raise McpError(ErrorCode.BUDGET_EXCEEDED, "call_deadline_exceeded")
        entry = self.store.resolve_pinned(ctx.run_id, ctx.tool_ref)
        provider = self._provider(entry.provider_id)
        if self.sessions.is_revoked(provider.provider_id):
            raise McpError(ErrorCode.POLICY_DENIED, "provider_revoked")
        self._validate_current(provider, entry, ctx)

    def _validate_current(self, provider: ProviderConfig, entry: ToolEntry, ctx: McpCallContext) -> None:
        if provider.tenant_id != entry.tenant_id or provider.owner_user_id != entry.owner_user_id:
            raise McpError(ErrorCode.BINDING_MISMATCH, "provider_scope_mismatch")
        if not provider.enabled:
            raise McpError(ErrorCode.ADAPTER_DISABLED, "provider_disabled")
        if entry.source_name not in provider.allowed_tools:
            raise McpError(ErrorCode.POLICY_DENIED, "tool_not_allowlisted")
        if provider.policy_revision != entry.policy_revision:
            raise McpError(ErrorCode.POLICY_DENIED, "policy_revision_changed")
        if provider.credential_version != entry.credential_version:
            raise McpError(ErrorCode.CREDENTIAL_STALE, "credential_version_changed")
        pinned_endpoint = self.store.pinned_endpoint_fingerprint(ctx.run_id, ctx.tool_ref)
        if pinned_endpoint != provider.endpoint_fingerprint:
            raise McpError(ErrorCode.BINDING_MISMATCH, "endpoint_fingerprint_changed")

    def _settlement_error(self, entry: ToolEntry, result: NormalizedResult) -> McpError | None:
        if result.status == "OK" and not result.truncated:
            if entry.risk_class == "read":
                return None
            # A write is only CONFIRMED by a non-empty text body or a non-empty structured result.
            # A resource link, binary digest or empty text/object is not proof the effect happened.
            if result.text.strip() or (result.structured is not None and bool(result.structured)):
                return None
            return McpError(ErrorCode.EFFECT_UNCERTAIN, "empty_write_result", effect_uncertain=True)
        if entry.risk_class == "read":
            return McpError(
                ErrorCode.TOOL_ERROR if result.status == "TOOL_ERROR" else ErrorCode.RESULT_UNSUPPORTED,
                f"read_result_{result.status.lower()}",
            )
        return McpError(
            ErrorCode.EFFECT_UNCERTAIN,
            f"write_result_{result.status.lower()}",
            effect_uncertain=True,
        )

    def _payload(self, entry: ToolEntry, result: NormalizedResult) -> dict[str, Any]:
        return {
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


_CALL_UNCERTAIN_CODES = frozenset({ErrorCode.RESULT_TRUNCATED, ErrorCode.OUTPUT_SCHEMA_MISMATCH})
