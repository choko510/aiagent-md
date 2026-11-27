"""Immutable contracts shared by the registry, API and runtime loader.

These are module-private Skill types. They do not redefine shared contracts (IDs are UUIDv7 strings,
timestamps are canonical UtcInstant strings) and never carry tool authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ReleaseState(StrEnum):
    DISCOVERED = "DISCOVERED"
    DOWNLOADING = "DOWNLOADING"
    QUARANTINED = "QUARANTINED"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    STAGED_FOR_REVIEW = "STAGED_FOR_REVIEW"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"
    REVOKED = "REVOKED"


class InstallationState(StrEnum):
    NOT_INSTALLED = "NOT_INSTALLED"
    INSTALLING = "INSTALLING"
    INSTALLED_DISABLED = "INSTALLED_DISABLED"
    ENABLING = "ENABLING"
    ENABLED = "ENABLED"
    SUSPENDED = "SUSPENDED"
    UPDATING = "UPDATING"
    ROLLING_BACK = "ROLLING_BACK"
    REMOVING = "REMOVING"
    REMOVED = "REMOVED"
    REVOKED = "REVOKED"


class DesiredState(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"


class TrustLevel(StrEnum):
    BUILT_IN = "built_in"
    USER_REVIEWED = "user_reviewed"
    THIRD_PARTY_UNTRUSTED = "third_party_untrusted"


class SourceKind(StrEnum):
    BUILTIN = "builtin"
    UPLOAD = "upload"
    GITHUB = "github"
    CLAWHUB = "clawhub"
    GENERATED = "generated"


class Severity(StrEnum):
    BLOCKING = "BLOCKING"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class CapabilityKind(StrEnum):
    TOOL = "tool"
    MCP = "mcp"
    SCRIPT = "script"
    NETWORK = "network"
    FILESYSTEM = "filesystem"


@dataclass(frozen=True)
class SkillFile:
    relative_path: str
    sha256: str
    byte_size: int
    media_type: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "relativePath": self.relative_path,
            "sha256": self.sha256,
            "byteSize": self.byte_size,
            "mediaType": self.media_type,
        }


@dataclass(frozen=True)
class SkillSource:
    kind: SourceKind
    origin_uri: str | None = None
    publisher_id: str | None = None
    publisher_handle: str | None = None
    commit_sha: str | None = None
    artifact_digest: str | None = None
    catalog_key: str | None = None
    subdir: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "originUri": self.origin_uri,
            "publisherId": self.publisher_id,
            "publisherHandle": self.publisher_handle,
            "commitSha": self.commit_sha,
            "artifactDigest": self.artifact_digest,
            "catalogKey": self.catalog_key,
            "subdir": self.subdir,
        }


@dataclass(frozen=True)
class HarnessCompatibility:
    harness_api: str = "1"
    platforms: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {"harnessApi": self.harness_api, "platforms": list(self.platforms)}


@dataclass(frozen=True)
class SkillDependency:
    skill_name: str
    range: str = "*"
    optional: bool = False

    def to_wire(self) -> dict[str, Any]:
        return {"skillName": self.skill_name, "range": self.range, "optional": self.optional}


@dataclass(frozen=True)
class Signature:
    alg: str
    signature: str
    key_id: str

    def to_wire(self) -> dict[str, Any]:
        return {"alg": self.alg, "signature": self.signature, "keyId": self.key_id}


@dataclass(frozen=True)
class SkillManifestV1:
    name: str
    version: str
    description: str
    entry: str
    source: SkillSource
    files: tuple[SkillFile, ...]
    requested_capabilities: tuple[str, ...] = ()
    dependencies: tuple[SkillDependency, ...] = ()
    compatibility: HarnessCompatibility = HarnessCompatibility()
    license: str | None = None
    publisher_id: str | None = None
    publisher_handle: str | None = None
    signature: Signature | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def to_wire(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "entry": self.entry,
            "source": self.source.to_wire(),
            "requestedCapabilities": list(self.requested_capabilities),
            "dependencies": [d.to_wire() for d in self.dependencies],
            "compatibility": self.compatibility.to_wire(),
            "files": [f.to_wire() for f in self.files],
            "license": self.license,
            "publisherId": self.publisher_id,
            "publisherHandle": self.publisher_handle,
            "signature": self.signature.to_wire() if self.signature else None,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class SecurityFinding:
    rule_id: str
    severity: Severity
    path: str
    line: int | None
    column: int | None
    excerpt: str
    rationale: str
    recommendation: str
    engine_version: str
    heuristic: bool = True

    def to_wire(self) -> dict[str, Any]:
        return {
            "ruleId": self.rule_id,
            "severity": self.severity.value,
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "excerpt": self.excerpt,
            "rationale": self.rationale,
            "recommendation": self.recommendation,
            "engineVersion": self.engine_version,
            "heuristic": self.heuristic,
        }


@dataclass(frozen=True)
class ScanResult:
    findings: tuple[SecurityFinding, ...]
    engine_version: str
    digest: str
    blocking: bool

    def to_wire(self) -> dict[str, Any]:
        return {
            "engineVersion": self.engine_version,
            "digest": self.digest,
            "blocking": self.blocking,
            "findings": [f.to_wire() for f in self.findings],
            "limitations": "A scanner PASS is not a proof of safety; review the findings and source.",
        }


@dataclass(frozen=True)
class SkillRelease:
    id: str
    manifest: SkillManifestV1
    content_digest: str
    state: ReleaseState
    trust_level: TrustLevel
    scan_digest: str
    created_at: str
    validated_at: str | None = None
    publisher_verified: bool = False
    signature_status: str = "unverified"
    source_commit_sha: str | None = None
    artifact_digest: str | None = None
    tenant_id: str | None = None
    owner_user_id: str | None = None
    scan: ScanResult | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "manifest": self.manifest.to_wire(),
            "contentDigest": self.content_digest,
            "state": self.state.value,
            "trustLevel": self.trust_level.value,
            "scanDigest": self.scan_digest,
            "createdAt": self.created_at,
            "validatedAt": self.validated_at,
            "publisherVerified": self.publisher_verified,
            "signatureStatus": self.signature_status,
            "sourceCommitSha": self.source_commit_sha,
            "artifactDigest": self.artifact_digest,
            "scan": self.scan.to_wire() if self.scan is not None else None,
        }


@dataclass(frozen=True)
class SkillInstallation:
    id: str
    tenant_id: str
    owner_user_id: str
    skill_name: str
    release_id: str
    desired: DesiredState
    effective: InstallationState
    record_version: int
    activation_generation: int
    policy_revision: str
    approved_release_digest: str | None
    created_at: str
    updated_at: str
    revoked_at: str | None = None
    revocation_epoch: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "skillName": self.skill_name,
            "releaseId": self.release_id,
            "desired": self.desired.value,
            "effective": self.effective.value,
            "recordVersion": self.record_version,
            "activationGeneration": self.activation_generation,
            "revocationEpoch": self.revocation_epoch,
            "policyRevision": self.policy_revision,
            "approvedReleaseDigest": self.approved_release_digest,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "revokedAt": self.revoked_at,
        }


@dataclass(frozen=True)
class SkillApproval:
    id: str
    tenant_id: str
    owner_user_id: str
    actor_user_id: str
    action: str
    release_id: str
    content_digest: str
    publisher_id: str | None
    capability_digest: str
    policy_revision: str
    scan_digest: str
    request_id: str
    expected_installation_revision: int
    expires_at: str
    created_at: str
    installation_id: str | None = None
    target_release_id: str | None = None
    enable_after: bool = False
    reenable: bool = False


@dataclass(frozen=True)
class OperationReceipt:
    id: str
    tenant_id: str
    actor_user_id: str
    request_id: str
    payload_sha256: str
    action: str
    installation_id: str | None
    release_id: str | None
    result: dict[str, Any]
    created_at: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "requestId": self.request_id,
            "action": self.action,
            "installationId": self.installation_id,
            "releaseId": self.release_id,
            "result": self.result,
            "createdAt": self.created_at,
        }


@dataclass(frozen=True)
class DependencyPin:
    """Exact dependency installation snapshot captured when a Run first loads the Skill surface."""

    skill_name: str
    release_id: str
    content_digest: str
    installation_id: str
    activation_generation: int
    version: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "skillName": self.skill_name,
            "releaseId": self.release_id,
            "contentDigest": self.content_digest,
            "installationId": self.installation_id,
            "activationGeneration": self.activation_generation,
            "version": self.version,
        }


@dataclass(frozen=True)
class RunSkillPin:
    tenant_id: str
    run_id: str
    installation_id: str
    release_id: str
    content_digest: str
    activation_generation: int
    policy_revision: str
    capability_digest: str
    dependency_digest: str
    skill_surface_digest: str
    created_at: str
    dependencies: tuple[DependencyPin, ...] = ()
    dependency_snapshot_digest: str = ""
    revocation_epoch: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "installationId": self.installation_id,
            "releaseId": self.release_id,
            "contentDigest": self.content_digest,
            "activationGeneration": self.activation_generation,
            "revocationEpoch": self.revocation_epoch,
            "policyRevision": self.policy_revision,
            "capabilityDigest": self.capability_digest,
            "dependencyDigest": self.dependency_digest,
            "dependencySnapshotDigest": self.dependency_snapshot_digest,
            "dependencies": [dependency.to_wire() for dependency in self.dependencies],
            "skillSurfaceDigest": self.skill_surface_digest,
        }


@dataclass(frozen=True)
class CatalogEntry:
    skill_id: str
    name: str
    description: str
    version: str
    compatibility: str
    installation_id: str | None = None
    enabled: bool = False

    def to_wire(self) -> dict[str, Any]:
        return {
            "skillId": self.skill_id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "compatibility": self.compatibility,
            "installationId": self.installation_id,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class SkillText:
    skill_id: str
    path: str
    text: str
    content_digest: str
    next_offset: int | None
    truncated: bool
