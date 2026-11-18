from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest
from test_code_intelligence_projection import make_repo, open_reader

from aiagent_development.code_intelligence.git_proof import (
    GitTreeProof,
    TreeObject,
    build_git_tree_proof,
    verify_git_tree_proof,
)
from aiagent_development.code_intelligence.models import Coverage, Identity, ReadError
from aiagent_development.code_intelligence.projection import ProjectionManifest, prepare_projection
from aiagent_development.code_intelligence.projection_stage import stage_git_projection
from aiagent_development.code_intelligence.projection_stream import ProjectionEncoder


def proof_fixture(tmp_path: Path):
    repo = make_repo(tmp_path, {"a.py": b"pass\n", "pkg/b.py": b"import a\n", ".env": b"hidden", "notes.txt": b"n"})
    reader = open_reader(repo)
    manifest = prepare_projection(reader, reader.snapshot.identity, reader.tree)
    proof = build_git_tree_proof(reader)
    return reader, manifest, proof


def test_actual_git_proof_matches_committed_tree_and_projection(tmp_path: Path) -> None:
    reader, manifest, proof = proof_fixture(tmp_path)
    check = verify_git_tree_proof(proof, manifest, reader.snapshot.identity, deadline=time.monotonic() + 10)
    assert check.commit == reader.snapshot.identity.commit and check.tree == reader.tree
    assert check.tree_count == 2 and check.entry_count == 5 and check.projected_files == 2
    assert check.excluded_files == 2
    assert check.object_hash == "sha1" and check.collision_resistance == "LEGACY_SHA1"
    assert b"hidden" not in proof.commit and all(b"hidden" not in tree.data for tree in proof.trees)


def test_complete_source_requirement_is_proved_from_git_tree(tmp_path: Path) -> None:
    reader, manifest, proof = proof_fixture(tmp_path)
    encoder = ProjectionEncoder(reader, manifest)
    wire = b"".join(encoder.chunks())
    assert encoder.receipt is not None
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    # Removing advisory issues cannot hide excluded Git blobs from the proof.
    with pytest.raises(ReadError, match="complete source projection unavailable"):
        with stage_git_projection(
            parent,
            io.BytesIO(wire),
            replace(manifest, issues=()),
            encoder.receipt,
            proof,
            reader.snapshot.identity,
            deadline=time.monotonic() + 10,
            require_complete=True,
        ):
            pytest.fail("incomplete source accepted")
    assert not list(parent.iterdir())
    complete_root = tmp_path / "complete"
    complete_root.mkdir()
    complete_reader = open_reader(make_repo(complete_root, {"a.py": b"value = 1\n"}))
    complete_manifest = prepare_projection(complete_reader, complete_reader.snapshot.identity, complete_reader.tree)
    complete_proof = build_git_tree_proof(complete_reader)
    complete_encoder = ProjectionEncoder(complete_reader, complete_manifest)
    complete_wire = b"".join(complete_encoder.chunks())
    assert complete_encoder.receipt is not None
    with stage_git_projection(
        parent,
        io.BytesIO(complete_wire),
        complete_manifest,
        complete_encoder.receipt,
        complete_proof,
        complete_reader.snapshot.identity,
        deadline=time.monotonic() + 10,
        require_complete=True,
    ) as staged:
        assert staged.check.excluded_files == 0
    assert not list(parent.iterdir())


def test_git_proof_rejects_scope_commit_tree_and_projection_mismatch(tmp_path: Path) -> None:
    reader, manifest, proof = proof_fixture(tmp_path)
    deadline = time.monotonic() + 10
    with pytest.raises(ReadError, match="scope"):
        verify_git_tree_proof(proof, manifest, replace(reader.snapshot.identity, tenant_id="other"), deadline=deadline)
    with pytest.raises(ReadError, match="commit"):
        verify_git_tree_proof(
            replace(proof, commit=proof.commit + b"x"), manifest, reader.snapshot.identity, deadline=deadline
        )
    with pytest.raises(ReadError, match="tree"):
        verify_git_tree_proof(proof, replace(manifest, tree="0" * 40), reader.snapshot.identity, deadline=deadline)
    with pytest.raises(ReadError, match="projection"):
        verify_git_tree_proof(
            proof, replace(manifest, files=manifest.files[:-1]), reader.snapshot.identity, deadline=deadline
        )


def test_git_proof_rejects_missing_changed_duplicate_and_unrelated_tree(tmp_path: Path) -> None:
    reader, manifest, proof = proof_fixture(tmp_path)
    deadline = time.monotonic() + 10
    bad = (
        replace(proof, trees=proof.trees[:-1]),
        replace(proof, trees=(replace(proof.trees[0], data=proof.trees[0].data + b"x"), *proof.trees[1:])),
        replace(proof, trees=(*proof.trees, proof.trees[0])),
    )
    for item in bad:
        with pytest.raises(ReadError):
            verify_git_tree_proof(item, manifest, reader.snapshot.identity, deadline=deadline)
    raw = b""
    oid = hashlib.sha1(b"tree 0\0", usedforsecurity=False).hexdigest()
    with pytest.raises(ReadError, match="unreferenced"):
        verify_git_tree_proof(
            replace(proof, trees=(*proof.trees, TreeObject(oid, raw))),
            manifest,
            reader.snapshot.identity,
            deadline=deadline,
        )


def test_git_proof_deadline_is_enforced(tmp_path: Path) -> None:
    reader, manifest, proof = proof_fixture(tmp_path)
    with pytest.raises(ReadError, match="deadline"):
        verify_git_tree_proof(proof, manifest, reader.snapshot.identity, deadline=time.monotonic() - 1)


@pytest.mark.parametrize("kind", ["traversal", "symlink", "duplicate", "truncated"])
def test_git_proof_rejects_malicious_tree_content(kind: str) -> None:
    item = b"100644 ok.py\0" + b"a" * 20
    if kind == "traversal":
        item = b"100644 ..\0" + b"a" * 20
    elif kind == "symlink":
        item = b"120000 link\0" + b"a" * 20
    elif kind == "duplicate":
        item = item + item
    else:
        item = item[:-1]
    tree_oid = hashlib.sha1(f"tree {len(item)}\0".encode() + item, usedforsecurity=False).hexdigest()
    commit = f"tree {tree_oid}\n\nfixture\n".encode()
    commit_oid = hashlib.sha1(f"commit {len(commit)}\0".encode() + commit, usedforsecurity=False).hexdigest()
    identity = Identity("tenant", "owner", "binding", "revision", commit_oid)
    manifest = ProjectionManifest(identity, tree_oid, (), 0, Coverage.PARTIAL, ())
    proof = GitTreeProof(commit, (TreeObject(tree_oid, item),))
    with pytest.raises(ReadError):
        verify_git_tree_proof(proof, manifest, identity, deadline=time.monotonic() + 10)


def test_git_sha256_object_proof(tmp_path: Path) -> None:
    git = shutil.which("git")
    assert git
    root = tmp_path / "sha256-repo"
    root.mkdir()
    init = subprocess.run([git, "-C", str(root), "init", "--object-format=sha256"], capture_output=True, check=False)
    if init.returncode:
        pytest.skip("Git SHA-256 repositories are unavailable")

    def run(*args: str) -> bytes:
        return subprocess.check_output([git, "-C", str(root), *args], stderr=subprocess.DEVNULL)

    run("config", "user.name", "Fixture")
    run("config", "user.email", "fixture@example.invalid")
    (root / "a.py").write_bytes(b"pass\n")
    run("add", "a.py")
    run("commit", "-m", "fixture")
    sha = run("rev-parse", "HEAD").decode().strip()
    reader = open_reader((root, sha, Path(git)))
    manifest = prepare_projection(reader, reader.snapshot.identity, reader.tree)
    proof = build_git_tree_proof(reader)
    result = verify_git_tree_proof(proof, manifest, reader.snapshot.identity, deadline=time.monotonic() + 10)
    assert result.object_hash == "sha256" and result.collision_resistance == "SHA256"
