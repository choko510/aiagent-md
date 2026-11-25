"""Run-scoped pins, lazy text disclosure and immutable read backends."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from .contracts import (
    CatalogEntry,
    InstallationState,
    RunSkillPin,
    SkillInstallation,
    SkillRelease,
    SkillText,
)
from .digest import content_digest, sha256_bytes
from .errors import ReleaseRevoked, SkillNotFound, SkillScopeViolation

MAX_DISCLOSURE_BYTES = 16 * 1024
MAX_BODY_BYTES = 256 * 1024


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
) -> RunSkillPin:
    if installation.tenant_id != tenant_id or release.tenant_id not in (None, tenant_id):
        raise SkillScopeViolation("installation or release belongs to another tenant")
    if installation.release_id != release.id:
        raise SkillScopeViolation("installation does not point at the release being pinned")
    return RunSkillPin(
        tenant_id=tenant_id,
        run_id=run_id,
        installation_id=installation.id,
        release_id=release.id,
        content_digest=release.content_digest,
        activation_generation=installation.activation_generation,
        policy_revision=installation.policy_revision,
        capability_digest=capability_digest,
        dependency_digest=dependency_digest,
        skill_surface_digest=skill_surface_digest,
        created_at=now,
    )


def verify_run_pin(
    pin: RunSkillPin,
    *,
    installation: SkillInstallation,
    release: SkillRelease,
    revoked: bool = False,
) -> None:
    if revoked or release.state.value == "REVOKED":
        raise ReleaseRevoked("pinned Skill release was revoked")
    checks = (
        pin.tenant_id == installation.tenant_id
        and pin.installation_id == installation.id
        and pin.release_id == installation.release_id
        and pin.release_id == release.id
        and pin.content_digest == release.content_digest
        and pin.activation_generation == installation.activation_generation
        and pin.policy_revision == installation.policy_revision
    )
    if not checks:
        raise SkillScopeViolation("run pin no longer matches the installation")
    if installation.effective is not InstallationState.ENABLED:
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


def _decode_prefix(data: bytes, limit: int) -> tuple[str, int]:
    if len(data) <= limit:
        return data.decode("utf-8"), len(data)
    cut = limit
    while cut > max(0, limit - 4):
        try:
            return data[:cut].decode("utf-8"), cut
        except UnicodeDecodeError:
            cut -= 1
    return data[:limit].decode("utf-8", errors="replace"), limit


def disclose_text(
    *,
    skill_id: str,
    path: str,
    body: bytes,
    content_digest_value: str,
    offset: int = 0,
    max_bytes: int = MAX_DISCLOSURE_BYTES,
) -> SkillText:
    if offset < 0 or offset > len(body) or max_bytes <= 0:
        raise SkillScopeViolation("invalid disclosure offset or budget")
    if offset == 0 and len(body) <= max_bytes:
        text, _ = _decode_prefix(body, max_bytes)
        return SkillText(skill_id, path, text, content_digest_value, None, False)
    chunk = body[offset : offset + max_bytes]
    text, consumed = _decode_prefix(chunk, len(chunk))
    next_offset = offset + consumed
    truncated = next_offset < len(body)
    return SkillText(
        skill_id=skill_id,
        path=path,
        text=text,
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
            matches.append(
                CatalogEntry(
                    skill_id=release.id,
                    name=name,
                    description=release.manifest.description,
                    version=release.manifest.version,
                    compatibility=release.manifest.compatibility.harness_api,
                    installation_id=installation.id,
                    enabled=True,
                )
            )
            if len(matches) >= min(limit, 50):
                break
        return tuple(matches)

    def get(self, name: str) -> tuple[SkillInstallation, SkillRelease]:
        try:
            return self._entries[name]
        except KeyError:
            raise SkillNotFound("Skill is not enabled in this catalog") from None

    def surface_digest(self) -> str:
        rows = [f"{name}|{release.id}|{release.content_digest}" for name, (_, release) in sorted(self._entries.items())]
        return sha256_bytes("\n".join(rows).encode("utf-8"))

    def all_entries(self) -> tuple[CatalogEntry, ...]:
        return self.search("", limit=50)


def assert_body_digest(release: SkillRelease, files: dict[str, bytes]) -> None:
    if content_digest(release.manifest, files.items()) != release.content_digest:
        raise SkillScopeViolation("Skill body no longer matches the pinned content digest")


def iter_enabled(
    rows: Iterable[tuple[SkillInstallation, SkillRelease]],
) -> tuple[tuple[SkillInstallation, SkillRelease], ...]:
    return tuple((i, r) for i, r in rows if i.effective is InstallationState.ENABLED)
