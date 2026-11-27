"""Turn a validated bundle into an immutable, reviewable release."""

from __future__ import annotations

from .bundle import ValidatedBundle
from .contracts import (
    ReleaseState,
    ScanResult,
    SkillRelease,
    SkillSource,
    SourceKind,
    TrustLevel,
)
from .digest import content_digest, verify_file_list
from .errors import ProtectedBuiltin, ReleaseRevoked, SkillError
from .frontmatter import parse_skill_markdown
from .identity import assert_not_protected_builtin, trust_for_source
from .manifest import build_manifest
from .policy import validate_requested
from .scanner import scan_files


def build_release(
    *,
    release_id: str,
    tenant_id: str,
    owner_user_id: str,
    source: SkillSource,
    bundle: ValidatedBundle,
    now: str,
) -> tuple[SkillRelease, ScanResult]:
    if source.kind is not SourceKind.BUILTIN:
        marker = parse_skill_markdown(bundle.skill_md())
        assert_not_protected_builtin(str(marker.data.get("name", "")))
    frontmatter = parse_skill_markdown(bundle.skill_md())
    manifest = build_manifest(frontmatter, bundle.files, source)
    validate_requested(manifest.requested_capabilities)
    verify_file_list(manifest.files, bundle.files)
    scan = scan_files(bundle.files, bundle.entries)
    content = content_digest(manifest, bundle.files.items())
    state = ReleaseState.STAGED_FOR_REVIEW if not scan.blocking and scan.complete else ReleaseState.REJECTED
    release = SkillRelease(
        id=release_id,
        manifest=manifest,
        content_digest=content,
        state=state,
        trust_level=trust_for_source(source.kind),
        scan_digest=scan.digest,
        created_at=now,
        validated_at=now,
        publisher_verified=bool(source.publisher_id and source.publisher_handle),
        signature_status="unverified" if manifest.signature else "absent",
        source_commit_sha=source.commit_sha,
        artifact_digest=source.artifact_digest or bundle.digest,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        scan=scan,
    )
    return release, scan


def assert_release_installable(release: SkillRelease, scan: ScanResult) -> None:
    if release.state is ReleaseState.REVOKED:
        raise ReleaseRevoked("release is revoked")
    if release.state is not ReleaseState.STAGED_FOR_REVIEW:
        raise SkillError("release is not awaiting review")
    if scan.blocking or not scan.complete or scan.digest != release.scan_digest:
        raise SkillError("release scan is not passing, complete, or unchanged")
    if not release.publisher_verified and release.manifest.source.kind is SourceKind.CLAWHUB:
        raise SkillError("ClawHub publisher identity was not verified")


def assert_manifest_consistent(release: SkillRelease, files: dict[str, bytes]) -> None:
    verify_file_list(release.manifest.files, files)
    expected = content_digest(release.manifest, files.items())
    if expected != release.content_digest:
        raise SkillError("release content digest does not match its files")


def assert_immutable(existing: SkillRelease, replacement: SkillRelease) -> None:
    if existing.id == replacement.id:
        if existing.content_digest != replacement.content_digest:
            raise SkillError("a release is immutable; publish a new release instead")
        return
    if existing.manifest.name == replacement.manifest.name and existing.content_digest == replacement.content_digest:
        raise SkillError("identical content already has a release")


def trust_level_for_builtin() -> TrustLevel:
    return TrustLevel.BUILT_IN


__all__ = [
    "ProtectedBuiltin",
    "assert_immutable",
    "assert_manifest_consistent",
    "assert_release_installable",
    "build_release",
]
