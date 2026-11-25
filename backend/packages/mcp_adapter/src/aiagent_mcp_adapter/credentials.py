"""Credential Broker seam. There is no environment-variable or config-file fallback in production.

Concrete brokers live outside this package; this module only defines the shape and test-only doubles that
refuse to operate outside `AIAGENT_PROFILE=test`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol

from .config import ProviderConfig
from .errors import ErrorCode, McpError
from .transport import Credential


class CredentialBroker(Protocol):
    async def resolve(self, provider: ProviderConfig, *, run_id: str | None = None) -> Credential | None: ...


class StaticCredentialBroker:
    """TEST ONLY. Holds already-resolved values in memory; never reads env/config files."""

    def __init__(self, credentials: Mapping[str, Credential]) -> None:
        if os.environ.get("AIAGENT_PROFILE", "") != "test":
            raise McpError(ErrorCode.CREDENTIAL_UNAVAILABLE, "static_broker_test_only")
        self._credentials = dict(credentials)

    async def resolve(self, provider: ProviderConfig, *, run_id: str | None = None) -> Credential | None:
        if provider.auth_mode == "none":
            return None
        credential = self._credentials.get(provider.provider_id)
        if credential is None:
            raise McpError(ErrorCode.CREDENTIAL_UNAVAILABLE, "credential_not_found")
        if credential.version != provider.credential_version:
            raise McpError(ErrorCode.CREDENTIAL_STALE, "credential_version_mismatch")
        return credential


class RefusingCredentialBroker:
    """Safe default when no broker is wired: an authenticated provider cannot be used at all."""

    async def resolve(self, provider: ProviderConfig, *, run_id: str | None = None) -> Credential | None:
        if provider.auth_mode == "none":
            return None
        raise McpError(ErrorCode.CREDENTIAL_UNAVAILABLE, "credential_broker_not_wired")
