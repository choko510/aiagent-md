"""Bounded session acquisition. Every operation opens and closes its own SDK session.

Why not pool sessions? The MCP SDK's streamable HTTP client owns anyio cancel scopes/task groups that must
be entered and exited by the same task. Sharing a session across concurrent request tasks is therefore not
safe in SDK 2.3.0. A fresh session per operation keeps cancellation and UNKNOWN semantics honest; discovery
and call single-flight are enforced by their own locks instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

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
        on_list_changed: Any,
        run_id: str | None = None,
        permit: str | None = None,
    ) -> AsyncIterator[SdkSession]:
        require_enabled()
        if not provider.enabled:
            raise McpError(ErrorCode.ADAPTER_DISABLED, "provider_disabled")
        if provider.provider_id in self._revoked:
            raise McpError(ErrorCode.POLICY_DENIED, "provider_revoked")
        credential: Credential | None = await self.credentials.resolve(provider, run_id=run_id)
        if provider.auth_mode != "none" and credential is None:
            raise McpError(ErrorCode.CREDENTIAL_UNAVAILABLE, "credential_required")
        async with self._semaphore:
            self._active += 1
            try:
                async with self.transport.open(
                    provider=provider,
                    credential=credential,
                    on_list_changed=on_list_changed,
                    limits=self.limits,
                    permit=permit,
                ) as session:
                    yield session
            finally:
                self._active -= 1

    @asynccontextmanager
    async def call_slot(self, provider_id: str) -> AsyncIterator[None]:
        """Per-provider concurrency ceiling for admitted tool calls."""
        del provider_id  # one global ceiling today; the key is kept for a future per-provider split
        async with self._call_semaphore:
            yield

    @property
    def active(self) -> int:
        return self._active
