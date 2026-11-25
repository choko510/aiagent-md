"""Landlock source projection tests. These are not VM/egress tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from aiagent_sandbox.landlock import _validate_separate_source, restrict_to_workspace

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux Landlock required")


def test_rejects_overlapping_source_and_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    nested = root / "source"
    nested.mkdir(parents=True)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    nested_fd = os.open(nested, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for output, source_fd in ((root, root_fd), (root, nested_fd), (nested, root_fd)):
            with pytest.raises(ValueError, match="overlap"):
                _validate_separate_source(output, source_fd)
    finally:
        os.close(root_fd)
        os.close(nested_fd)


def test_landlock_source_is_read_only_and_output_is_writable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    (source / "input.py").write_bytes(b"source\n")
    (source / "input.py").chmod(0o666)
    source.chmod(0o777)
    output.chmod(0o777)
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            restrict_to_workspace(output, source_fd=source_fd)
            assert (source / "input.py").read_bytes() == b"source\n"
            (output / "result").write_bytes(b"output")
            denied = 0
            for action in (
                lambda: (source / "input.py").write_bytes(b"changed"),
                lambda: os.truncate(source / "input.py", 0),
                lambda: os.link(source / "input.py", output / "hardlink"),
                lambda: os.rename(source / "input.py", output / "moved"),
            ):
                try:
                    action()
                except OSError:
                    denied += 1
            os.write(write_fd, f"denied={denied}".encode("ascii"))
        except BaseException as exc:
            os.write(write_fd, type(exc).__name__.encode("ascii"))
        finally:
            os._exit(0)
    os.close(write_fd)
    os.close(source_fd)
    result = os.read(read_fd, 100)
    os.close(read_fd)
    os.waitpid(pid, 0)
    assert result == b"denied=4"
    assert (source / "input.py").read_bytes() == b"source\n"
    assert (output / "result").read_bytes() == b"output"


def test_read_only_source_requires_landlock_abi_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from aiagent_sandbox import landlock

    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(landlock, "_syscall", lambda *_args: 2)
    try:
        with pytest.raises(OSError, match="ABI 3"):
            restrict_to_workspace(output, source_fd=source_fd)
    finally:
        os.close(source_fd)
