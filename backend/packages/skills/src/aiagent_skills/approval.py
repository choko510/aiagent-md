"""Exact-digest review approvals with a hard 24h TTL and no replay across releases."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta

from aiagent_domain.canonical import canonical_json, sha256_hex

from .contracts import SkillApproval, SkillRelease
from .errors import ApprovalExpired, ApprovalInvalid

MAX_APPROVAL_TTL_SECONDS = 24 * 60 * 60


def parse_instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ApprovalInvalid("timestamp is not a canonical instant") from None
    if parsed.tzinfo is None:
        raise ApprovalInvalid("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def approval_binding_digest(approval: SkillApproval) -> str:
    return sha256_hex(
        canonical_json(
            {
                "tenantId": approval.tenant_id,
                "ownerUserId": approval.owner_user_id,
                "actorUserId": approval.actor_user_id,
                "action": approval.action,
                "releaseId": approval.release_id,
                "contentDigest": approval.content_digest,
                "publisherId": approval.publisher_id,
                "capabilityDigest": approval.capability_digest,
                "policyRevision": approval.policy_revision,
                "scanDigest": approval.scan_digest,
                "requestId": approval.request_id,
                "expectedInstallationRevision": approval.expected_installation_revision,
                "expiresAt": approval.expires_at,
            }
        )
    )


def create_approval(
    *,
    approval_id: str,
    tenant_id: str,
    owner_user_id: str,
    actor_user_id: str,
    action: str,
    release: SkillRelease,
    capability_digest: str,
    policy_revision: str,
    request_id: str,
    expected_installation_revision: int,
    now: str,
    ttl_seconds: int = 3600,
) -> SkillApproval:
    if ttl_seconds <= 0 or ttl_seconds > MAX_APPROVAL_TTL_SECONDS:
        raise ApprovalInvalid("approval TTL must be between 1 second and 24 hours")
    created = parse_instant(now)
    expires = created + timedelta(seconds=ttl_seconds)
    return SkillApproval(
        id=approval_id,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        actor_user_id=actor_user_id,
        action=action,
        release_id=release.id,
        content_digest=release.content_digest,
        publisher_id=release.manifest.publisher_id,
        capability_digest=capability_digest,
        policy_revision=policy_revision,
        scan_digest=release.scan_digest,
        request_id=request_id,
        expected_installation_revision=expected_installation_revision,
        expires_at=expires.isoformat().replace("+00:00", "Z"),
        created_at=now,
    )


def verify_approval(
    approval: SkillApproval,
    *,
    tenant_id: str,
    owner_user_id: str,
    actor_user_id: str,
    action: str,
    release: SkillRelease,
    capability_digest: str,
    policy_revision: str,
    expected_installation_revision: int,
    now: str,
) -> None:
    checks = {
        "tenant": hmac.compare_digest(approval.tenant_id, tenant_id),
        "owner": hmac.compare_digest(approval.owner_user_id, owner_user_id),
        "actor": hmac.compare_digest(approval.actor_user_id, actor_user_id),
        "action": hmac.compare_digest(approval.action, action),
        "release": hmac.compare_digest(approval.release_id, release.id),
        "content": hmac.compare_digest(approval.content_digest, release.content_digest),
        "scan": hmac.compare_digest(approval.scan_digest, release.scan_digest),
        "capability": hmac.compare_digest(approval.capability_digest, capability_digest),
        "policy": hmac.compare_digest(approval.policy_revision, policy_revision),
        "revision": approval.expected_installation_revision == expected_installation_revision,
    }
    if not all(checks.values()):
        raise ApprovalInvalid("approval does not bind the current release and policy")
    if parse_instant(now) >= parse_instant(approval.expires_at):
        raise ApprovalExpired("review approval has expired")
