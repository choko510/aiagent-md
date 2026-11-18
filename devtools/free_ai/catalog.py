"""Immutable catalog of the four external free-AI services used by the dev lab.

This module is network-free and environment-free by construction: it exposes frozen
constants only. Endpoints, model identifiers and limits cannot be overridden through
``os.environ``, CLI flags or user input, and no other module may build a URL.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

ProviderId = Literal["kilo", "ovh", "jev_legacy", "pollinations_legacy"]
WireKind = Literal["chat_completions", "system_one"]
Eligibility = Literal["ELIGIBLE", "EXPERIMENTAL_DISABLED"]
EvidenceClass = Literal["official_anonymous", "user_observed_legacy"]

#: Date on which the catalog values below were last reviewed against primary sources.
CATALOG_CHECKED_ON = "2026-11-18"


class CatalogError(RuntimeError):
    """A provider is unknown, its spec was tampered with, or its endpoint is not HTTPS."""


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """One frozen catalog entry. Instances are only valid when identical to the catalog."""

    provider_id: ProviderId
    endpoint: str
    model_id: str
    wire_kind: WireKind
    eligibility: Eligibility
    evidence_class: EvidenceClass
    min_interval_seconds: int
    max_calls_per_local_day: int

    @property
    def endpoint_host(self) -> str:
        """Return the host only; the full endpoint never enters a stored record."""
        host = urlsplit(self.endpoint).hostname
        return "" if host is None else host

    @property
    def is_legacy(self) -> bool:
        """True for services whose anonymous terms are not currently confirmed."""
        return self.eligibility == "EXPERIMENTAL_DISABLED"


_FROZEN_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        provider_id="kilo",
        endpoint="https://ai-gateway.kilo.ai/api/v1/chat/completions",
        model_id="nvidia/nemotron-3.5-super-vl:free",
        wire_kind="chat_completions",
        eligibility="ELIGIBLE",
        evidence_class="official_anonymous",
        min_interval_seconds=180,
        max_calls_per_local_day=16,
    ),
    ProviderSpec(
        provider_id="ovh",
        endpoint="https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
        model_id="gpt-oss-120b",
        wire_kind="chat_completions",
        eligibility="ELIGIBLE",
        evidence_class="official_anonymous",
        min_interval_seconds=65,
        max_calls_per_local_day=12,
    ),
    ProviderSpec(
        provider_id="jev_legacy",
        endpoint="https://opencode.ai/zen/v1/systemone",
        model_id="jev-1.13-free",
        wire_kind="system_one",
        eligibility="EXPERIMENTAL_DISABLED",
        evidence_class="user_observed_legacy",
        min_interval_seconds=300,
        max_calls_per_local_day=2,
    ),
    ProviderSpec(
        provider_id="pollinations_legacy",
        endpoint="https://text.pollinations.ai/openai",
        model_id="openai-fast",
        wire_kind="chat_completions",
        eligibility="EXPERIMENTAL_DISABLED",
        evidence_class="user_observed_legacy",
        min_interval_seconds=300,
        max_calls_per_local_day=2,
    ),
)

PROVIDERS: Mapping[str, ProviderSpec] = MappingProxyType(
    {spec.provider_id: spec for spec in _FROZEN_SPECS},
)

ALL_PROVIDER_IDS: tuple[ProviderId, ...] = ("kilo", "ovh", "jev_legacy", "pollinations_legacy")

#: Legacy services may only be called when their anonymous use is confirmed against
#: the current primary documentation. On :data:`CATALOG_CHECKED_ON` the current docs of
#: both services require an API key, so both entries stay ``False`` and the CLI skips
#: them without sending anything. Flipping one to ``True`` is a deliberate, reviewed
#: change that must cite the checked source, never an environment variable.
LEGACY_ANONYMOUS_ALLOWED: Mapping[ProviderId, bool] = MappingProxyType(
    {
        "jev_legacy": False,
        "pollinations_legacy": False,
    },
)


def _check_endpoint_shape(spec: ProviderSpec) -> None:
    """Reject any endpoint that is not a fixed public HTTPS origin with a concrete path."""
    parts = urlsplit(spec.endpoint)
    if parts.scheme != "https":
        raise CatalogError("endpoint scheme must be https")
    if parts.username is not None or parts.password is not None:
        raise CatalogError("endpoint must not carry URL credentials")
    if parts.query or parts.fragment:
        raise CatalogError("endpoint must not carry a query or fragment")
    if parts.port not in (None, 443):
        raise CatalogError("endpoint must use the default HTTPS port")
    host = parts.hostname
    if not host:
        raise CatalogError("endpoint must name a host")
    if host == "localhost" or host.endswith(".localhost"):
        raise CatalogError("endpoint must not be a loopback host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise CatalogError("endpoint must not be an IP literal")
    if not parts.path.startswith("/") or parts.path == "/":
        raise CatalogError("endpoint must include a fixed path")


def validate_spec(spec: ProviderSpec) -> None:
    """Raise :class:`CatalogError` unless ``spec`` is the frozen catalog instance."""
    frozen = _FROZEN_SPECS_BY_ID.get(spec.provider_id)
    if frozen is None or frozen != spec:
        raise CatalogError("provider spec is not the frozen catalog entry")
    _check_endpoint_shape(spec)


_FROZEN_SPECS_BY_ID: Mapping[ProviderId, ProviderSpec] = MappingProxyType(
    {spec.provider_id: spec for spec in _FROZEN_SPECS},
)


def get_spec(provider_id: str) -> ProviderSpec | None:
    """Return the frozen spec for ``provider_id`` or ``None`` when unknown."""
    return PROVIDERS.get(provider_id)


def require_spec(provider_id: str) -> ProviderSpec:
    """Return the frozen spec or raise :class:`CatalogError`."""
    spec = get_spec(provider_id)
    if spec is None:
        raise CatalogError("unknown provider id")
    return spec


def catalog_specs() -> tuple[ProviderSpec, ...]:
    """Return the catalog in display order (kilo, ovh, jev_legacy, pollinations_legacy)."""
    return tuple(require_spec(provider_id) for provider_id in ALL_PROVIDER_IDS)


def _validate_catalog_at_import() -> None:
    for spec in _FROZEN_SPECS:
        validate_spec(spec)
        if spec.min_interval_seconds <= 0 or spec.max_calls_per_local_day <= 0:
            raise CatalogError("catalog limits must be positive")
        if not spec.model_id or any(char.isspace() for char in spec.model_id):
            raise CatalogError("catalog model id must be a fixed non-empty token")


_validate_catalog_at_import()

__all__ = [
    "ALL_PROVIDER_IDS",
    "CATALOG_CHECKED_ON",
    "LEGACY_ANONYMOUS_ALLOWED",
    "PROVIDERS",
    "CatalogError",
    "Eligibility",
    "EvidenceClass",
    "ProviderId",
    "ProviderSpec",
    "WireKind",
    "catalog_specs",
    "get_spec",
    "require_spec",
    "validate_spec",
]
