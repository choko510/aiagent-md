"""Validate untrusted manifest paths without applying patches in the trusted process.

This is *preflight* only. The Untrusted Cell must enforce equivalent checks at
file-open time (openat2 RESOLVE_BENEATH/NO_SYMLINKS or fd-relative equivalent),
not use path-based preflight as a race-free filesystem write authorization.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from .contracts import InvalidPath


def safe_relative_path(candidate: str) -> str:
    if not isinstance(candidate, str) or not candidate or len(candidate) > 1024:
        raise InvalidPath("empty/oversized path")
    if "\\" in candidate or "\x00" in candidate or ":" in candidate or any(ord(c) < 32 for c in candidate):
        raise InvalidPath("non-portable or control-containing path")
    p = PurePosixPath(candidate)
    segments = candidate.split("/")
    if (
        p.is_absolute()
        or len(segments) > 32
        or any(s in ("", ".", "..") or s.casefold() == ".git" for s in segments)
        or candidate.startswith("~")
        or p.as_posix() != candidate
    ):
        raise InvalidPath("path escapes workspace or targets git metadata")
    return candidate


def validate_manifest_paths(repo_root: Path, candidates: tuple[str, ...]) -> tuple[str, ...]:
    if not candidates or len(candidates) > 512 or len(set(candidates)) != len(candidates):
        raise InvalidPath("empty, duplicate or oversized patch manifest")
    root = Path(repo_root)
    if not root.is_dir() or root.is_symlink() or root.resolve(strict=True) != root:
        raise InvalidPath("repository root must be a real directory")
    if not os.path.isabs(str(root)):
        raise InvalidPath("root must be an absolute path selected by the trusted broker")
    normalized = tuple(safe_relative_path(p) for p in candidates)
    for name in normalized:
        cur = root
        for part in name.split("/"):
            cur = cur / part
            # Reject existing links even if they point back into the repository.
            # A later Cell write must repeat this check atomically with opening.
            if cur.is_symlink():
                raise InvalidPath("symlink in patch path")
    return normalized
