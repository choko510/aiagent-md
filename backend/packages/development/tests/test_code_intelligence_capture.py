from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time
from pathlib import Path

import pytest
from test_code_intelligence_git import repo as _repo  # noqa: F401
from test_code_intelligence_scip import prepare

from aiagent_development.code_intelligence.artifact_capture import capture_scip
from aiagent_development.code_intelligence.models import ReadError
from aiagent_development.code_intelligence.scip_navigation import IndexStatus, load_index

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="POSIX staging and SCIP decoder")


def directories(tmp_path: Path) -> tuple[Path, int]:
    parent = tmp_path / "private"
    output = tmp_path / "output"
    parent.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    return parent, os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def test_capture_exact_bytes_in_unlinked_readonly_file(tmp_path: Path) -> None:
    parent, output_fd = directories(tmp_path)
    data = b"synthetic scip bytes\0" * 6000
    (tmp_path / "output" / "index.scip").write_bytes(data)
    try:
        with capture_scip(output_fd, parent, expected_uid=os.geteuid(), deadline=time.monotonic() + 10) as captured:
            assert captured.sha256 == hashlib.sha256(data).hexdigest()
            assert captured.byte_length == len(data) and os.read(captured.fd, len(data) + 1) == data
            with pytest.raises(OSError):
                os.write(captured.fd, b"x")
            assert list(parent.iterdir()) == []
            fd = captured.fd
        with pytest.raises(OSError):
            os.fstat(fd)
    finally:
        os.close(output_fd)


def test_capture_refuses_symlink_hardlink_and_oversized(tmp_path: Path) -> None:
    parent, output_fd = directories(tmp_path)
    output = tmp_path / "output"
    outside = tmp_path / "outside"
    outside.write_bytes(b"not-an-artifact")
    try:
        (output / "index.scip").symlink_to(outside)
        with pytest.raises(OSError):
            with capture_scip(output_fd, parent, expected_uid=os.geteuid(), deadline=time.monotonic() + 10):
                pytest.fail("symlink accepted")
        (output / "index.scip").unlink()
        os.link(outside, output / "index.scip")
        with pytest.raises(ReadError, match="invalid execution artifact"):
            with capture_scip(output_fd, parent, expected_uid=os.geteuid(), deadline=time.monotonic() + 10):
                pytest.fail("hardlink accepted")
        (output / "index.scip").unlink()
        (output / "index.scip").write_bytes(b"x" * 257)
        with pytest.raises(ReadError, match="invalid execution artifact"):
            with capture_scip(
                output_fd, parent, expected_uid=os.geteuid(), deadline=time.monotonic() + 10, max_bytes=256
            ):
                pytest.fail("oversized file accepted")
        assert list(parent.iterdir()) == []
    finally:
        os.close(output_fd)


def test_capture_rejects_file_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent, output_fd = directories(tmp_path)
    output = tmp_path / "output"
    (output / "index.scip").write_bytes(b"original")
    original_read = os.read
    replaced = False

    def replace_after_read(fd: int, length: int) -> bytes:
        nonlocal replaced
        data = original_read(fd, length)
        if not replaced:
            replaced = True
            (output / "replacement").write_bytes(b"attacker")
            os.replace(output / "replacement", output / "index.scip")
        return data

    monkeypatch.setattr(os, "read", replace_after_read)
    try:
        with pytest.raises(ReadError, match="changed during capture"):
            with capture_scip(output_fd, parent, expected_uid=os.geteuid(), deadline=time.monotonic() + 10):
                pytest.fail("replaced file accepted")
        assert list(parent.iterdir()) == []
    finally:
        os.close(output_fd)


def test_captured_official_scip_fixture_revalidated_by_b1(repo: tuple[Path, str, Path], tmp_path: Path) -> None:
    root, _, _ = repo
    fixture = Path(__file__).parent / "fixtures" / "scip_python"
    for name in ("library.py", "main.py"):
        shutil.copyfile(fixture / (name + ".txt"), root / name)
    data = (fixture / "index.scip").read_bytes()
    snapshot, manifest = prepare(repo, data, "scip-python", "0.6.8")
    parent, output_fd = directories(tmp_path)
    (tmp_path / "output" / "index.scip").write_bytes(data)
    try:
        with capture_scip(output_fd, parent, expected_uid=os.geteuid(), deadline=time.monotonic() + 10) as captured:
            copied = os.read(captured.fd, captured.byte_length + 1)
            assert captured.sha256 == manifest.index_digest
            result = load_index(snapshot, manifest, copied)
            assert result.status == IndexStatus.PARTIAL and result.graph is not None
            symbol = "scip-python python synthetic-demo 1.0 library/greet()."
            assert result.graph.definitions(symbol)[0].path == "library.py"
            assert {item.path for item in result.graph.references(symbol)} == {"main.py"}
    finally:
        os.close(output_fd)
