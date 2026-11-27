"""Strict Agent Skills frontmatter normalization and internal manifest validation."""

from __future__ import annotations

import re
from typing import Any

from .contracts import (
    HarnessCompatibility,
    Signature,
    SkillDependency,
    SkillFile,
    SkillManifestV1,
    SkillSource,
    SourceKind,
)
from .digest import bundle_digest, content_digest, manifest_digest, sha256_bytes
from .errors import ManifestInvalid, UnsupportedSchema
from .frontmatter import Frontmatter
from .identity import validate_publisher_handle, validate_skill_name

SEMVER_RE = re.compile(
    r"\A(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z"
)

MAX_DESCRIPTION = 1024
MAX_CAPABILITIES = 32
MAX_DEPENDENCIES = 16

_MEDIA_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".ts": "text/typescript",
    ".sh": "text/x-shellscript",
    ".html": "text/html",
    ".css": "text/css",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
}


def media_type(path: str) -> str:
    lower = path.casefold()
    for suffix, media in _MEDIA_TYPES.items():
        if lower.endswith(suffix):
            return media
    return "application/octet-stream"


def validate_semver(value: object, *, what: str = "version") -> str:
    if not isinstance(value, str) or not SEMVER_RE.match(value):
        raise ManifestInvalid(f"{what} must be canonical SemVer")
    return value


def _string(value: object, what: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestInvalid(f"{what} must be a non-empty string")
    if len(value) > maximum:
        raise ManifestInvalid(f"{what} exceeds {maximum} characters")
    if any(ord(ch) < 32 and ch not in "\t\n" for ch in value):
        raise ManifestInvalid(f"{what} contains control characters")
    return value.strip()


def parse_allowed_tools(value: object) -> tuple[str, ...]:
    """allowed-tools is a capability REQUEST; it is never an authorization grant."""
    if value is None:
        return ()
    raw: list[str] = []
    if isinstance(value, str):
        raw = [part for part in value.replace(",", " ").split() if part]
    elif isinstance(value, list):
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ManifestInvalid("allowed-tools entries must be non-empty strings")
            raw.extend(part for part in item.replace(",", " ").split() if part)
    else:
        raise ManifestInvalid("allowed-tools must be a string or a list of strings")
    if len(raw) > MAX_CAPABILITIES:
        raise ManifestInvalid(f"at most {MAX_CAPABILITIES} capabilities may be requested")
    out = []
    for item in raw:
        if len(item) > 128 or any(ord(ch) < 33 or ord(ch) > 126 for ch in item):
            raise ManifestInvalid("capability token contains invalid characters")
        out.append(item)
    return tuple(sorted(set(out)))


def _compatibility(value: object) -> HarnessCompatibility:
    if value is None:
        return HarnessCompatibility()
    if isinstance(value, str):
        return HarnessCompatibility(harness_api=_string(value, "compatibility", maximum=64))
    if isinstance(value, dict):
        unknown = set(value) - {"harnessApi", "platforms", "harness_api"}
        if unknown:
            raise ManifestInvalid("unknown compatibility fields", fields=sorted(unknown))
        api = value.get("harnessApi", value.get("harness_api", "1"))
        platforms = value.get("platforms", [])
        if not isinstance(platforms, list) or not all(isinstance(p, str) for p in platforms):
            raise ManifestInvalid("compatibility.platforms must be a list of strings")
        return HarnessCompatibility(
            harness_api=_string(api, "compatibility.harnessApi", maximum=64),
            platforms=tuple(sorted({p.casefold() for p in platforms})),
        )
    raise ManifestInvalid("compatibility must be a string or a mapping")


def _dependencies(value: object) -> tuple[SkillDependency, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ManifestInvalid("dependencies must be a list")
    if len(value) > MAX_DEPENDENCIES:
        raise ManifestInvalid(f"at most {MAX_DEPENDENCIES} dependencies")
    out: list[SkillDependency] = []
    for item in value:
        if not isinstance(item, dict):
            raise ManifestInvalid("each dependency must be a mapping")
        unknown = set(item) - {"skillName", "name", "range", "optional"}
        if unknown:
            raise ManifestInvalid("unknown dependency fields", fields=sorted(unknown))
        name = validate_skill_name(item.get("skillName", item.get("name")))
        rng = item.get("range", "*")
        if not isinstance(rng, str) or not rng or len(rng) > 64:
            raise ManifestInvalid("dependency range must be a short string")
        optional = item.get("optional", False)
        if not isinstance(optional, bool):
            raise ManifestInvalid("dependency optional must be a boolean")
        out.append(SkillDependency(name, rng, optional))
    return tuple(sorted(set(out), key=lambda d: d.skill_name))


def build_manifest(
    frontmatter: Frontmatter,
    files: dict[str, bytes],
    source: SkillSource,
) -> SkillManifestV1:
    data = frontmatter.data
    if "SKILL.md" not in files:
        raise ManifestInvalid("SKILL.md is required at the bundle root")
    name = validate_skill_name(data.get("name"))
    description = _string(data.get("description"), "description", maximum=MAX_DESCRIPTION)
    license_value = data.get("license")
    license_text = _string(license_value, "license", maximum=200) if license_value is not None else None
    if "version" in data:
        version = validate_semver(data.get("version"))
        version_source = "frontmatter"
    else:
        # An external Skill without a version does not get an invented upstream SemVer. The
        # derived build identity is explicitly marked and binds the exact content digest.
        artifact = source.artifact_digest or sha256_bytes(b"".join(files[k] for k in sorted(files)))
        version = f"0.0.0+src.{artifact[:16]}"
        version_source = "derived-artifact-digest"
    publisher_handle = None
    if source.publisher_handle:
        publisher_handle = validate_publisher_handle(source.publisher_handle)
    publisher_id = source.publisher_id
    if publisher_id is not None and (not isinstance(publisher_id, str) or len(publisher_id) > 128):
        raise ManifestInvalid("publisherId is invalid")
    signature = None
    raw_signature = data.get("signature")
    if raw_signature is not None:
        if not isinstance(raw_signature, dict):
            raise ManifestInvalid("signature must be a mapping")
        unknown = set(raw_signature) - {"alg", "signature", "keyId"}
        if unknown:
            raise ManifestInvalid("unknown signature fields", fields=sorted(unknown))
        signature = Signature(
            alg=_string(raw_signature.get("alg"), "signature.alg", maximum=32),
            signature=_string(raw_signature.get("signature"), "signature.signature", maximum=4096),
            key_id=_string(raw_signature.get("keyId"), "signature.keyId", maximum=128),
        )
    metadata = data.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ManifestInvalid("metadata must be a mapping")
    dependencies = _dependencies(data.get("dependencies") or (metadata or {}).get("dependencies"))
    recognized = {
        "name",
        "description",
        "license",
        "compatibility",
        "metadata",
        "allowed-tools",
        "allowedTools",
        "version",
        "dependencies",
        "signature",
    }
    unrecognized = sorted(set(data) - recognized)
    skill_files = tuple(
        SkillFile(path, sha256_bytes(payload), len(payload), media_type(path))
        for path, payload in sorted(files.items())
    )
    capabilities = parse_allowed_tools(data.get("allowed-tools", data.get("allowedTools")))
    provenance: dict[str, Any] = {
        "frontmatterDigest": frontmatter.digest,
        "versionSource": version_source,
        "unrecognizedFrontmatterFields": unrecognized,
        "metadata": metadata or {},
        "bundleDigest": bundle_digest(files.items()),
    }
    return SkillManifestV1(
        name=name,
        version=version,
        description=description,
        entry="SKILL.md",
        source=source,
        files=skill_files,
        requested_capabilities=capabilities,
        dependencies=dependencies,
        compatibility=_compatibility(data.get("compatibility")),
        license=license_text,
        publisher_id=publisher_id,
        publisher_handle=publisher_handle,
        signature=signature,
        provenance=provenance,
    )


def manifest_and_content_digests(manifest: SkillManifestV1, files: dict[str, bytes]) -> tuple[str, str]:
    return manifest_digest(manifest), content_digest(manifest, files.items())


_CANONICAL_KEYS = frozenset(
    {
        "schemaVersion",
        "name",
        "version",
        "description",
        "entry",
        "source",
        "requestedCapabilities",
        "dependencies",
        "compatibility",
        "files",
        "license",
        "publisherId",
        "publisherHandle",
        "signature",
        "provenance",
    }
)


def manifest_from_wire(data: object) -> SkillManifestV1:
    """Strict reader for persisted manifests. Unknown fields fail closed."""
    if not isinstance(data, dict):
        raise ManifestInvalid("manifest must be an object")
    unknown = set(data) - _CANONICAL_KEYS
    if unknown:
        raise ManifestInvalid("unknown manifest fields", fields=sorted(unknown))
    schema = data.get("schemaVersion")
    if schema != 1:
        raise UnsupportedSchema(f"unsupported manifest schemaVersion: {schema!r}")
    source_data = data.get("source")
    if not isinstance(source_data, dict):
        raise ManifestInvalid("source is required")
    try:
        source = SkillSource(
            kind=SourceKind(str(source_data["kind"])),
            origin_uri=source_data.get("originUri"),
            publisher_id=source_data.get("publisherId"),
            publisher_handle=source_data.get("publisherHandle"),
            commit_sha=source_data.get("commitSha"),
            artifact_digest=source_data.get("artifactDigest"),
            catalog_key=source_data.get("catalogKey"),
            subdir=source_data.get("subdir"),
        )
    except (KeyError, ValueError) as exc:
        raise ManifestInvalid("invalid source", problem=str(exc)[:100]) from None
    files_data = data.get("files")
    if not isinstance(files_data, list) or not files_data:
        raise ManifestInvalid("files must be a non-empty list")
    files = []
    for item in files_data:
        if not isinstance(item, dict):
            raise ManifestInvalid("file entry must be an object")
        files.append(
            SkillFile(
                relative_path=str(item["relativePath"]),
                sha256=str(item["sha256"]),
                byte_size=int(item["byteSize"]),
                media_type=str(item["mediaType"]),
            )
        )
    compatibility = data.get("compatibility") or {}
    signature_data = data.get("signature")
    manifest = SkillManifestV1(
        name=validate_skill_name(data.get("name")),
        version=validate_semver(data.get("version")),
        description=_string(data.get("description"), "description", maximum=MAX_DESCRIPTION),
        entry=str(data.get("entry")),
        source=source,
        files=tuple(files),
        requested_capabilities=tuple(str(x) for x in data.get("requestedCapabilities", [])),
        dependencies=_dependencies(data.get("dependencies")),
        compatibility=_compatibility(compatibility),
        license=data.get("license"),
        publisher_id=data.get("publisherId"),
        publisher_handle=data.get("publisherHandle"),
        signature=Signature(**signature_data) if isinstance(signature_data, dict) else None,
        provenance=dict(data.get("provenance") or {}),
    )
    if manifest.entry != "SKILL.md":
        raise ManifestInvalid("manifest entry must be SKILL.md")
    return manifest
