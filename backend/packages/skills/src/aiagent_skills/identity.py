"""Namespaced Skill identity and built-in protection.

A name alone is never an identity. The immutable identity binds source, publisher, slug, exact version,
commit/artifact digest and owner scope. Names are ASCII-only and casefold-stable to stop Unicode confusables
and case collisions; built-in names are reserved and can never be claimed by an imported bundle.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .contracts import SkillSource, SourceKind, TrustLevel
from .digest import sha256_bytes
from .errors import ManifestInvalid, NameCollision, ProtectedBuiltin

NAME_RE = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z")
PUBLISHER_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}\Z")

# Bundled, human-reviewed product skills. Imported bundles can never take these names.
PROTECTED_BUILTIN_NAMES = frozenset({"nihongo", "price-research"})


@dataclass(frozen=True)
class SkillIdentity:
    source: SourceKind
    slug: str
    exact_version: str
    publisher_id: str | None
    publisher_handle: str | None
    commit_sha: str | None
    artifact_digest: str | None
    owner_scope: str

    def key(self) -> str:
        return "|".join(
            [
                self.source.value,
                self.publisher_id or self.publisher_handle or "-",
                self.slug,
                self.exact_version,
                self.commit_sha or "-",
                self.artifact_digest or "-",
                self.owner_scope,
            ]
        )

    def digest(self) -> str:
        return sha256_bytes(self.key().encode("utf-8"))


def validate_skill_name(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestInvalid("skill name is required")
    if len(value) > 64:
        raise ManifestInvalid("skill name exceeds 64 characters")
    if any(ord(ch) > 127 for ch in value):
        raise ManifestInvalid("skill name must be ASCII")
    if unicodedata.normalize("NFKC", value) != value or value.casefold() != value:
        raise ManifestInvalid("skill name is not normalization/case stable")
    if not NAME_RE.match(value):
        raise ManifestInvalid("skill name must match ^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
    if "--" in value:
        raise ManifestInvalid("consecutive hyphens are not allowed in a skill name")
    return value


def validate_publisher_handle(value: object) -> str:
    if not isinstance(value, str) or not PUBLISHER_RE.match(value.casefold()):
        raise ManifestInvalid("publisher handle is invalid")
    return value.casefold()


def assert_not_protected_builtin(value: str) -> None:
    if value.casefold() in PROTECTED_BUILTIN_NAMES:
        raise ProtectedBuiltin(f"built-in skill name is reserved: {value}", name=value)


def assert_no_collision(new: SkillIdentity, existing: list[SkillIdentity]) -> None:
    for old in existing:
        if old.slug.casefold() == new.slug.casefold():
            if old.owner_scope == new.owner_scope:
                raise NameCollision("skill name already exists in this scope", name=new.slug)
            if old.publisher_id and new.publisher_id and old.publisher_id != new.publisher_id:
                continue
        if old.key() == new.key():
            raise NameCollision("identical skill identity already exists", name=new.slug)


def publisher_matches(expected: str | None, observed: str | None) -> bool:
    if not expected or not observed:
        return False
    return expected.casefold() == observed.casefold()


def trust_for_source(source: SourceKind) -> TrustLevel:
    if source is SourceKind.BUILTIN:
        return TrustLevel.BUILT_IN
    return TrustLevel.THIRD_PARTY_UNTRUSTED


def lineage_key(source: SkillSource, owner_scope: str) -> str:
    """Stable provenance identity, deliberately excluding version/commit/artifact digest.

    An update may move a Skill to a new exact release, but it may not silently switch publisher, repository or
    subdirectory. Uploads have no external publisher identity, so their lineage is the owner scope.
    """
    if source.kind in (SourceKind.GITHUB, SourceKind.CLAWHUB):
        if not source.publisher_id or not source.catalog_key:
            raise ManifestInvalid("external Skill source lacks a stable publisher/catalog identity")
        return f"{source.kind.value}:{source.publisher_id.casefold()}:{source.catalog_key.casefold()}"
    return f"{source.kind.value}:{owner_scope}"


def assert_same_lineage(current: SkillSource, target: SkillSource, owner_scope: str) -> None:
    if lineage_key(current, owner_scope) != lineage_key(target, owner_scope):
        raise ManifestInvalid("update or rollback crosses the Skill's stable source lineage")
