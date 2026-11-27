"""Shared fixtures for the MCP adapter tests (no network, no database)."""

from __future__ import annotations

from typing import Any

from aiagent_mcp_adapter.config import AdapterLimits, ProviderConfig
from aiagent_mcp_adapter.contracts import (
    ToolDescriptor,
    ToolEntry,
    build_entry,
    descriptor_from_tool,
    snapshot_from_entries,
)
from aiagent_mcp_adapter.policy import PolicySet

ECHO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 2000}},
    "required": ["text"],
    "additionalProperties": False,
}


def provider(**overrides: Any) -> ProviderConfig:
    data: dict[str, Any] = {
        "provider_id": "demo",
        "tenant_id": "tenant-a",
        "owner_user_id": "owner-a",
        "display_name": "Demo MCP",
        "endpoint": "https://mcp.example.com/mcp",
        "enabled": True,
        "auth_mode": "none",
        "allowed_tools": ("echo",),
        "policy_revision": 2,
    }
    data.update(overrides)
    return ProviderConfig(**data)


def limits(**overrides: Any) -> AdapterLimits:
    return AdapterLimits(**overrides)


def descriptor(name: str = "echo", **kwargs: Any) -> ToolDescriptor:
    return descriptor_from_tool(
        name=name,
        description=kwargs.pop("description", "Echo text back."),
        input_schema=kwargs.pop("input_schema", ECHO_SCHEMA),
        output_schema=kwargs.pop("output_schema", None),
    )


def entry(
    *,
    desc: ToolDescriptor | None = None,
    provider_id: str = "demo",
    tenant_id: str = "tenant-a",
    owner_user_id: str = "owner-a",
    risk_class: str = "read",
    approval_required: bool = False,
    generation: int = 1,
    policy_revision: int = 2,
) -> ToolEntry:
    desc = desc or descriptor()
    return build_entry(
        descriptor=desc,
        provider_id=provider_id,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        risk_class=risk_class,  # type: ignore[arg-type]
        approval_required=approval_required,
        policy_revision=policy_revision,
        credential_version=1,
        generation=generation,
    )


def snapshot(*entries: ToolEntry, **overrides: Any):
    entries = entries or (entry(),)
    first = entries[0]
    return snapshot_from_entries(
        tenant_id=first.tenant_id,
        owner_user_id=first.owner_user_id,
        provider_id=first.provider_id,
        generation=overrides.pop("generation", first.generation),
        policy_revision=overrides.pop("policy_revision", first.policy_revision),
        credential_version=first.credential_version,
        protocol_revision=overrides.pop("protocol_revision", "2025-11-25"),
        endpoint_fingerprint=overrides.pop("endpoint_fingerprint", provider().endpoint_fingerprint),
        entries=list(entries),
        ttl_s=overrides.pop("ttl_s", 600.0),
        **overrides,
    )


def approved_policy(desc: ToolDescriptor | None = None, *, risk_class: str = "read") -> PolicySet:
    desc = desc or descriptor()
    base = PolicySet(provider_id="demo", revision=1, allowlist=frozenset({desc.source_name}))
    return base.approve(desc, risk_class=risk_class, approval_required=False, actor="admin")  # type: ignore[arg-type]
