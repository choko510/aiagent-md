from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from aiagent_development.code_intelligence.git_reader import GitSnapshot
from aiagent_development.code_intelligence.models import AuthorizedSnapshot, Coverage, Identity, Limits, ReadError
from aiagent_development.code_intelligence.projection import prepare_projection
from aiagent_development.code_intelligence.projection_stream import ProjectionEncoder


def make_repo(tmp_path: Path, files: dict[str, bytes]) -> tuple[Path, str, Path]:
    git = shutil.which("git")
    assert git
    root = tmp_path / "repo"
    root.mkdir()

    def run(*args: str) -> bytes:
        return subprocess.check_output([git, "-C", str(root), *args], stderr=subprocess.DEVNULL)

    run("init")
    run("config", "user.name", "Test")
    run("config", "user.email", "test@example.invalid")
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(content)
    run("add", ".")
    run("commit", "-m", "test")
    return root, run("rev-parse", "HEAD").decode().strip(), Path(git)


def open_reader(repo: tuple[Path, str, Path], limits: Limits | None = None) -> GitSnapshot:
    root, sha, git = repo
    return GitSnapshot(AuthorizedSnapshot(Identity("tenant", "owner", "binding", "rev", sha), root), git, limits)


def test_manifest_is_bound_to_verified_commit(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, {"a.py": b"original\n", "b.py": b"import a\n"})
    (repo[0] / "a.py").write_text("changed")
    (repo[0] / "untracked.py").write_text("ignored")
    reader = open_reader(repo)
    result = prepare_projection(reader, reader.snapshot.identity, reader.tree)
    assert [f.path for f in result.files] == ["a.py", "b.py"]
    assert result.files[0].sha256 == hashlib.sha256(b"original\n").hexdigest()
    assert result.total_bytes == len(b"original\nimport a\n")
    assert prepare_projection(reader, reader.snapshot.identity, reader.tree) == result


def test_scope_and_tree_mismatch_fail_closed(tmp_path: Path) -> None:
    reader = open_reader(make_repo(tmp_path, {"a.py": b"x"}))
    identity = reader.snapshot.identity
    for wrong in (
        Identity("other", identity.owner_id, identity.binding_id, identity.revision, identity.commit),
        Identity(identity.tenant_id, "other", identity.binding_id, identity.revision, identity.commit),
        Identity(identity.tenant_id, identity.owner_id, identity.binding_id, "old", identity.commit),
    ):
        with pytest.raises(ReadError, match="scope"):
            prepare_projection(reader, wrong, reader.tree)
    with pytest.raises(ReadError, match="tree"):
        prepare_projection(reader, identity, "0" * 40)


def test_lfs_and_budget_fail_closed(tmp_path: Path) -> None:
    reader = open_reader(make_repo(tmp_path, {"a.py": b"version https://git-lfs.github.com/spec/v1\n"}))
    with pytest.raises(ReadError, match="LFS"):
        prepare_projection(reader, reader.snapshot.identity, reader.tree)
    reader = open_reader((reader.root, reader.snapshot.identity.commit, reader.git), Limits(file_bytes=1))
    with pytest.raises(ReadError, match="budget"):
        prepare_projection(reader, reader.snapshot.identity, reader.tree)


def test_symlink_git_entry_rejected(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, {"a.py": b"x"})
    subprocess.run(
        [
            str(repo[2]),
            "-C",
            str(repo[0]),
            "update-index",
            "--add",
            "--cacheinfo",
            "120000",
            subprocess.check_output([str(repo[2]), "-C", str(repo[0]), "hash-object", "-w", "a.py"]).decode().strip(),
            "link",
        ],
        check=True,
    )
    subprocess.run([str(repo[2]), "-C", str(repo[0]), "commit", "-m", "link"], check=True, stdout=subprocess.DEVNULL)
    sha = subprocess.check_output([str(repo[2]), "-C", str(repo[0]), "rev-parse", "HEAD"]).decode().strip()
    reader = open_reader((repo[0], sha, repo[2]))
    with pytest.raises(ReadError, match="unsupported"):
        prepare_projection(reader, reader.snapshot.identity, reader.tree)


def test_repeated_blob_counts_towards_projection_budget(tmp_path: Path) -> None:
    reader = open_reader(make_repo(tmp_path, {"a.py": b"1234", "b.py": b"1234"}), Limits(total_bytes=7))
    with pytest.raises(ReadError, match="projection byte budget"):
        prepare_projection(reader, reader.snapshot.identity, reader.tree)


def test_submodule_entry_rejected(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, {"a.py": b"x"})
    subprocess.run(
        [str(repo[2]), "-C", str(repo[0]), "update-index", "--add", "--cacheinfo", "160000", repo[1], "module"],
        check=True,
    )
    subprocess.run([str(repo[2]), "-C", str(repo[0]), "commit", "-m", "module"], check=True, stdout=subprocess.DEVNULL)
    sha = subprocess.check_output([str(repo[2]), "-C", str(repo[0]), "rev-parse", "HEAD"]).decode().strip()
    reader = open_reader((repo[0], sha, repo[2]))
    with pytest.raises(ReadError, match="unsupported"):
        prepare_projection(reader, reader.snapshot.identity, reader.tree)


def test_source_policy_excludes_sensitive_and_non_source_files(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path, {"a.py": b"pass\n", ".env": b"SECRET=hidden", "id_rsa": b"secret", "notes.txt": b"ignored"}
    )
    reader = open_reader(repo)
    result = prepare_projection(reader, reader.snapshot.identity, reader.tree)
    assert [f.path for f in result.files] == ["a.py"]
    assert result.coverage == Coverage.PARTIAL
    assert len(result.issues) == 4
    assert all("hidden" not in issue for issue in result.issues)


def test_private_key_marker_in_source_is_rejected(tmp_path: Path) -> None:
    reader = open_reader(make_repo(tmp_path, {"a.py": b"-----BEGIN PRIVATE KEY-----\ntest"}))
    with pytest.raises(ReadError, match="private key"):
        prepare_projection(reader, reader.snapshot.identity, reader.tree)


def test_credential_assignment_in_source_is_rejected(tmp_path: Path) -> None:
    reader = open_reader(make_repo(tmp_path, {"a.py": b"api_key = 'example-credential'\n"}))
    with pytest.raises(ReadError, match="possible credential"):
        prepare_projection(reader, reader.snapshot.identity, reader.tree)


def test_projection_stream_is_bounded_and_digest_matches_wire(tmp_path: Path) -> None:
    original = b"# " + b"x" * 150_000 + b"\n"
    repo = make_repo(tmp_path, {"a.py": original, "z.py": b"import a\n", ".env": b"not-for-cell"})
    (repo[0] / "a.py").write_bytes(b"uncommitted")
    reader = open_reader(repo)
    manifest = prepare_projection(reader, reader.snapshot.identity, reader.tree)
    encoder = ProjectionEncoder(reader, manifest)
    chunks = list(encoder.chunks())
    wire = b"".join(chunks)
    assert max(map(len, chunks)) <= 65536
    assert original in wire and b"not-for-cell" not in wire and b"uncommitted" not in wire
    assert encoder.receipt is not None
    assert encoder.receipt.sha256 == hashlib.sha256(wire).hexdigest()
    assert encoder.receipt.byte_length == len(wire) and encoder.receipt.file_count == 2
    with pytest.raises(ReadError, match="already used"):
        list(encoder.chunks())


def test_projection_stream_rejects_modified_manifest_and_byte_limit(tmp_path: Path) -> None:
    reader = open_reader(make_repo(tmp_path, {"a.py": b"print(1)\n"}))
    manifest = prepare_projection(reader, reader.snapshot.identity, reader.tree)
    changed = replace(manifest, files=(replace(manifest.files[0], sha256="0" * 64),))
    encoder = ProjectionEncoder(reader, changed)
    with pytest.raises(ReadError, match="manifest mismatch"):
        list(encoder.chunks())
    assert encoder.receipt is None
    limited = ProjectionEncoder(reader, manifest, max_wire_bytes=1)
    with pytest.raises(ReadError, match="wire limit"):
        list(limited.chunks())
    assert limited.receipt is None


def test_projection_stream_interruption_never_produces_receipt(tmp_path: Path) -> None:
    reader = open_reader(make_repo(tmp_path, {"a.py": b"x" * 100_000}))
    manifest = prepare_projection(reader, reader.snapshot.identity, reader.tree)
    encoder = ProjectionEncoder(reader, manifest)
    stream = encoder.chunks()
    assert next(stream)
    stream.close()
    assert encoder.receipt is None
