"""Inventory Git objects without executing manifests or following links."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath

from .git_reader import GitSnapshot
from .models import Coverage, Entry, Identity, ReadError
from .paths import display_path

MANIFESTS = {
    "pyproject.toml",
    "requirements.txt",
    "uv.lock",
    "package.json",
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "gradle.properties",
    "settings.gradle.kts",
    "build.gradle.kts",
    "pom.xml",
}
LOCKS = {"uv.lock", "pnpm-lock.yaml", "package-lock.json", "yarn.lock", "Cargo.lock", "go.sum"}
EXTENSIONS = {
    ".py": "Python",
    ".pyi": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".sh": "Shell",
    ".bash": "Shell",
    ".go": "Go",
}


@dataclass(frozen=True)
class FileInfo:
    entry: Entry
    language: str
    classification: str
    digest: str | None
    reason: str | None = None


@dataclass(frozen=True)
class RepositoryInventory:
    identity: Identity
    files: tuple[FileInfo, ...]
    languages: tuple[tuple[str, int], ...]
    manifests: tuple[FileInfo, ...]
    locks: tuple[FileInfo, ...]
    directories: tuple[str, ...]
    coverage: Coverage
    issues: tuple[str, ...]


def _classify(entry: Entry, data: bytes) -> tuple[str, str, str | None]:  # noqa: PLR0911
    p = PurePosixPath(entry.path)
    parts = {x.lower() for x in p.parts}
    language = EXTENSIONS.get(p.suffix.lower(), "Other")
    if entry.kind != "BLOB":
        return language, entry.kind, "link target not read"
    if data.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
        return language, "UNKNOWN", "LFS pointer; content not fetched"
    if b"\x00" in data:
        return language, "BINARY", "binary data not analyzed"
    if parts & {"node_modules", "vendor", "third_party", "third-party"}:
        return language, "THIRD_PARTY", "third-party heuristic"
    if parts & {"dist", "build", "generated", "__generated__"} or ".generated." in p.name:
        return language, "GENERATED", "generated heuristic"
    if p.name in MANIFESTS or language in {"JSON", "YAML", "TOML"}:
        return language, "CONFIG", None
    if (
        "tests" in parts
        or "test" in parts
        or p.name.startswith("test_")
        or ".test." in p.name
        or p.name.endswith("_test.py")
    ):
        return language, "TEST", None
    if language != "Other":
        return language, "SOURCE", None
    return language, "UNKNOWN", "unrecognized file type"


def inventory(reader: GitSnapshot) -> RepositoryInventory:
    try:
        entries, tree_issues = reader.entries()
    except ReadError as exc:
        return RepositoryInventory(reader.snapshot.identity, (), (), (), (), (), Coverage.UNAVAILABLE, (str(exc),))
    except (OSError, UnicodeError):
        return RepositoryInventory(
            reader.snapshot.identity, (), (), (), (), (), Coverage.UNAVAILABLE, ("tree unavailable",)
        )
    issues = list(tree_issues)
    files: list[FileInfo] = []
    languages: dict[str, int] = {}
    directories: set[str] = set()
    for entry in entries:
        parent = str(PurePosixPath(entry.path).parent)
        if parent != ".":
            directories.add(parent)
        data = b""
        digest = None
        reason = None
        name = PurePosixPath(entry.path).name.lower()
        sensitive = (
            name.startswith(".env")
            or name in {"credentials", "id_rsa", "id_ed25519"}
            or name.endswith((".pem", ".key", ".p12", ".pfx"))
        )
        if entry.kind == "BLOB" and sensitive:
            reason = "sensitive filename withheld"
            issues.append(f"{display_path(entry.path)}: {reason}")
        elif entry.kind == "BLOB":
            try:
                data = reader.read(entry)
                digest = hashlib.sha256(data).hexdigest()
            except ReadError as exc:
                reason = str(exc)
                issues.append(f"{display_path(entry.path)}: {reason}")
        language, classification, classification_reason = _classify(entry, data)
        if reason:
            classification = "UNKNOWN"
        reason = reason or classification_reason
        if reason and classification in {"SYMLINK", "SUBMODULE", "GENERATED", "THIRD_PARTY", "BINARY"}:
            issues.append(f"{display_path(entry.path)}: {reason}")
        if classification_reason and (
            "LFS" in classification_reason or classification_reason == "unrecognized file type"
        ):
            issues.append(f"{display_path(entry.path)}: {classification_reason}")
        if language != "Other":
            languages[language] = languages.get(language, 0) + 1
        files.append(FileInfo(entry, language, classification, digest, reason))
    manifests = tuple(f for f in files if PurePosixPath(f.entry.path).name in MANIFESTS)
    return RepositoryInventory(
        reader.snapshot.identity,
        tuple(files),
        tuple(sorted(languages.items())),
        manifests,
        tuple(f for f in manifests if PurePosixPath(f.entry.path).name in LOCKS),
        tuple(sorted(directories)),
        Coverage.PARTIAL if issues else Coverage.COMPLETE,
        tuple(issues),
    )
