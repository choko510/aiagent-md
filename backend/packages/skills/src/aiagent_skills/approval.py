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
                "installationId": approval.installation_id,
                "targetReleaseId": approval.target_release_id,
                "enableAfter": approval.enable_after,
                "reenable": approval.reenable,
                "expectedInstallationRevision": approval.expected_installation_revision,
                "createdAt": approval.created_at,
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
    installation_id: str | None = None,
    target_release_id: str | None = None,
    enable_after: bool = False,
    reenable: bool = False,
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
        installation_id=installation_id,
        target_release_id=target_release_id,
        enable_after=enable_after,
        reenable=reenable,
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
    request_id: str | None = None,
    installation_id: str | None = None,
    target_release_id: str | None = None,
    enable_after: bool | None = None,
    reenable: bool | None = None,
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
        "publisher": (
            approval.publisher_id == release.manifest.publisher_id
            if approval.publisher_id is None or release.manifest.publisher_id is None
            else hmac.compare_digest(approval.publisher_id, release.manifest.publisher_id)
        ),
        "revision": approval.expected_installation_revision == expected_installation_revision,
        "request": request_id is None or approval.request_id == request_id,
        "installation": installation_id is None or approval.installation_id == installation_id,
        "target": target_release_id is None or approval.target_release_id == target_release_id,
        "enableAfter": enable_after is None or approval.enable_after is enable_after,
        "reenable": reenable is None or approval.reenable is reenable,
    }
    if not all(checks.values()):
        raise ApprovalInvalid("approval does not bind the current release, request and policy")
    created = parse_instant(approval.created_at)
    expires = parse_instant(approval.expires_at)
    current = parse_instant(now)
    if created > current:
        raise ApprovalInvalid("approval was issued in the future")
    if expires <= created or expires - created > timedelta(seconds=MAX_APPROVAL_TTL_SECONDS):
        raise ApprovalInvalid("approval expiry exceeds the maximum TTL")
    if current >= expires:
        raise ApprovalExpired("review approval has expired")
