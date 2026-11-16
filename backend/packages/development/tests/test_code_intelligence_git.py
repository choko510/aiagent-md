from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from aiagent_development.code_intelligence.git_reader import GitSnapshot
from aiagent_development.code_intelligence.models import AuthorizedSnapshot, Identity, ReadError


@pytest.fixture(name="repo")
def repo(tmp_path: Path) -> tuple[Path, str, Path]:
    git = shutil.which("git")
    assert git
    root = tmp_path / "repo"
    root.mkdir()

    def run(*args: str) -> str:
        return subprocess.check_output([git, "-C", str(root), *args], stderr=subprocess.DEVNULL).decode().strip()

    run("init")
    run("config", "user.name", "Test")
    run("config", "user.email", "test@example.invalid")
    (root / "a.py").write_text("old\n")
    run("add", "a.py")
    run("commit", "-m", "first")
    sha = run("rev-parse", "HEAD")
    return root, sha, Path(git)


def reader(repo: tuple[Path, str, Path]) -> GitSnapshot:
    root, sha, git = repo
    return GitSnapshot(AuthorizedSnapshot(Identity("tenant", "owner", "binding", "1", sha), root), git)


def test_snapshot_old_and_dirty(repo: tuple[Path, str, Path]) -> None:
    root, _, git = repo
    (root / "a.py").write_text("new\n")
    (root / "new.py").write_text("untracked\n")
    subprocess.run([str(git), "-C", str(root), "commit", "-am", "second"], check=True, stdout=subprocess.DEVNULL)
    snapshot = reader(repo)
    entries, issues = snapshot.entries()
    assert not issues
    assert [entry.path for entry in entries] == ["a.py"]
    assert snapshot.read(entries[0]) == b"old\n"


def test_reject_partial_clone_before_read(repo: tuple[Path, str, Path]) -> None:
    root, _, git = repo
    subprocess.run([str(git), "-C", str(root), "config", "extensions.partialClone", "origin"], check=True)
    with pytest.raises(ReadError, match="partial clone"):
        reader(repo)


def test_reject_promisor_pack(repo: tuple[Path, str, Path]) -> None:
    root, _, _ = repo
    (root / ".git" / "objects" / "pack" / "fake.promisor").touch()
    with pytest.raises(ReadError, match="promisor"):
        reader(repo)
