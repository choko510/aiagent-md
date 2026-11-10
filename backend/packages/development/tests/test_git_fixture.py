"""REAL git CAS/worktree smoke test against a NEW disposable repository only.

This test-only runner invokes fixed, audited git commands. It never executes
agent-generated code, accesses a user's repository or pushes to GitHub.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - fixed test-only commands against pytest tmp_path
from pathlib import Path


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    return subprocess.run(  # nosec B603 - controlled test arguments; not in production
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def test_real_git_fixture_worktree_and_compare_and_swap(tmp_path: Path):
    repo = tmp_path / "disposable-repo"
    worktree = tmp_path / "isolated-worktree"
    repo.mkdir()
    git(repo, "init", "-b", "feat/fixture")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "base")
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "worktree", "add", "--detach", str(worktree), base)
    git(worktree, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "candidate")
    new = git(worktree, "rev-parse", "HEAD").stdout.strip()
    assert new != base
    git(repo, "update-ref", "refs/heads/feat/fixture", new, base)
    stale = git(repo, "update-ref", "refs/heads/feat/fixture", base, base, check=False)
    assert stale.returncode != 0
    assert git(repo, "rev-parse", "refs/heads/feat/fixture").stdout.strip() == new
    assert git(worktree, "rev-parse", "HEAD").stdout.strip() == new
