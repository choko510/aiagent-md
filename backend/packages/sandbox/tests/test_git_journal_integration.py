"""Git worker durable journal integration against a disposable repository."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from aiagent_development.git_commit_worker import IsolatedGitCommitWorker
from aiagent_domain import uuid7
from aiagent_sandbox.git_operation_journal import GitJournalError, GitOperationJournal


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    executable = shutil.which("git")
    assert executable is not None
    return subprocess.run(  # noqa: S603
        [executable, "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


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
