"""Validate Git tree paths without consulting or following the worktree."""

from __future__ import annotations


def validate_git_path(path: str) -> str:
    """Reject paths that cannot safely be displayed or used as repository paths.

    Git paths may contain spaces, Unicode and newlines. They are never passed to
    a shell or opened relative to the host filesystem by this validator.
    """
    if not isinstance(path, str) or not path or len(path.encode("utf-8", errors="strict")) > 4096:
        raise ValueError("empty or oversized Git path")
    if "\x00" in path or "\\" in path or path.startswith("/"):
        raise ValueError("unsafe Git path")
    parts = path.split("/")
    if any(part in ("", ".", "..") or part.casefold() == ".git" for part in parts):
        raise ValueError("unsafe Git path component")
    if any(":" in part for part in parts):
        raise ValueError("nonportable Git path component")
    return path
