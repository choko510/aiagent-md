"""Git worker durable journal integration against a disposable repository."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from aiagent_development.git_commit_worker import IsolatedGitCommitWorker
from aiagent_domain import uuid7
from aiagent_sandbox.git_operation_journal import (
    GitJournalError,
    GitOperationJournal,
    verify_confirmed_journal,
    verify_prepared_journal,
)


def test_journal_capacity_fails_closed_and_preserves_evidence_after_restart(tmp_path: Path):
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o700)
    operation, attempt, binding = uuid7(), uuid7(), uuid7()
    request, parent, commit, tree = "a" * 64, "1" * 40, "2" * 40, "3" * 40
    journal = GitOperationJournal(directory.resolve(), b"j" * 32, max_records=3)
    try:
        journal.admit(operation, request, attempt, binding, 1, "feat/fixture")
        journal.prepare(operation, request, parent, commit, tree)
        journal.confirm(operation, request, parent, commit, tree)
        with pytest.raises(GitJournalError, match="capacity"):
            journal.admit(uuid7(), request, attempt, binding, 1, "feat/fixture")
    finally:
        journal.close()
    journal = GitOperationJournal(directory.resolve(), b"j" * 32, max_records=3)
    try:
        assert journal.read(operation, "confirmed")["commit"] == commit
        with pytest.raises(GitJournalError, match="capacity"):
            journal.admit(uuid7(), request, attempt, binding, 1, "feat/fixture")
        assert len(list(directory.iterdir())) == 3
    finally:
        journal.close()


def test_journal_capacity_serializes_competing_workers(tmp_path: Path):
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o700)
    attempt, binding = uuid7(), uuid7()
    barrier = threading.Barrier(8, timeout=5)

    def compete(_index):
        journal = GitOperationJournal(directory.resolve(), b"j" * 32, max_records=3)
        try:
            operation = uuid7()
            barrier.wait()
            try:
                journal.admit(operation, "a" * 64, attempt, binding, 1, "feat/fixture")
            except GitJournalError:
                return None
            return operation
        finally:
            journal.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(compete, range(8)))
    admitted = [operation for operation in outcomes if operation is not None]
    assert 1 <= len(admitted) <= 3
    assert len(list(directory.iterdir())) == len(admitted)
    journal = GitOperationJournal(directory.resolve(), b"j" * 32, max_records=3)
    try:
        assert all(journal.read(operation, "admitted")["attempt"] == attempt for operation in admitted)
    finally:
        journal.close()


def test_journal_capacity_lock_is_shared_across_processes(tmp_path: Path):
    fcntl = pytest.importorskip("fcntl")
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o700)
    lock_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    script = """\
import sys
from pathlib import Path
from aiagent_domain import uuid7
from aiagent_sandbox.git_operation_journal import GitJournalError, GitOperationJournal
journal = GitOperationJournal(Path(sys.argv[1]), b'j' * 32, max_records=3)
try:
    try:
        journal.admit(uuid7(), 'a' * 64, uuid7(), uuid7(), 1, 'feat/fixture')
    except GitJournalError:
        sys.exit(0)
    sys.exit(1)
finally:
    journal.close()
"""
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(  # noqa: S603 - fixed fixture code and a private pytest directory
            [sys.executable, "-c", script, str(directory)], capture_output=True, timeout=10, check=False
        )
        assert result.returncode == 0, result.stderr
        assert list(directory.iterdir()) == []
    finally:
        os.close(lock_fd)
    journal = GitOperationJournal(directory.resolve(), b"j" * 32, max_records=3)
    try:
        operation = uuid7()
        journal.admit(operation, "a" * 64, uuid7(), uuid7(), 1, "feat/fixture")
        assert journal.read(operation, "admitted")["operation"] == operation
    finally:
        journal.close()


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
        envelope = journal.export_prepared(op)
        scope = {
            "operation": op,
            "attempt": attempt,
            "binding": binding,
            "generation": 1,
            "branch": "feat/fixture",
            "request_sha256": request,
            "parent": parent,
        }
        assert verify_prepared_journal(b"j" * 32, envelope, **scope) == prepared
        with pytest.raises(GitJournalError):
            verify_prepared_journal(
                b"j" * 32, {"payload": {**prepared, "tree": "f" * 40}, "mac": envelope["mac"]}, **scope
            )
        with pytest.raises(GitJournalError):
            verify_prepared_journal(b"j" * 32, envelope, **{**scope, "attempt": uuid7()})
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
        prepared2, confirmed2 = journal.read(second, "prepared"), journal.export_confirmed(second)
        verify_confirmed_journal(b"j" * 32, confirmed2, prepared2)
        with pytest.raises(GitJournalError):
            verify_confirmed_journal(b"j" * 32, {**confirmed2, "mac": "0" * 64}, prepared2)
        (directory / (second + ".prepared")).write_bytes(b"tampered")
        with pytest.raises(GitJournalError):
            journal.read(second, "prepared")
    finally:
        journal.close()
