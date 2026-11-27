"""Admin tool-publication policy. This is NOT user approval for an external side effect.

A policy record binds one exact descriptor revision and both schema digests. Any schema, description or
semantic change yields a new revision, so the old approval never silently carries over.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from .canonical import digest
from .contracts import RiskClass, ToolDescriptor, utc_now
from .errors import ErrorCode, McpError


@dataclass(frozen=True)
class ToolPolicy:
    provider_id: str
    tenant_id: str
    owner_user_id: str
    tool_name: str
    descriptor_revision: str
    input_digest: str
    output_digest: str | None
    risk_class: RiskClass
    approval_required: bool
    enabled: bool
    approved_by: str
    approved_at: datetime
    policy_revision: int
    record_digest: str = ""


@dataclass(frozen=True)
class PolicyDecision:
    policy: ToolPolicy | None
    reason: Literal[
        "approved",
        "not_allowlisted",
        "unclassified",
        "descriptor_changed",
        "disabled",
        "revoked",
        "provider_mismatch",
        "scope_mismatch",
        "record_identity_mismatch",
        "policy_revision_mismatch",
        "record_integrity_failed",
    ]


class PolicySet:
    """Immutable provider policy set; every mutation returns a new revision."""

    def __init__(
        self,
        *,
        provider_id: str,
        tenant_id: str = "",
        owner_user_id: str = "",
        revision: int,
        allowlist: frozenset[str],
        policies: dict[str, ToolPolicy] | None = None,
        revoked: frozenset[str] = frozenset(),
    ) -> None:
        if revision < 1:
            raise ValueError("policy revision must be >= 1")
        self.provider_id = provider_id
        self.tenant_id = tenant_id
        self.owner_user_id = owner_user_id
        self.revision = revision
        self.allowlist = allowlist
        self._policies = dict(policies or {})
        self.revoked = revoked

    def decide(self, descriptor: ToolDescriptor) -> PolicyDecision:
        name = descriptor.source_name
        if name in self.revoked:
            return PolicyDecision(None, "revoked")
        if name not in self.allowlist:
            return PolicyDecision(None, "not_allowlisted")
        policy = self._policies.get(name)
        if policy is None:
            return PolicyDecision(None, "unclassified")
        if policy.tool_name != name:
            # The dictionary key is not the record's identity.
            return PolicyDecision(None, "record_identity_mismatch")
        if policy.provider_id != self.provider_id:
            # A policy record cannot be transplanted from another provider with the same tool name.
            return PolicyDecision(None, "provider_mismatch")
        if policy.tenant_id != self.tenant_id or policy.owner_user_id != self.owner_user_id:
            return PolicyDecision(None, "scope_mismatch")
        if not policy.approved_by.strip():
            return PolicyDecision(None, "unclassified")
        if policy.policy_revision > self.revision:
            # A record approved for a future revision is not authority for this set.
            return PolicyDecision(None, "policy_revision_mismatch")
        if not policy.record_digest or policy.record_digest != _record_digest(policy):
            # Detects accidental or in-place replacement of an approved record. Cryptographic
            # provenance and the single-use authority live in Sentinel, not in this local digest.
            return PolicyDecision(None, "record_integrity_failed")
        if not policy.enabled:
            return PolicyDecision(None, "disabled")
        if (
            policy.descriptor_revision != descriptor.revision
            or policy.input_digest != digest(descriptor.input_schema)
            or policy.output_digest != (digest(descriptor.output_schema) if descriptor.output_schema else None)
        ):
            return PolicyDecision(None, "descriptor_changed")
        return PolicyDecision(policy, "approved")

    def approve(
        self,
        descriptor: ToolDescriptor,
        *,
        risk_class: RiskClass,
        approval_required: bool,
        actor: str,
        now: datetime | None = None,
    ) -> PolicySet:
        if risk_class not in ("read", "write_reversible", "external_irreversible"):
            raise McpError(ErrorCode.POLICY_DENIED, "explicit_risk_class_required")
        if not actor.strip():
            raise McpError(ErrorCode.POLICY_DENIED, "approving_actor_required")
        if descriptor.source_name not in self.allowlist:
            raise McpError(ErrorCode.POLICY_DENIED, "tool_not_allowlisted")
        record = ToolPolicy(
            provider_id=self.provider_id,
            tenant_id=self.tenant_id,
            owner_user_id=self.owner_user_id,
            tool_name=descriptor.source_name,
            descriptor_revision=descriptor.revision,
            input_digest=digest(descriptor.input_schema),
            output_digest=digest(descriptor.output_schema) if descriptor.output_schema else None,
            risk_class=risk_class,
            approval_required=approval_required,
            enabled=True,
            approved_by=actor,
            approved_at=now or utc_now(),
            policy_revision=self.revision + 1,
        )
        record = replace(record, record_digest=_record_digest(record))
        policies = {**self._policies, descriptor.source_name: record}
        return PolicySet(
            provider_id=self.provider_id,
            tenant_id=self.tenant_id,
            owner_user_id=self.owner_user_id,
            revision=self.revision + 1,
            allowlist=self.allowlist,
            policies=policies,
            revoked=self.revoked - {descriptor.source_name},
        )

    def revoke(self, tool_name: str) -> PolicySet:
        if tool_name not in self._policies and tool_name not in self.allowlist:
            raise McpError(ErrorCode.POLICY_DENIED, "unknown_tool")
        policies = {name: p for name, p in self._policies.items() if name != tool_name}
        return PolicySet(
            provider_id=self.provider_id,
            tenant_id=self.tenant_id,
            owner_user_id=self.owner_user_id,
            revision=self.revision + 1,
            allowlist=self.allowlist,
            policies=policies,
            revoked=self.revoked | {tool_name},
        )

    def with_allowlist(self, names: tuple[str, ...]) -> PolicySet:
        return PolicySet(
            provider_id=self.provider_id,
            tenant_id=self.tenant_id,
            owner_user_id=self.owner_user_id,
            revision=self.revision + 1,
            allowlist=frozenset(names),
            policies={k: v for k, v in self._policies.items() if k in names},
            revoked=self.revoked,
        )

    def matches_scope(self, *, provider_id: str, tenant_id: str, owner_user_id: str) -> bool:
        return self.provider_id == provider_id and self.tenant_id == tenant_id and self.owner_user_id == owner_user_id


def _record_digest(policy: ToolPolicy) -> str:
    return digest(
        {
            "provider_id": policy.provider_id,
            "tenant_id": policy.tenant_id,
            "owner_user_id": policy.owner_user_id,
            "tool_name": policy.tool_name,
            "descriptor_revision": policy.descriptor_revision,
            "input_digest": policy.input_digest,
            "output_digest": policy.output_digest,
            "risk_class": policy.risk_class,
            "approval_required": bool(policy.approval_required),
            "enabled": bool(policy.enabled),
            "approved_by": policy.approved_by,
            "approved_at": policy.approved_at.isoformat(),
            "policy_revision": policy.policy_revision,
        }
    )
