"""Git source transfer into Landlock staging; not a Supervisor or VM E2E."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from aiagent_development.code_intelligence.git_reader import GitSnapshot
from aiagent_development.code_intelligence.models import AuthorizedSnapshot, Identity
from aiagent_development.code_intelligence.projection import prepare_projection
from aiagent_development.code_intelligence.projection_stage import stage_projection
from aiagent_development.code_intelligence.projection_stream import ProjectionEncoder
from aiagent_sandbox.landlock import restrict_to_workspace

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux Landlock required")


def test_git_snapshot_stages_and_is_read_only_to_landlock_child(tmp_path: Path) -> None:
    git = shutil.which("git")
    assert git
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args: str) -> bytes:
        return subprocess.check_output([git, "-C", str(repo), *args], stderr=subprocess.DEVNULL)

    run("init")
    run("config", "user.name", "Fixture")
    run("config", "user.email", "fixture@example.invalid")
    (repo / "a.py").write_bytes(b"value = 42\n")
    run("add", "a.py")
    run("commit", "-m", "fixture")
    commit = run("rev-parse", "HEAD").decode().strip()
    (repo / "a.py").write_bytes(b"uncommitted\n")
    reader = GitSnapshot(AuthorizedSnapshot(Identity("t", "o", "repo", "revision", commit), repo), Path(git))
    manifest = prepare_projection(reader, reader.snapshot.identity, reader.tree)
    encoder = ProjectionEncoder(reader, manifest)
    wire = b"".join(encoder.chunks())
    assert encoder.receipt is not None
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    output = tmp_path / "output"
    output.mkdir()
    with stage_projection(
        parent, io.BytesIO(wire), manifest, encoder.receipt, deadline=time.monotonic() + 10
    ) as root_fd:
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                restrict_to_workspace(output, source_fd=root_fd)
                fd = os.open("a.py", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
                content = os.read(fd, 100)
                os.close(fd)
                (output / "result").write_bytes(content)
                try:
                    os.open("a.py", os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, dir_fd=root_fd)
                except PermissionError:
                    os.write(write_fd, b"read-only")
                else:
                    os.write(write_fd, b"WRITABLE")
            except BaseException as exc:
                os.write(write_fd, type(exc).__name__.encode("ascii"))
            finally:
                os._exit(0)
        os.close(write_fd)
        result = os.read(read_fd, 100)
        os.close(read_fd)
        os.waitpid(pid, 0)
        assert result == b"read-only"
        assert (output / "result").read_bytes() == b"value = 42\n"
    assert not list(parent.iterdir())
