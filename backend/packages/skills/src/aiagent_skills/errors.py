"""Fail-closed error taxonomy. Codes are stable wire values; messages never contain bundle content."""

from __future__ import annotations


class SkillError(Exception):
    """Base error with a stable machine code and an HTTP status suggestion."""

    code = "SKILL_ERROR"
    http_status = 400

    def __init__(self, detail: str = "", **context: object) -> None:
        super().__init__(detail or self.code)
        self.detail = detail
        self.context = context

    def to_wire(self) -> dict[str, object]:
        return {"error": self.code, "detail": self.detail}


class ManifestInvalid(SkillError):
    code = "MANIFEST_INVALID"


class UnsupportedSchema(SkillError):
    code = "UNSUPPORTED_SCHEMA_VERSION"


class FrontmatterInvalid(SkillError):
    code = "FRONTMATTER_INVALID"


class ArchiveRejected(SkillError):
    code = "ARCHIVE_REJECTED"


class BundleInvalid(SkillError):
    code = "BUNDLE_INVALID"


class DigestMismatch(SkillError):
    code = "DIGEST_MISMATCH"


class NameCollision(SkillError):
    code = "SKILL_NAME_COLLISION"
    http_status = 409


class ProtectedBuiltin(SkillError):
    code = "PROTECTED_BUILTIN_SKILL"
    http_status = 409


class OwnerNotVerifiable(SkillError):
    code = "OWNER_NOT_VERIFIABLE"
    http_status = 409


class DependencyError(SkillError):
    code = "SKILL_DEPENDENCY_ERROR"


class UnsupportedCapability(SkillError):
    code = "UNSUPPORTED_CAPABILITY"
    http_status = 409


class LifecycleConflict(SkillError):
    code = "LIFECYCLE_CONFLICT"
    http_status = 409


class RevisionConflict(SkillError):
    code = "STALE_REVISION"
    http_status = 412


class ApprovalInvalid(SkillError):
    code = "APPROVAL_INVALID"
    http_status = 403


class ApprovalExpired(ApprovalInvalid):
    code = "APPROVAL_EXPIRED"


class IdempotencyConflict(SkillError):
    code = "IDEMPOTENCY_KEY_REUSED"
    http_status = 409


class ReleaseRevoked(SkillError):
    code = "SKILL_RELEASE_REVOKED"
    http_status = 409


class SkillNotAvailable(SkillError):
    code = "SKILL_NOT_AVAILABLE"
    http_status = 404


class ScopeViolation(SkillError):
    code = "SKILL_SCOPE_VIOLATION"
    http_status = 404


class BudgetExceeded(SkillError):
    code = "SKILL_BUDGET_EXCEEDED"
    http_status = 409


class DependencyUnavailable(SkillError):
    code = "DEPENDENCY_UNAVAILABLE"
    http_status = 503


class QuarantineUnavailable(SkillError):
    code = "QUARANTINE_UNAVAILABLE"
    http_status = 503


class SourceUnavailable(SkillError):
    code = "SOURCE_UNAVAILABLE"
    http_status = 503


class ScriptExecutionUnsupported(SkillError):
    code = "SCRIPT_EXECUTION_UNSUPPORTED"
    http_status = 409


class InstallationConflict(LifecycleConflict):
    code = "INSTALLATION_CONFLICT"


# Readable aliases used by newer modules. They are the same exception types, not a second taxonomy.
StaleRevision = RevisionConflict
SkillNotFound = SkillNotAvailable
SkillScopeViolation = ScopeViolation
IdempotencyKeyReused = IdempotencyConflict
