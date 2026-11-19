"""REAL git CAS/worktree smoke test against a NEW disposable repository only.

This test-only runner invokes fixed, audited git commands. It never executes
agent-generated code, accesses a user's repository or pushes to GitHub.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess  # nosec B404 - fixed test-only commands against pytest tmp_path
from pathlib import Path

import pytest
from aiagent_domain import uuid7
from aiagent_sandbox.git_operation_journal import GitJournalError, GitOperationJournal

from aiagent_development.contracts import InvalidEvidence, StaleReference
from aiagent_development.git_commit_worker import IsolatedGitCommitWorker


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
    git(
        repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "base",
    )
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "worktree", "add", "--detach", str(worktree), base)
    git(
        worktree,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "candidate",
    )
    new = git(worktree, "rev-parse", "HEAD").stdout.strip()
    assert new != base
    git(repo, "update-ref", "refs/heads/feat/fixture", new, base)
    stale = git(repo, "update-ref", "refs/heads/feat/fixture", base, base, check=False)
    assert stale.returncode != 0
    assert git(repo, "rev-parse", "refs/heads/feat/fixture").stdout.strip() == new
    assert git(worktree, "rev-parse", "HEAD").stdout.strip() == new


def test_isolated_worker_applies_exact_approved_patch_and_cas(tmp_path: Path):
    executable = shutil.which("git")
    assert executable is not None
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "feat/fixture")
    (repo / "example.txt").write_text("before\n", encoding="utf-8")
    git(repo, "add", "example.txt")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "base")
    parent = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "example.txt").write_text("after\n", encoding="utf-8")
    patch = git(repo, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-renames").stdout.encode()
    digest = hashlib.sha256(patch).hexdigest()
    worker = IsolatedGitCommitWorker(repo.resolve(), Path(executable).resolve(), "feat/fixture")
    with pytest.raises(InvalidEvidence):
        worker.commit(parent, patch, "0" * 64, ("example.txt",), "approved")
    with pytest.raises(InvalidEvidence):
        worker.commit(parent, patch, digest, ("different.txt",), "approved")
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == parent
    commit = worker.commit(parent, patch, digest, ("example.txt",), "approved")
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == commit
    assert git(repo, "show", f"{commit}:example.txt").stdout == "after\n"
    assert git(repo, "rev-parse", f"{commit}^ ".strip()).stdout.strip() == parent
    with pytest.raises(StaleReference):
        worker.commit(parent, patch, digest, ("example.txt",), "approved")


def test_isolated_worker_rejects_symlinks_and_main(tmp_path: Path):
    executable = shutil.which("git")
    assert executable is not None
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "feat/fixture")
    git(
        repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "base",
    )
    parent = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "link").symlink_to("outside")
    git(repo, "add", "link")
    patch = git(repo, "diff", "--cached", "--binary", "--full-index", "--no-ext-diff", "--no-renames").stdout.encode()
    worker = IsolatedGitCommitWorker(repo.resolve(), Path(executable).resolve(), "feat/fixture")
    with pytest.raises(InvalidEvidence):
        worker.commit(parent, patch, hashlib.sha256(patch).hexdigest(), ("link",), "approved")
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == parent
    with pytest.raises(ValueError):
        IsolatedGitCommitWorker(repo.resolve(), Path(executable).resolve(), "main")


def test_isolated_worker_does_not_run_configured_textconv(tmp_path: Path):
    executable = shutil.which("git")
    assert executable is not None
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "feat/fixture")
    marker = tmp_path / "textconv-ran"
    converter = tmp_path / "converter"
    converter.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat \"$1\"\n", encoding="utf-8")
    converter.chmod(0o755)
    git(repo, "config", "diff.fixture.textconv", str(converter))
    (repo / ".gitattributes").write_text("*.txt diff=fixture filter=fixture\n", encoding="utf-8")
    (repo / "example.txt").write_text("before\n", encoding="utf-8")
    git(repo, "add", ".gitattributes", "example.txt")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "base")
    parent = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "example.txt").write_text("after\n", encoding="utf-8")
    patch = git(
        repo, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--no-renames"
    ).stdout.encode()
    git(repo, "config", "filter.fixture.clean", str(converter))
    worker = IsolatedGitCommitWorker(repo.resolve(), Path(executable).resolve(), "feat/fixture")
    worker.commit(parent, patch, hashlib.sha256(patch).hexdigest(), ("example.txt",), "approved")
    assert not marker.exists()


def test_git_cas_requires_durable_preparation_when_provider_supplies_fence(tmp_path: Path):
    executable = shutil.which("git")
    assert executable is not None
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "feat/fixture")
    (repo / "a.txt").write_text("before\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "base")
    parent = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("after\n", encoding="utf-8")
    patch = git(
        repo, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--no-renames"
    ).stdout.encode()
    digest = hashlib.sha256(patch).hexdigest()
    worker = IsolatedGitCommitWorker(repo.resolve(), Path(executable).resolve(), "feat/fixture")
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o700)
    journal = GitOperationJournal(directory.resolve(), b"j" * 32)
    op, request = uuid7(), "a" * 64
    attempt, binding = uuid7(), uuid7()
    try:
        journal.admit(op, request, attempt, binding, 1, "feat/fixture")

        def before_cas(commit: str, tree: str) -> None:
            journal.prepare(op, request, parent, commit, tree)
            raise RuntimeError("injected crash before CAS")

        with pytest.raises(RuntimeError, match="injected crash"):
            worker.commit(parent, patch, digest, ("a.txt",), "approved", before_cas=before_cas)
        assert git(repo, "rev-parse", "HEAD").stdout.strip() == parent
        journal.close()
        journal = GitOperationJournal(directory.resolve(), b"j" * 32)
        prepared = journal.read(op, "prepared")
        assert prepared["parent"] == parent and prepared["request_sha256"] == request
        with pytest.raises(GitJournalError):
            journal.admit(op, request, attempt, binding, 1, "feat/fixture")
        with pytest.raises(GitJournalError):
            journal.read(op, "confirmed")

        second = uuid7()
        journal.admit(second, request, attempt, binding, 1, "feat/fixture")
        captured: list[str] = []

        def before_success(commit: str, tree: str) -> None:
            journal.prepare(second, request, parent, commit, tree)
            captured.append(tree)

        commit = worker.commit(parent, patch, digest, ("a.txt",), "approved", before_cas=before_success)
        journal.confirm(second, request, parent, commit, captured[0])
        assert journal.read(second, "confirmed")["commit"] == commit
        (directory / (second + ".prepared")).write_bytes(b"tampered")
        with pytest.raises(GitJournalError):
            journal.read(second, "prepared")
    finally:
        journal.close()
