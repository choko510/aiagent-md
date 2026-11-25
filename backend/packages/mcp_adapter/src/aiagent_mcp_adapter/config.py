"""Adapter configuration: finite bounds, admin-approved endpoints, no secrets.

Nothing here is derived from MCP server metadata. Provider identity is admin-assigned; the endpoint and
credential binding are admin configuration. The only secret material ever stored here is an opaque
`credential_binding_ref` resolved by the Credential Broker inside the worker, never a token.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import ErrorCode, McpError

# Hard ceilings. Admin configuration may lower these; nothing may raise them without a code review.
MAX_PAGES = 64
MAX_TOOLS = 128
MAX_LISTING_BYTES = 1024 * 1024
MAX_SCHEMA_BYTES = 64 * 1024
MAX_REQUEST_SECONDS = 60.0
MAX_LIST_SECONDS = 30.0
MAX_CALL_SECONDS = 300.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024
MAX_SESSIONS = 64
MAX_CONCURRENT_CALLS = 2
MAX_RECONNECT_ATTEMPTS = 3
MAX_SESSION_TTL_SECONDS = 1800.0
MAX_SSE_EVENT_BYTES = 1024 * 1024

FeatureFlag = Literal["MCP_ADAPTER_ENABLED"]


def adapter_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Feature flag: absent/false means the adapter cannot connect, discover or expose tools."""
    source = os.environ if env is None else env
    return source.get("MCP_ADAPTER_ENABLED", "").strip() == "1"


def require_enabled(env: Mapping[str, str] | None = None) -> None:
    if not adapter_enabled(env):
        raise McpError(ErrorCode.ADAPTER_DISABLED, "MCP_ADAPTER_ENABLED is not 1")


class AdapterLimits(BaseModel):
    """Finite per-operation bounds. Every field has a hard ceiling enforced at validation time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_pages: int = Field(default=MAX_PAGES, ge=1, le=MAX_PAGES)
    max_tools: int = Field(default=MAX_TOOLS, ge=1, le=MAX_TOOLS)
    max_listing_bytes: int = Field(default=MAX_LISTING_BYTES, ge=1024, le=MAX_LISTING_BYTES)
    max_schema_bytes: int = Field(default=MAX_SCHEMA_BYTES, ge=1024, le=MAX_SCHEMA_BYTES)
    max_schema_depth: int = Field(default=24, ge=1, le=64)
    max_properties: int = Field(default=256, ge=1, le=1024)
    max_regex_length: int = Field(default=512, ge=1, le=2048)
    list_timeout_s: float = Field(default=5.0, gt=0, le=MAX_LIST_SECONDS)
    connect_timeout_s: float = Field(default=5.0, gt=0, le=MAX_LIST_SECONDS)
    call_timeout_s: float = Field(default=10.0, gt=0, le=MAX_CALL_SECONDS)
    max_response_bytes: int = Field(default=MAX_RESPONSE_BYTES, ge=1024, le=MAX_RESPONSE_BYTES)
    max_text_bytes: int = Field(default=MAX_TEXT_BYTES, ge=1024, le=MAX_TEXT_BYTES)
    max_sessions: int = Field(default=MAX_SESSIONS, ge=1, le=MAX_SESSIONS)
    max_concurrent_calls: int = Field(default=MAX_CONCURRENT_CALLS, ge=1, le=MAX_CONCURRENT_CALLS)
    max_reconnect_attempts: int = Field(default=MAX_RECONNECT_ATTEMPTS, ge=0, le=MAX_RECONNECT_ATTEMPTS)
    session_ttl_s: float = Field(default=300.0, gt=0, le=MAX_SESSION_TTL_SECONDS)
    snapshot_ttl_s: float = Field(default=600.0, gt=0, le=MAX_SESSION_TTL_SECONDS)
    max_refresh_attempts: int = Field(default=2, ge=1, le=4)
    max_sse_event_bytes: int = Field(default=MAX_SSE_EVENT_BYTES, ge=1024, le=MAX_SSE_EVENT_BYTES)

    @model_validator(mode="after")
    def _snapshot_not_shorter_than_session(self) -> AdapterLimits:
        if self.snapshot_ttl_s < self.session_ttl_s:
            raise ValueError("snapshot_ttl_s must be >= session_ttl_s")
        return self


AuthMode = Literal["none", "bearer", "api_key_header"]


class ProviderConfig(BaseModel):
    """Admin-owned provider configuration. `provider_id` is assigned by an administrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    tenant_id: str = Field(min_length=1, max_length=128)
    owner_user_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1, max_length=2048)
    transport: Literal["streamable_http"] = "streamable_http"
    enabled: bool = False
    auth_mode: AuthMode = "none"
    credential_binding_ref: str | None = Field(default=None, max_length=256)
    credential_header: str | None = Field(default=None, max_length=64)
    credential_version: int = Field(default=1, ge=1)
    policy_revision: int = Field(default=1, ge=1)
    allowed_tools: tuple[str, ...] = ()
    # Test-only escape hatch: plaintext http:// and loopback endpoints. Refused outside AIAGENT_PROFILE=test.
    allow_loopback_for_tests: bool = False

    @field_validator("allowed_tools")
    @classmethod
    def _allowed_tools_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("allowed_tools contains duplicates")
        for name in value:
            if not name or len(name) > 128 or name.strip() != name:
                raise ValueError("invalid allowed tool name")
        return value

    @model_validator(mode="after")
    def _loopback_only_in_tests(self) -> ProviderConfig:
        if self.allow_loopback_for_tests and os.environ.get("AIAGENT_PROFILE", "production") != "test":
            raise ValueError("allow_loopback_for_tests is refused outside the test profile")
        return self

    @model_validator(mode="after")
    def _credential_consistency(self) -> ProviderConfig:
        if self.auth_mode == "none":
            if self.credential_binding_ref is not None or self.credential_header is not None:
                raise ValueError("auth_mode none cannot carry a credential binding or header")
            return self
        if not self.credential_binding_ref:
            raise ValueError("auth_mode requires credential_binding_ref")
        if self.auth_mode == "bearer":
            if self.credential_header not in (None, "Authorization"):
                raise ValueError("bearer credentials are injected only as Authorization")
        elif self.auth_mode == "api_key_header":
            header = self.credential_header or ""
            if not header.startswith("X-") or not all(c.isalnum() or c in "-_" for c in header):
                raise ValueError("api_key_header must be a single X- header name")
        return self

    @property
    def endpoint_origin(self) -> EndpointOrigin:
        return parse_endpoint(self.endpoint, allow_loopback=self.allow_loopback_for_tests)

    @property
    def endpoint_fingerprint(self) -> str:
        origin = self.endpoint_origin
        return hashlib.sha256(f"{origin.scheme}://{origin.host}:{origin.port}{origin.path}".encode()).hexdigest()[:32]


@dataclass(frozen=True)
class EndpointOrigin:
    scheme: str
    host: str
    port: int
    path: str

    @property
    def origin(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default = 443 if self.scheme == "https" else 80
        netloc = host if self.port == default else f"{host}:{self.port}"
        return f"{self.scheme}://{netloc}{self.path}"


_DENY_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)
_DENY_SUFFIXES = (".localhost", ".local", ".internal", ".metadata.google.internal")


def parse_endpoint(url: str, *, allow_loopback: bool = False) -> EndpointOrigin:
    """Validate an admin-approved endpoint. No userinfo/query/fragment, no private host by default."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise McpError(ErrorCode.ENDPOINT_DENIED, "invalid_endpoint")
    if any(ch in url for ch in ("\\", "\r", "\n", "\0", " ")):
        raise McpError(ErrorCode.ENDPOINT_DENIED, "invalid_endpoint")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise McpError(ErrorCode.ENDPOINT_DENIED, "invalid_endpoint") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise McpError(ErrorCode.ENDPOINT_DENIED, "unsupported_scheme")
    if parts.username is not None or parts.password is not None:
        raise McpError(ErrorCode.ENDPOINT_DENIED, "endpoint_userinfo_forbidden")
    if parts.query or parts.fragment:
        raise McpError(ErrorCode.ENDPOINT_DENIED, "endpoint_query_or_fragment_forbidden")
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host or len(host) > 255:
        raise McpError(ErrorCode.ENDPOINT_DENIED, "invalid_host")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise McpError(ErrorCode.ENDPOINT_DENIED, "invalid_port") from exc
    if not 1 <= port <= 65535:
        raise McpError(ErrorCode.ENDPOINT_DENIED, "invalid_port")
    path = parts.path or "/"
    if len(path) > 512 or not path.startswith("/"):
        raise McpError(ErrorCode.ENDPOINT_DENIED, "invalid_path")

    loopback = _is_loopback_host(host)
    if scheme == "http" and not (allow_loopback and loopback):
        raise McpError(ErrorCode.ENDPOINT_DENIED, "https_required")
    if (host in _DENY_HOSTS or host.endswith(_DENY_SUFFIXES)) and not (allow_loopback and loopback):
        raise McpError(ErrorCode.ENDPOINT_DENIED, "private_or_metadata_host")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A single-label hostname is never a legitimate public provider endpoint.
        if "." not in host and not (allow_loopback and loopback):
            raise McpError(ErrorCode.ENDPOINT_DENIED, "single_label_host") from None
        if not allow_loopback and host.startswith(("127.", "10.", "192.168.", "169.254.")):
            raise McpError(ErrorCode.ENDPOINT_DENIED, "private_host") from None
    else:
        if not _public_ip(ip) and not (allow_loopback and (ip.is_loopback or ip.is_unspecified)):
            raise McpError(ErrorCode.ENDPOINT_DENIED, "private_ip")
    return EndpointOrigin(scheme=scheme, host=host, port=port, path=path)


def _is_loopback_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def default_limits(**overrides: Any) -> AdapterLimits:
    return AdapterLimits(**overrides)
