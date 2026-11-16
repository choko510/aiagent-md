"""Literal text matches, not symbol definitions or references."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .git_reader import GitSnapshot
from .inventory import RepositoryInventory
from .models import Coverage, Identity, ReadError
from .paths import display_path


@dataclass(frozen=True)
class SearchHit:
    identity: Identity
    path: str
    line: int
    blob_id: str
    digest: str
    excerpt: str | None = None
    excerpt_reason: str = "source excerpt withheld by default"
    method: str = "literal text match"
    confidence: str = "TEXT_ONLY"


@dataclass(frozen=True)
class CodeSearchResult:
    identity: Identity
    hits: tuple[SearchHit, ...]
    coverage: Coverage
    truncated: bool
    issues: tuple[str, ...]
    interpretation: str = "Text matches are not exact symbol references; zero matches do not prove absence."


def search(  # noqa: PLR0912
    reader: GitSnapshot,
    inv: RepositoryInventory,
    query: str,
    *,
    path_contains: str = "",
    languages: tuple[str, ...] = (),
    classifications: tuple[str, ...] = ("SOURCE", "TEST"),
) -> CodeSearchResult:
    if inv.identity != reader.snapshot.identity:
        raise ReadError("inventory snapshot scope mismatch")
    if inv.coverage == Coverage.UNAVAILABLE:
        return CodeSearchResult(inv.identity, (), Coverage.UNAVAILABLE, False, inv.issues)
    if not query or len(query.encode("utf-8")) > 1024 or "\x00" in query or "\n" in query or "\r" in query:
        raise ValueError("query must be a bounded single-line literal")
    if len(path_contains) > 1024:
        raise ValueError("path filter too long")
    hits: list[SearchHit] = []
    issues = list(inv.issues)
    truncated = False
    for item in inv.files:
        entry = item.entry
        if path_contains not in entry.path or (languages and item.language not in languages):
            continue
        if item.classification not in classifications:
            continue
        try:
            data = reader.read(entry)
            if hashlib.sha256(data).hexdigest() != item.digest:
                raise ReadError("blob digest mismatch")
            content = data.decode("utf-8")
        except (ReadError, UnicodeError):
            issues.append(f"{display_path(entry.path)}: text read failed or invalid UTF-8")
            continue
        for line_no, line in enumerate(content.split("\n"), 1):
            if query in line:
                if len(hits) == reader.limits.hits:
                    truncated = True
                    issues.append("hit limit reached")
                    break
                hits.append(SearchHit(inv.identity, entry.path, line_no, entry.oid, item.digest or ""))
        if truncated:
            break
    return CodeSearchResult(
        inv.identity, tuple(hits), Coverage.PARTIAL if issues else Coverage.TEXT_ONLY, truncated, tuple(issues)
    )
