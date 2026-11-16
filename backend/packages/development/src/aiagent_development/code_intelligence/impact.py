"""Diagnostic impact candidates; never selects or omits required tests."""

from __future__ import annotations

import ast
import hashlib
import time
from dataclasses import dataclass
from pathlib import PurePosixPath

from .git_reader import GitSnapshot
from .inventory import LOCKS, MANIFESTS, RepositoryInventory
from .models import Coverage, Identity, ReadError
from .paths import display_path, validate_git_path


@dataclass(frozen=True)
class ImpactCandidate:
    path: str
    reasons: tuple[str, ...]
    confidence: str = "HEURISTIC"


@dataclass(frozen=True)
class ImpactAnalysisResult:
    identity: Identity
    changed: tuple[str, ...]
    candidates: tuple[ImpactCandidate, ...]
    coverage: Coverage
    issues: tuple[str, ...]
    warnings: tuple[str, ...] = (
        "Static imports are candidates, not runtime dependencies.",
        "Dynamic import, reflection, FFI and unsupported languages may hide dependencies.",
        "An empty candidate list does not prove absence of impact.",
    )


def _test_matches(source_stem: str, test_stem: str) -> bool:
    """Use whole names, not arbitrary substrings (e.g. a.py and banana.py)."""
    source, target = source_stem.casefold(), test_stem.casefold()
    return target in {f"test_{source}", f"{source}_test", f"{source}.test", f"{source}.spec"}


def impact(reader: GitSnapshot, inv: RepositoryInventory, changed: tuple[str, ...]) -> ImpactAnalysisResult:  # noqa: PLR0912, PLR0915
    if inv.identity != reader.snapshot.identity:
        raise ReadError("inventory snapshot scope mismatch")
    if not changed or len(changed) > reader.limits.files:
        raise ValueError("invalid changed-file count")
    paths = tuple(sorted({validate_git_path(p) for p in changed}))
    if inv.coverage == Coverage.UNAVAILABLE:
        return ImpactAnalysisResult(inv.identity, paths, (), Coverage.UNAVAILABLE, inv.issues)
    files = {item.entry.path: item for item in inv.files}
    issues = list(inv.issues)
    reasons: dict[str, set[str]] = {}

    def add(path: str, reason: str) -> None:
        reasons.setdefault(path, set()).add(reason)

    # Direct changes are mandatory, even if they alone exceed the candidate limit.
    for path in paths:
        add(path, "direct change")
    exhausted = False
    for path in paths:
        if path not in files:
            issues.append(f"{display_path(path)}: absent from snapshot; may be a deletion")
        source = PurePosixPath(path)
        for candidate, item in files.items():
            if time.monotonic() > reader.deadline:
                issues.append("impact time budget exceeded")
                exhausted = True
                break
            target = PurePosixPath(candidate)
            if target.parent == source.parent and candidate != path:
                add(candidate, "same directory")
            if item.classification == "TEST" and _test_matches(source.stem, target.stem):
                add(candidate, "test filename heuristic")
            if (source.name in MANIFESTS or source.name in LOCKS) and (
                target.parent == source.parent or target.is_relative_to(source.parent)
            ):
                add(candidate, "manifest or lock scope heuristic")
        if exhausted:
            break
    stems = {PurePosixPath(p).stem for p in paths if p.endswith(".py")}
    for item in inv.files:
        if exhausted or time.monotonic() > reader.deadline:
            issues.append("impact time budget exceeded")
            break
        if item.language != "Python" or item.classification not in {"SOURCE", "TEST"}:
            continue
        try:
            raw = reader.read(item.entry)
            if hashlib.sha256(raw).hexdigest() != item.digest:
                raise ReadError("blob digest mismatch")
            tree = ast.parse(raw)
        except (ReadError, SyntaxError, ValueError, UnicodeError, RecursionError):
            issues.append(f"{display_path(item.entry.path)}: Python import analysis unavailable")
            continue
        imports: set[str] = set()
        for node in ast.walk(tree):
            if time.monotonic() > reader.deadline:
                issues.append("impact time budget exceeded")
                exhausted = True
                break
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module)
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Call):
                name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else ""
                )
                if name in {"__import__", "import_module", "getattr", "eval", "exec", "CDLL"}:
                    issues.append(f"{display_path(item.entry.path)}: dynamic reference or FFI detected")
        if any(stem in name.split(".") for name in imports for stem in stems):
            add(item.entry.path, "Python static import name candidate")
        if item.entry.path in paths:
            for name in imports:
                for imported in inv.files:
                    if time.monotonic() > reader.deadline:
                        issues.append("impact time budget exceeded")
                        exhausted = True
                        break
                    if imported.language == "Python" and PurePosixPath(imported.entry.path).stem in name.split("."):
                        add(imported.entry.path, "imported module name candidate")
    direct = set(paths)
    indirect = sorted((path, why) for path, why in reasons.items() if path not in direct)
    remaining = max(0, reader.limits.hits - len(direct))
    if len(indirect) > remaining or len(direct) > reader.limits.hits:
        issues.append("impact candidate limit reached; direct changes retained")
    ordered = sorted([(path, reasons[path]) for path in paths] + indirect[:remaining])
    return ImpactAnalysisResult(
        inv.identity,
        paths,
        tuple(ImpactCandidate(p, tuple(sorted(r))) for p, r in ordered),
        Coverage.PARTIAL if issues else Coverage.TEXT_ONLY,
        tuple(issues),
    )
