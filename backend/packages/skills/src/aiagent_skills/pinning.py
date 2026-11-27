"""Run-scoped pins, lazy text disclosure and immutable read backends."""

from __future__ import annotations

import hmac
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from aiagent_domain.canonical import canonical_json

from .contracts import (
    CatalogEntry,
    DependencyPin,
    InstallationState,
    RunSkillPin,
    SkillInstallation,
    SkillRelease,
    SkillText,
)
from .digest import content_digest, dependency_digest, sha256_bytes
from .errors import ReleaseRevoked, SkillNotFound, SkillScopeViolation

MAX_DISCLOSURE_BYTES = 16 * 1024
MAX_BODY_BYTES = 256 * 1024


def dependency_snapshot_digest(dependencies: tuple[DependencyPin, ...]) -> str:
    """Canonical digest of the complete, ordered dependency closure."""
    return sha256_bytes(canonical_json([dependency.to_wire() for dependency in dependencies]).encode("utf-8"))


def _ordered_dependencies(dependencies: tuple[DependencyPin, ...]) -> tuple[DependencyPin, ...]:
    ordered = tuple(sorted(dependencies, key=lambda dependency: dependency.skill_name))
    names = [dependency.skill_name for dependency in ordered]
    if len(names) != len(set(names)):
        raise SkillScopeViolation("dependency closure contains duplicate Skill names")
    return ordered


def create_run_pin(
    *,
    tenant_id: str,
    run_id: str,
    installation: SkillInstallation,
    release: SkillRelease,
    capability_digest: str,
    dependency_digest: str,
    skill_surface_digest: str,
    now: str,
    dependencies: tuple[DependencyPin, ...] = (),
) -> RunSkillPin:
    if installation.tenant_id != tenant_id or release.tenant_id not in (None, tenant_id):
        raise SkillScopeViolation("installation or release belongs to another tenant")
    if installation.release_id != release.id:
        raise SkillScopeViolation("installation does not point at the release being pinned")
    ordered = _ordered_dependencies(dependencies)
    return RunSkillPin(
        tenant_id=tenant_id,
        run_id=run_id,
        installation_id=installation.id,
        release_id=release.id,
        content_digest=release.content_digest,
        activation_generation=installation.activation_generation,
        revocation_epoch=installation.revocation_epoch,
        policy_revision=installation.policy_revision,
        capability_digest=capability_digest,
        dependency_digest=dependency_digest,
        skill_surface_digest=skill_surface_digest,
        created_at=now,
        dependencies=ordered,
        dependency_snapshot_digest=dependency_snapshot_digest(ordered),
    )


def verify_run_pin(
    pin: RunSkillPin,
    *,
    installation: SkillInstallation,
    release: SkillRelease,
    revoked: bool = False,
    snapshot: bool = False,
) -> None:
    if revoked or release.state.value == "REVOKED":
        raise ReleaseRevoked("pinned Skill release was revoked")
    ordered = _ordered_dependencies(pin.dependencies)
    if ordered != pin.dependencies:
        raise SkillScopeViolation("pinned dependency closure is not canonical")
    if dependency_digest({item.skill_name: item.version for item in pin.dependencies}) != pin.dependency_digest:
        raise SkillScopeViolation("pinned dependency digest does not match the snapshot")
    if not hmac.compare_digest(pin.dependency_snapshot_digest, dependency_snapshot_digest(pin.dependencies)):
        raise SkillScopeViolation("pinned dependency snapshot digest does not match the snapshot")
    checks = (
        pin.tenant_id == installation.tenant_id
        and pin.installation_id == installation.id
        and pin.release_id == release.id
        and pin.content_digest == release.content_digest
        and pin.revocation_epoch == installation.revocation_epoch
    )
    if not snapshot:
        checks = checks and (
            pin.release_id == installation.release_id
            and pin.activation_generation == installation.activation_generation
            and pin.policy_revision == installation.policy_revision
        )
    if not checks:
        raise SkillScopeViolation("run pin no longer matches the installation")
    if installation.effective is InstallationState.REVOKED:
        raise ReleaseRevoked("installation is revoked")
    if not snapshot and installation.effective is not InstallationState.ENABLED:
        raise SkillScopeViolation("installation is not enabled")


class SkillReadBackend(Protocol):
    def list_files(self) -> tuple[str, ...]: ...
    def read_bytes(self, path: str) -> bytes: ...


class ImmutableSkillBackend:
    """Serves exactly one already-digest-verified release; it has no host filesystem access."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = dict(files)

    def list_files(self) -> tuple[str, ...]:
        return tuple(sorted(self._files))

    def read_bytes(self, path: str) -> bytes:
        if path not in self._files:
            raise SkillNotFound("Skill file does not exist")
        return self._files[path]

    def read_text(self, path: str, *, max_bytes: int = MAX_BODY_BYTES) -> str:
        data = self.read_bytes(path)
        if len(data) > max_bytes:
            raise SkillScopeViolation("Skill file exceeds the disclosure budget")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise SkillScopeViolation("Skill file is not valid UTF-8") from None


@dataclass(frozen=True)
class DisclosureSlice:
    text: str
    next_offset: int | None
    truncated: bool


def _validated_text(body: bytes) -> str:
    if len(body) > MAX_BODY_BYTES:
        raise SkillScopeViolation("Skill body exceeds the maximum disclosure body size")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        raise SkillScopeViolation("Skill body is not valid UTF-8") from None


def disclose_text(
    *,
    skill_id: str,
    path: str,
    body: bytes,
    content_digest_value: str,
    offset: int = 0,
    max_bytes: int = MAX_DISCLOSURE_BYTES,
) -> SkillText:
    if offset < 0 or offset > len(body):
        raise SkillScopeViolation("invalid disclosure offset")
    if not 0 < max_bytes <= MAX_DISCLOSURE_BYTES:
        raise SkillScopeViolation("disclosure budget must be between 1 byte and 16 KiB")
    _validated_text(body)
    try:
        # A cursor inside a multi-byte code point is not a valid UTF-8 boundary.
        body[:offset].decode("utf-8")
    except UnicodeDecodeError:
        raise SkillScopeViolation("disclosure offset is not a UTF-8 boundary") from None
    if offset == len(body):
        return SkillText(skill_id, path, "", content_digest_value, None, False)
    remaining = body[offset:]
    if len(remaining) <= max_bytes:
        return SkillText(skill_id, path, remaining.decode("utf-8"), content_digest_value, None, False)
    cut = max_bytes
    while cut > 0:
        try:
            chunk = remaining[:cut].decode("utf-8")
            break
        except UnicodeDecodeError:
            # The complete body already decoded, so the only possible error here is a truncated code point.
            cut -= 1
    else:
        raise SkillScopeViolation("disclosure budget cannot fit one UTF-8 character")
    next_offset = offset + cut
    truncated = next_offset < len(body)
    return SkillText(
        skill_id=skill_id,
        path=path,
        text=chunk,
        content_digest=content_digest_value,
        next_offset=next_offset if truncated else None,
        truncated=truncated,
    )


def normalize_query(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    if len(normalized) > 128:
        raise SkillScopeViolation("search query is too long")
    return normalized


class EnabledSkillCatalog:
    """Search/describe only enabled, pinned releases. Skill bodies stay out of the prompt and out of search."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[SkillInstallation, SkillRelease]] = {}

    def add(self, installation: SkillInstallation, release: SkillRelease) -> None:
        if installation.tenant_id != release.tenant_id and release.tenant_id is not None:
            raise SkillScopeViolation("release and installation tenant mismatch")
        if installation.effective is not InstallationState.ENABLED:
            raise SkillScopeViolation("only enabled installations enter the catalog")
        if installation.release_id != release.id:
            raise SkillScopeViolation("installation does not point at the release")
        self._entries[release.manifest.name] = (installation, release)

    def remove(self, name: str) -> None:
        self._entries.pop(name, None)

    def search(self, query: str, *, limit: int = 10) -> tuple[CatalogEntry, ...]:
        token = normalize_query(query)
        matches = []
        for name, (installation, release) in sorted(self._entries.items()):
            haystack = f"{name} {release.manifest.description}".casefold()
            if token and token not in haystack:
                continue
            matches.append(self._entry(name, installation, release))
            if len(matches) >= min(limit, 50):
                break
        return tuple(matches)

    def get(self, name: str) -> tuple[SkillInstallation, SkillRelease]:
        try:
            return self._entries[name]
        except KeyError:
            raise SkillNotFound("Skill is not enabled in this catalog") from None

    def surface_digest(self) -> str:
        rows = [
            "|".join(
                (
                    name,
                    release.id,
                    release.content_digest,
                    installation.id,
                    str(installation.activation_generation),
                    installation.policy_revision,
                    str(release.manifest.version),
                    ",".join(
                        sorted(
                            f"{dependency.skill_name}:{dependency.range}"
                            for dependency in release.manifest.dependencies
                        )
                    ),
                )
            )
            for name, (installation, release) in sorted(self._entries.items())
        ]
        return sha256_bytes("\n".join(rows).encode("utf-8"))

    @staticmethod
    def _entry(name: str, installation: SkillInstallation, release: SkillRelease) -> CatalogEntry:
        return CatalogEntry(
            skill_id=release.id,
            name=name,
            description=release.manifest.description,
            version=release.manifest.version,
            compatibility=release.manifest.compatibility.harness_api,
            installation_id=installation.id,
            enabled=True,
        )

    def all_entries(self) -> tuple[CatalogEntry, ...]:
        return tuple(
            self._entry(name, installation, release) for name, (installation, release) in sorted(self._entries.items())
        )


def assert_body_digest(release: SkillRelease, files: dict[str, bytes]) -> None:
    if content_digest(release.manifest, files.items()) != release.content_digest:
        raise SkillScopeViolation("Skill body no longer matches the pinned content digest")


def iter_enabled(
    rows: Iterable[tuple[SkillInstallation, SkillRelease]],
) -> tuple[tuple[SkillInstallation, SkillRelease], ...]:
    return tuple((i, r) for i, r in rows if i.effective is InstallationState.ENABLED)
