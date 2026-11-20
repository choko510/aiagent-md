from __future__ import annotations

import hashlib
import io
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest
from test_code_intelligence_projection import make_repo, open_reader

from aiagent_development.code_intelligence.git_proof import build_git_tree_proof
from aiagent_development.code_intelligence.models import ReadError
from aiagent_development.code_intelligence.projection import prepare_projection
from aiagent_development.code_intelligence.projection_stage import (
    cell_source_traversal,
    stage_git_projection,
    stage_projection,
)
from aiagent_development.code_intelligence.projection_stream import ProjectionEncoder

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow staging")


def encoded(tmp_path: Path):
    reader = open_reader(make_repo(tmp_path, {"a.py": b"print('hello')\n", "nested/b.py": b"import a\n"}))
    manifest = prepare_projection(reader, reader.snapshot.identity, reader.tree)
    encoder = ProjectionEncoder(reader, manifest)
    data = b"".join(encoder.chunks())
    assert encoder.receipt is not None
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    return manifest, encoder.receipt, data, parent


def read_staged(root_fd: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
    try:
        return os.read(fd, 1024)
    finally:
        os.close(fd)


def test_stage_verified_git_snapshot_and_cleanup(tmp_path: Path) -> None:
    manifest, receipt, data, parent = encoded(tmp_path)
    with stage_projection(parent, io.BytesIO(data), manifest, receipt, deadline=time.monotonic() + 10) as stage:
        assert read_staged(stage, "a.py") == b"print('hello')\n"
        assert read_staged(stage, "nested/b.py") == b"import a\n"
        assert len(list(parent.iterdir())) == 1
    assert list(parent.iterdir()) == []


def test_stage_rejects_truncation_and_tampering_and_cleans_up(tmp_path: Path) -> None:
    manifest, receipt, data, parent = encoded(tmp_path)
    for data_bad in (data[:-1], data[:-1] + bytes([data[-1] ^ 1])):
        with pytest.raises(ReadError):
            with stage_projection(parent, io.BytesIO(data_bad), manifest, receipt, deadline=time.monotonic() + 10):
                pytest.fail("unverified source exposed")
        assert list(parent.iterdir()) == []
    bad_receipt = replace(receipt, sha256=hashlib.sha256(b"other").hexdigest())
    with pytest.raises(ReadError, match="digest mismatch"):
        with stage_projection(parent, io.BytesIO(data), manifest, bad_receipt, deadline=time.monotonic() + 10):
            pytest.fail("unverified source exposed")
    assert list(parent.iterdir()) == []


def test_stage_rejects_path_traversal_and_untrusted_parent(tmp_path: Path) -> None:
    manifest, receipt, data, parent = encoded(tmp_path)
    bad = replace(manifest, files=(replace(manifest.files[0], path="../escape.py"),))
    with pytest.raises(ReadError):
        with stage_projection(parent, io.BytesIO(data), bad, receipt, deadline=time.monotonic() + 10):
            pytest.fail("unsafe path accepted")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        with stage_projection(link, io.BytesIO(data), manifest, receipt, deadline=time.monotonic() + 10):
            pytest.fail("symlink parent accepted")
    assert not list(outside.iterdir())


def test_stage_rejects_rehashed_changed_source(tmp_path: Path) -> None:
    manifest, receipt, data, parent = encoded(tmp_path)
    changed = data[:-1] + bytes([data[-1] ^ 1])
    forged = replace(receipt, sha256=hashlib.sha256(changed).hexdigest())
    with pytest.raises(ReadError, match="source verification"):
        with stage_projection(parent, io.BytesIO(changed), manifest, forged, deadline=time.monotonic() + 10):
            pytest.fail("altered source exposed")
    assert not list(parent.iterdir())


def test_stage_accepts_fragmented_input_without_large_reads(tmp_path: Path) -> None:
    manifest, receipt, data, parent = encoded(tmp_path)

    class Fragmented(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            assert 0 <= size <= 65536
            return super().read(min(size, 7))

    with stage_projection(parent, Fragmented(data), manifest, receipt, deadline=time.monotonic() + 10) as stage:
        assert read_staged(stage, "nested/b.py") == b"import a\n"
    assert not list(parent.iterdir())


def test_stage_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    manifest, receipt, data, parent = encoded(tmp_path)
    link = tmp_path / "ancestor"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        with stage_projection(link / parent.name, io.BytesIO(data), manifest, receipt, deadline=time.monotonic() + 10):
            pytest.fail("symlink ancestor accepted")
    assert not list(parent.iterdir())


def test_stage_rejects_source_if_deadline_expires_during_read(tmp_path: Path) -> None:
    manifest, receipt, data, parent = encoded(tmp_path)

    class Slow(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            time.sleep(0.03)
            return super().read(size)

    with pytest.raises(ReadError, match="deadline"):
        with stage_projection(parent, Slow(data), manifest, receipt, deadline=time.monotonic() + 0.01):
            pytest.fail("expired source exposed")
    assert not list(parent.iterdir())


def test_staging_checks_git_blob_id_even_if_wire_and_sha256_are_rehashed(tmp_path: Path) -> None:
    manifest, receipt, data, parent = encoded(tmp_path)
    original = b"import a\n"
    altered = b"import b\n"
    old_entry = manifest.files[1]
    altered_sha = hashlib.sha256(altered).hexdigest()
    altered_data = data.replace(bytes.fromhex(old_entry.sha256), bytes.fromhex(altered_sha), 1).replace(
        original, altered, 1
    )
    altered_manifest = replace(manifest, files=(manifest.files[0], replace(old_entry, sha256=altered_sha)))
    altered_receipt = replace(receipt, sha256=hashlib.sha256(altered_data).hexdigest())
    with pytest.raises(ReadError, match="source verification"):
        with stage_projection(
            parent, io.BytesIO(altered_data), altered_manifest, altered_receipt, deadline=time.monotonic() + 10
        ):
            pytest.fail("Git blob mismatch accepted")
    assert not list(parent.iterdir())


def test_staging_rejects_different_identity(tmp_path: Path) -> None:
    manifest, receipt, data, parent = encoded(tmp_path)
    wrong_identity = replace(manifest.identity, tenant_id="other-tenant")
    with pytest.raises(ReadError):
        with stage_projection(
            parent,
            io.BytesIO(data),
            replace(manifest, identity=wrong_identity),
            receipt,
            deadline=time.monotonic() + 10,
        ):
            pytest.fail("different tenant accepted")
    assert not list(parent.iterdir())


def test_cell_source_traversal_rejects_untrusted_inputs(tmp_path: Path) -> None:
    with pytest.raises(ReadError):
        with cell_source_traversal(tmp_path, cell_gid=0):
            pytest.fail("invalid Cell identity accepted")


def test_git_proof_verified_before_staging(tmp_path: Path) -> None:
    reader = open_reader(make_repo(tmp_path, {"a.py": b"print(1)\n"}))
    manifest = prepare_projection(reader, reader.snapshot.identity, reader.tree)
    proof = build_git_tree_proof(reader)
    encoder = ProjectionEncoder(reader, manifest)
    data = b"".join(encoder.chunks())
    assert encoder.receipt is not None
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    with stage_git_projection(
        parent,
        io.BytesIO(data),
        manifest,
        encoder.receipt,
        proof,
        reader.snapshot.identity,
        deadline=time.monotonic() + 10,
    ) as staged:
        assert read_staged(staged.root_fd, "a.py") == b"print(1)\n"
        assert staged.check.collision_resistance == "LEGACY_SHA1"
    with pytest.raises(ReadError):
        with stage_git_projection(
            parent,
            io.BytesIO(data),
            manifest,
            encoder.receipt,
            replace(proof, commit=proof.commit + b"x"),
            reader.snapshot.identity,
            deadline=time.monotonic() + 10,
        ):
            pytest.fail("invalid Git proof exposed")
    assert not list(parent.iterdir())
