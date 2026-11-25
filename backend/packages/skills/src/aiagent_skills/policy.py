"""Capability requests are narrowing constraints, never grants.

Effective Skill capability is the intersection of the actor, tenant, task/run and current Gateway policy,
further restricted to the approved Skill allowlist and to what the Skill explicitly requested. A Skill with no
capability request grants nothing; a Skill with a request cannot add a single capability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .digest import capability_digest
from .errors import UnsupportedCapability

CAPABILITY_RE = re.compile(r"\A(tool|mcp|network|filesystem):[a-z0-9][a-z0-9._/-]{0,127}\Z")
FORBIDDEN_PREFIXES = (
    "approval:",
    "policy:",
    "secret:",
    "shell:",
    "sandbox:",
    "gateway:",
    "docker:",
    "script:",
)


@dataclass(frozen=True)
class CapabilityDecision:
    effective: frozenset[str]
    narrowed: frozenset[str]
    denied: frozenset[str]
    digest: str


def validate_requested(capabilities: tuple[str, ...] | list[str]) -> frozenset[str]:
    out: set[str] = set()
    for item in capabilities:
        value = item.strip()
        if not value or len(value) > 160:
            raise UnsupportedCapability("capability token is invalid")
        if value.startswith(FORBIDDEN_PREFIXES):
            kind = value.split(":", 1)[0]
            raise UnsupportedCapability(f"capability class cannot be requested by a Skill: {kind}")
        if not CAPABILITY_RE.match(value):
            raise UnsupportedCapability(f"unsupported capability token: {value[:40]}")
        out.add(value)
    return frozenset(out)


def effective_capabilities(
    requested: tuple[str, ...] | list[str],
    *,
    actor_grants: frozenset[str],
    tenant_policy: frozenset[str],
    run_policy: frozenset[str],
    gateway_policy: frozenset[str],
    approved_allowlist: frozenset[str],
) -> CapabilityDecision:
    request = validate_requested(requested)
    grants = actor_grants & tenant_policy & run_policy & gateway_policy & approved_allowlist
    effective = frozenset() if not request else request & grants
    denied = request - effective
    narrowed = grants - request if request else grants
    return CapabilityDecision(effective, narrowed, denied, capability_digest(effective))


def assert_no_escalation(before: frozenset[str], after: frozenset[str]) -> None:
    added = after - before
    if added:
        raise UnsupportedCapability("capability set may not grow", added=sorted(added))


def assert_tool_allowed(effective: frozenset[str], tool_name: str) -> None:
    """A Skill may only reference a tool that survived the intersection; the Gateway still authorizes the call."""
    if f"tool:{tool_name}" not in effective:
        raise UnsupportedCapability(f"tool is not in the effective Skill capability: {tool_name}")


def assert_mcp_declared(effective: frozenset[str], server_tool: str) -> None:
    if f"mcp:{server_tool}" not in effective:
        raise UnsupportedCapability(f"MCP dependency is not available: {server_tool}")
