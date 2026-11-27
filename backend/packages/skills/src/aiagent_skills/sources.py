"""Source adapters with an injected egress port and no implicit network access.

GitHub and ClawHub resolution binds an exact commit/artifact digest and a verified publisher identity. All bytes
are fetched through the caller-supplied EgressPort, which must be the existing Sentinel-evaluated transport. These
adapters never execute repository code, never install packages and never fall back to latest.
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .bundle import (
    BundleLimits,
    ValidatedBundle,
    _entry_kind,
    _validate_path_kinds,
    inspect_zip,
    normalize_member_path,
)
from .contracts import SkillSource, SourceKind
from .digest import sha256_bytes
from .errors import ArchiveRejected, ManifestInvalid, OwnerNotVerifiable, SourceUnavailable

GITHUB_LOCATOR_RE = re.compile(
    r"\A([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100})(?:@([A-Za-z0-9_./-]{1,100}))?(?:#([A-Za-z0-9_./-]{1,200}))?\Z"
)
COMMIT_RE = re.compile(r"\A[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
SEMVER_RE = re.compile(
    r"\A(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?\Z"
)


def _rewrite_github_archive(archive: bytes, subdir: str | None) -> bytes:
    """Strip GitHub's single zipball wrapper and optional subdirectory without executing anything."""
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as original:
            all_infos = original.infolist()
            if len(all_infos) > 4096:
                raise ArchiveRejected("GitHub archive has too many entries")
            limits = BundleLimits()
            declared_raw_total = 0
            raw_paths: list[tuple[str, str]] = []
            for info in all_infos:
                path = normalize_member_path(info.filename, limits)
                kind = _entry_kind(info)
                raw_paths.append((path, kind))
                if kind == "dir":
                    continue
                if info.flag_bits & 0x1:
                    raise ArchiveRejected("encrypted GitHub archive entries are not supported")
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ArchiveRejected("unsupported GitHub archive compression")
                if info.file_size > 0 and info.compress_size == 0:
                    raise ArchiveRejected("GitHub archive entry has an invalid compression ratio")
                if info.compress_size and info.file_size > info.compress_size * limits.max_compression_ratio:
                    raise ArchiveRejected("GitHub archive entry exceeds the compression ratio limit")
                declared_raw_total += info.file_size
                if declared_raw_total > 64 * 1024 * 1024:
                    raise ArchiveRejected("GitHub archive expands beyond the safe limit")
            _validate_path_kinds(raw_paths)
            infos = [info for info in all_infos if not info.is_dir()]
            if not infos:
                raise SourceUnavailable("GitHub archive is empty")
            roots = {info.filename.split("/", 1)[0] for info in infos}
            root = next(iter(roots)) if len(roots) == 1 else ""
            prefix = f"{root}/" if root else ""
            if subdir:
                prefix += subdir.strip("/") + "/"
            selected = [info for info in infos if info.filename.startswith(prefix)]
            if not selected:
                raise SourceUnavailable("GitHub archive does not contain the requested Skill subdirectory")
            _validate_path_kinds(
                [(normalize_member_path(info.filename, limits)[len(prefix) :], _entry_kind(info)) for info in selected]
            )
            buffer = io.BytesIO()
            total = 0
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as rewritten:
                for info in selected:
                    relative = info.filename[len(prefix) :]
                    parts = relative.split("/")
                    if not relative or relative.startswith("/") or any(part in ("", ".", "..") for part in parts):
                        raise ArchiveRejected("GitHub archive contains an unsafe path")
                    total += info.file_size
                    if info.file_size > 2 * 1024 * 1024 or total > 8 * 1024 * 1024:
                        raise ArchiveRejected("GitHub archive expands beyond the safe limit")
                    with original.open(info) as stream:
                        payload = stream.read(2 * 1024 * 1024 + 1)
                    if len(payload) > 2 * 1024 * 1024 or len(payload) != info.file_size:
                        raise ArchiveRejected("GitHub archive entry size mismatch")
                    rewritten.writestr(relative, payload)
            return buffer.getvalue()
    except zipfile.BadZipFile as exc:
        raise ArchiveRejected("GitHub archive is not a valid zip") from exc


class EgressPort(Protocol):
    """Implemented by the Sentinel-owned egress transport; adapters never open sockets directly."""

    def fetch_json(self, url: str, *, max_bytes: int) -> Mapping[str, object]: ...
    def fetch_bytes(self, url: str, *, max_bytes: int) -> bytes: ...


@dataclass(frozen=True)
class GitHubLocator:
    owner: str
    repo: str
    ref: str | None
    subdir: str | None


@dataclass(frozen=True)
class ResolvedSource:
    source: SkillSource
    archive: bytes
    publisher_verified: bool


def parse_github_locator(value: str) -> GitHubLocator:
    match = GITHUB_LOCATOR_RE.match(value.strip())
    if not match:
        raise ManifestInvalid("GitHub locator must be owner/repo[@ref][#subdir]")
    owner, repo, ref, subdir = match.groups()
    if subdir is not None:
        parts = subdir.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ManifestInvalid("GitHub subdir contains an unsafe segment")
    return GitHubLocator(owner, repo, ref, subdir)


class GitHubResolver:
    def __init__(self, egress: EgressPort, *, api_base: str = "https://api.github.com") -> None:
        self.egress = egress
        self.api_base = api_base.rstrip("/")

    def resolve(
        self,
        locator: GitHubLocator,
        *,
        expected_publisher_id: str | None,
        expected_publisher_handle: str | None,
        max_archive_bytes: int,
    ) -> ResolvedSource:
        repo_meta = self.egress.fetch_json(
            f"{self.api_base}/repos/{locator.owner}/{locator.repo}", max_bytes=256 * 1024
        )
        owner = repo_meta.get("owner")
        if not isinstance(owner, dict):
            raise OwnerNotVerifiable("GitHub repository owner is unavailable")
        owner_id = owner.get("id")
        owner_login = owner.get("login")
        if not isinstance(owner_id, int) or not isinstance(owner_login, str):
            raise OwnerNotVerifiable("GitHub repository owner identity is malformed")
        if expected_publisher_id is not None and str(owner_id) != expected_publisher_id:
            raise OwnerNotVerifiable("GitHub owner does not match the expected publisher")
        if expected_publisher_handle is not None and owner_login.casefold() != expected_publisher_handle.casefold():
            raise OwnerNotVerifiable("GitHub owner handle does not match the expected publisher")
        ref = locator.ref or "HEAD"
        commit_meta = self.egress.fetch_json(
            f"{self.api_base}/repos/{locator.owner}/{locator.repo}/commits/{ref}",
            max_bytes=512 * 1024,
        )
        commit_sha = commit_meta.get("sha")
        if not isinstance(commit_sha, str) or not COMMIT_RE.match(commit_sha):
            raise SourceUnavailable("GitHub did not return an exact commit SHA")
        if locator.ref and COMMIT_RE.match(locator.ref) and locator.ref != commit_sha:
            raise SourceUnavailable("GitHub commit does not match the pinned ref")
        archive = self.egress.fetch_bytes(
            f"{self.api_base}/repos/{locator.owner}/{locator.repo}/zipball/{commit_sha}",
            max_bytes=max_archive_bytes,
        )
        if len(archive) > max_archive_bytes:
            raise SourceUnavailable("GitHub archive exceeds the byte limit")
        archive = _rewrite_github_archive(archive, locator.subdir)
        source = SkillSource(
            kind=SourceKind.GITHUB,
            origin_uri=f"https://github.com/{locator.owner}/{locator.repo}",
            publisher_id=str(owner_id),
            publisher_handle=owner_login.casefold(),
            commit_sha=commit_sha,
            artifact_digest=sha256_bytes(archive),
            catalog_key=(
                f"github:{owner_login.casefold()}/{locator.repo.casefold()}"
                + (f"#{locator.subdir.strip('/')}" if locator.subdir else "")
            ),
            subdir=locator.subdir,
        )
        return ResolvedSource(source=source, archive=archive, publisher_verified=True)


@dataclass(frozen=True)
class ClawHubRelease:
    version: str
    artifact_sha256: str
    artifact_url: str
    publisher_id: str
    publisher_handle: str
    commit_sha: str | None
    slug: str | None = None


def _required_string(mapping: Mapping[str, object], key: str, *, what: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OwnerNotVerifiable(f"{what} is missing {key}")
    return value


def parse_clawhub_release(payload: Mapping[str, object]) -> ClawHubRelease:
    version = _required_string(payload, "version", what="ClawHub release")
    if not SEMVER_RE.match(version):
        raise OwnerNotVerifiable("ClawHub release version is not exact SemVer")
    artifact = payload.get("artifact")
    if not isinstance(artifact, dict):
        raise OwnerNotVerifiable("ClawHub release artifact is missing")
    url = _required_string(artifact, "url", what="ClawHub artifact")
    if not url.startswith("https://"):
        raise OwnerNotVerifiable("ClawHub artifact URL must be HTTPS")
    digest = _required_string(artifact, "sha256", what="ClawHub artifact")
    if not SHA256_RE.match(digest):
        raise OwnerNotVerifiable("ClawHub artifact digest is not SHA-256")
    publisher = payload.get("publisher")
    if not isinstance(publisher, dict):
        raise OwnerNotVerifiable("ClawHub publisher identity is not verifiable")
    publisher_id = _required_string(publisher, "id", what="ClawHub publisher")
    publisher_handle = _required_string(publisher, "handle", what="ClawHub publisher").casefold()
    commit = payload.get("commitSha")
    if commit is not None and (not isinstance(commit, str) or not COMMIT_RE.match(commit)):
        raise OwnerNotVerifiable("ClawHub commit SHA is malformed")
    slug = payload.get("slug")
    if not isinstance(slug, str) or not re.match(r"\A[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z", slug):
        # Without the release slug, two different ClawHub Skills from one publisher collapse to the same lineage.
        raise OwnerNotVerifiable("ClawHub slug is required and must be a valid Skill name")
    return ClawHubRelease(version, digest, url, publisher_id, publisher_handle, commit, slug)


def resolve_clawhub(
    payload: Mapping[str, object],
    *,
    egress: EgressPort,
    expected_publisher_id: str,
    expected_publisher_handle: str,
    max_archive_bytes: int,
) -> ResolvedSource:
    release = parse_clawhub_release(payload)
    if release.publisher_id != expected_publisher_id:
        raise OwnerNotVerifiable("ClawHub publisher id does not match")
    if release.publisher_handle != expected_publisher_handle.casefold():
        raise OwnerNotVerifiable("ClawHub publisher handle does not match")
    archive = egress.fetch_bytes(release.artifact_url, max_bytes=max_archive_bytes)
    if sha256_bytes(archive) != release.artifact_sha256:
        raise ArchiveRejected("ClawHub artifact digest does not match")
    source = SkillSource(
        kind=SourceKind.CLAWHUB,
        origin_uri="https://clawhub.ai",
        publisher_id=release.publisher_id,
        publisher_handle=release.publisher_handle,
        commit_sha=release.commit_sha,
        artifact_digest=release.artifact_sha256,
        catalog_key=f"clawhub:{release.publisher_handle}:{release.slug}",
    )
    return ResolvedSource(source=source, archive=archive, publisher_verified=True)


def upload_source(data: bytes, *, filename: str | None = None) -> ResolvedSource:
    source = SkillSource(
        kind=SourceKind.UPLOAD,
        origin_uri="upload://local" if filename is None else f"upload://{filename[:100]}",
        artifact_digest=sha256_bytes(data),
    )
    return ResolvedSource(source=source, archive=data, publisher_verified=False)


def extract_subdir(files: dict[str, bytes], subdir: str | None) -> dict[str, bytes]:
    if not subdir:
        return files
    prefix = subdir.strip("/") + "/"
    selected = {path[len(prefix) :]: body for path, body in files.items() if path.startswith(prefix)}
    if not selected:
        raise ArchiveRejected("GitHub subdir does not contain the Skill")
    return selected


def collect_builtin(skills_root: Path, name: str, *, limits: int = 2 * 1024 * 1024) -> tuple[ValidatedBundle, str]:
    """Read a packaged built-in Skill without following links; returns bundle and filesystem digest."""
    root = Path(skills_root).resolve()
    candidate = (root / name).resolve()
    if candidate.parent != root or candidate.name != name:
        raise ArchiveRejected("built-in Skill path escapes the packaged root")
    if not candidate.is_dir() or candidate.is_symlink():
        raise ArchiveRejected("built-in Skill directory is missing")
    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(candidate.rglob("*")):
            if path.is_symlink():
                raise ArchiveRejected("built-in Skill contains a symlink")
            if path.is_dir():
                continue
            resolved = path.resolve()
            if not str(resolved).startswith(str(candidate) + os.sep):
                raise ArchiveRejected("built-in Skill path escapes its directory")
            total += resolved.stat().st_size
            if total > limits:
                raise ArchiveRejected("built-in Skill exceeds the byte limit")
            archive.write(resolved, resolved.relative_to(candidate).as_posix())
    bundle = inspect_zip(buffer.getvalue())
    files_digest = sha256_bytes(
        "\n".join(f"{file.relative_path}:{file.sha256}" for file in bundle.entries).encode("utf-8")
    )
    return bundle, files_digest
