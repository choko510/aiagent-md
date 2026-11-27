"""Bounded session acquisition. Every operation opens and closes its own SDK session.

Why not pool sessions? The MCP SDK's streamable HTTP client owns anyio cancel scopes/task groups that must
be entered and exited by the same task. Sharing a session across concurrent request tasks is therefore not
safe in SDK 2.3.0. A fresh session per operation keeps cancellation and UNKNOWN semantics honest; discovery
and call single-flight are enforced by their own locks instead.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field

from .config import AdapterLimits, ProviderConfig, require_enabled
from .credentials import CredentialBroker
from .errors import ErrorCode, McpError
from .sdk_transport import SdkSession, SdkStreamableHttpTransport
from .transport import Credential


@dataclass
class SessionManager:
    transport: SdkStreamableHttpTransport
    credentials: CredentialBroker
    limits: AdapterLimits
    _semaphore: asyncio.Semaphore = field(init=False)
    _call_semaphore: asyncio.Semaphore = field(init=False)
    _revoked: set[str] = field(default_factory=set)
    _active: int = 0

    def __post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(self.limits.max_sessions)
        self._call_semaphore = asyncio.Semaphore(self.limits.max_concurrent_calls)

    def revoke(self, provider_id: str) -> None:
        self._revoked.add(provider_id)

    def is_revoked(self, provider_id: str) -> bool:
        return provider_id in self._revoked

    @asynccontextmanager
    async def session(
        self,
        provider: ProviderConfig,
        *,
        on_list_changed: Callable[[], Awaitable[None] | None],
        run_id: str | None = None,
        permit: str | None = None,
        effect_tool: str | None = None,
        effect_arguments_digest: str | None = None,
        before_effect_send: Callable[[], Awaitable[None]] | None = None,
        before_open: Callable[[], Awaitable[None]] | None = None,
        expected_credential_version: int | None = None,
        deadline_epoch_s: float | None = None,
    ) -> AsyncIterator[SdkSession]:
        require_enabled()
        if not provider.enabled:
            raise McpError(ErrorCode.ADAPTER_DISABLED, "provider_disabled")
        if provider.provider_id in self._revoked:
            raise McpError(ErrorCode.POLICY_DENIED, "provider_revoked")
        credential: Credential | None = await self.credentials.resolve(provider, run_id=run_id)
        # Re-check after the broker await: a revoke/rotate may have happened while waiting.
        if provider.provider_id in self._revoked:
            raise McpError(ErrorCode.POLICY_DENIED, "provider_revoked")
        if not provider.enabled:
            raise McpError(ErrorCode.ADAPTER_DISABLED, "provider_disabled")
        if provider.auth_mode != "none" and credential is None:
            raise McpError(ErrorCode.CREDENTIAL_UNAVAILABLE, "credential_required")
        if (
            credential is not None
            and expected_credential_version is not None
            and credential.version != expected_credential_version
        ):
            raise McpError(ErrorCode.CREDENTIAL_STALE, "credential_version_mismatch")
        remaining = None
        if deadline_epoch_s is not None:
            remaining = deadline_epoch_s - time.time()
            if remaining <= 0:
                raise McpError(ErrorCode.BUDGET_EXCEEDED, "call_deadline_exceeded")
        try:
            async with AsyncExitStack() as stack:
                # The deadline covers the semaphore wait itself, not just the open after it.
                if remaining is not None:
                    await stack.enter_async_context(asyncio.timeout(remaining))
                await stack.enter_async_context(self._semaphore)
                # Re-check after the semaphore wait: a revoke or rotation may have happened while
                # this operation was queued. The callback also checks current registry/provider state.
                require_enabled()
                if provider.provider_id in self._revoked or not provider.enabled:
                    raise McpError(ErrorCode.POLICY_DENIED, "provider_revoked_or_disabled")
                if deadline_epoch_s is not None and time.time() >= deadline_epoch_s:
                    raise McpError(ErrorCode.BUDGET_EXCEEDED, "call_deadline_exceeded")
                if before_open is not None:
                    await before_open()
                self._active += 1
                try:
                    session = await stack.enter_async_context(
                        self.transport.open(
                            provider=provider,
                            credential=credential,
                            on_list_changed=on_list_changed,
                            limits=self.limits,
                            permit=permit,
                            effect_tool=effect_tool,
                            effect_arguments_digest=effect_arguments_digest,
                            before_effect_send=before_effect_send,
                        )
                    )
                    yield session
                finally:
                    self._active -= 1
        except TimeoutError as exc:
            # Once a session may be open, a deadline hit cannot prove whether an effect was sent.
            raise McpError(
                ErrorCode.BUDGET_EXCEEDED,
                "call_deadline_exceeded",
                effect_uncertain=True,
            ) from exc

    @asynccontextmanager
    async def call_slot(self, provider_id: str) -> AsyncIterator[None]:
        """Per-provider concurrency ceiling for admitted tool calls."""
        del provider_id  # one global ceiling today; the key is kept for a future per-provider split
        async with self._call_semaphore:
            yield

    @property
    def active(self) -> int:
        return self._active
