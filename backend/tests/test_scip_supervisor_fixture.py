"""Opt-in real scip-python fixture in a disposable Docker Cell.

The image and temporary data are test-only. This does not exercise the future
authenticated brokers, canonical Execution authority or a separate-kernel VM.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from aiagent_development.code_intelligence.artifact_capture import capture_scip
from aiagent_development.code_intelligence.git_proof import build_git_tree_proof
from aiagent_development.code_intelligence.git_reader import GitSnapshot
from aiagent_development.code_intelligence.indexer_recipe import python_scip_argv
from aiagent_development.code_intelligence.models import AuthorizedSnapshot, Identity
from aiagent_development.code_intelligence.models import Limits as GitLimits
from aiagent_development.code_intelligence.projection import prepare_projection
from aiagent_development.code_intelligence.projection_stage import cell_source_traversal, stage_git_projection
from aiagent_development.code_intelligence.projection_stream import ProjectionEncoder
from aiagent_development.code_intelligence.scip_navigation import (
    IndexManifest,
    IndexStatus,
    SourceProvenance,
    load_index,
)
from aiagent_domain import uuid7
from aiagent_sandbox.grant import Grant, sign
from aiagent_sandbox.supervisor import Limits, Supervisor

resource = pytest.importorskip("resource")

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("AIAGENT_SCIP_FIXTURE") != "1" or os.geteuid() != 0,
    reason="requires the pinned offline SCIP fixture image and privileged Supervisor",
)


def test_real_scip_indexer_runs_through_supervisor_on_staged_source(tmp_path: Path) -> None:
    git = shutil.which("git")
    assert git
    root = tmp_path / "repo"
    root.mkdir()

    def git_run(*args: str) -> bytes:
        return subprocess.check_output([git, "-C", str(root), *args], stderr=subprocess.DEVNULL)

    git_run("init")
    git_run("config", "user.name", "Fixture")
    git_run("config", "user.email", "fixture@example.invalid")
    (root / "library.py").write_bytes(b"def greet(name: str) -> str:\n    return 'hello ' + name\n")
    (root / "main.py").write_bytes(b"from library import greet\nresult = greet('world')\n")
    git_run("add", ".")
    git_run("commit", "-m", "synthetic fixture")
    commit = git_run("rev-parse", "HEAD").decode().strip()
    (root / "main.py").write_bytes(b"uncommitted change must not appear\n")
    snapshot = GitSnapshot(
        AuthorizedSnapshot(Identity("tenant", "owner", "fixture", "revision", commit), root),
        Path(git),
        GitLimits(total_seconds=180, process_seconds=10),
    )
    projection = prepare_projection(snapshot, snapshot.snapshot.identity, snapshot.tree)
    proof = build_git_tree_proof(snapshot)
    encoder = ProjectionEncoder(snapshot, projection)
    wire = b"".join(encoder.chunks())
    assert encoder.receipt is not None
    with tempfile.TemporaryDirectory(prefix="codex-scip-cell-", dir="/tmp") as accessible:
        fixture_root = Path(accessible)
        os.chown(fixture_root, -1, 20001)
        fixture_root.chmod(0o710)
        parent = fixture_root / "private"
        parent.mkdir(mode=0o700)
        capture_parent = fixture_root / "capture"
        capture_parent.mkdir(mode=0o700)
        supervisor = Supervisor(
            fixture_root / "cells",
            b"g" * 32,
            limits=Limits(cpu_s=60, memory_bytes=8 * 1024**3, nproc=64),
            require_netns=True,
            cell_uid=20001,
            cell_gid=20001,
        )
        workspace, _ = supervisor.workspace("tenant", "task", ".")
        sources = tuple(SourceProvenance(f.path, f.blob_id, f.sha256) for f in projection.files)
        with stage_git_projection(
            parent,
            io.BytesIO(wire),
            projection,
            encoder.receipt,
            proof,
            snapshot.snapshot.identity,
            deadline=time.monotonic() + 60,
        ) as staged:
            fd = staged.root_fd
            assert staged.check.collision_resistance == "LEGACY_SHA1"
            original_gid = parent.stat().st_gid
            with cell_source_traversal(parent, cell_gid=20001):
                source_path = os.readlink(f"/proc/self/fd/{fd}")
                probe = [
                    sys.executable,
                    "-c",
                    "from pathlib import Path\n"
                    f"p=Path({source_path!r}) / 'library.py'\n"
                    "try:\n p.read_bytes()\n"
                    "except PermissionError: print('DENIED')\n"
                    "else: print('CROSS_TASK_READ')\n",
                ]
                probe_grant = sign(
                    b"g" * 32,
                    Grant(
                        uuid.uuid4().hex,
                        "tenant",
                        "other-task",
                        "run",
                        "intent",
                        1,
                        tuple(probe),
                        ".",
                        5000,
                        512 * 1024**2,
                        int(time.time()) + 60,
                        attempt_id=uuid.uuid4().hex,
                        attempt_binding_version=1,
                    ),
                )
                denied = asyncio.run(supervisor.execute(probe_grant, probe, "."))
                assert denied["status"] == "EXITED" and denied["exitCode"] == 0, denied
                assert "DENIED" in denied["stdout"] and "CROSS_TASK_READ" not in denied["stdout"]
                for attempt in range(2):
                    output_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        argv = list(python_scip_argv(fd, output_fd))
                        token = sign(
                            b"g" * 32,
                            Grant(
                                uuid.uuid4().hex,
                                "tenant",
                                "task",
                                "run",
                                "intent",
                                1,
                                tuple(argv),
                                ".",
                                40000,
                                8 * 1024**3,
                                int(time.time()) + 60,
                                attempt_id=uuid.uuid4().hex,
                                attempt_binding_version=1,
                                execution_binding_version=1,
                                execution_id=uuid7(),
                                execution_binding_sha256=hashlib.sha256(wire).hexdigest(),
                            ),
                        )
                        before = resource.getrusage(resource.RUSAGE_CHILDREN)
                        result = asyncio.run(supervisor._execute(token, argv, ".", source_fd=fd, output_fd=output_fd))
                        after = resource.getrusage(resource.RUSAGE_CHILDREN)
                        if result.get("exitCode") != 0:
                            print("fixture diagnostic:", result.get("stderr"))
                        assert result["status"] == "EXITED" and result["exitCode"] == 0, (
                            result["status"],
                            result.get("exitCode"),
                            result.get("stderr"),
                        )
                        assert not result["timedOut"] and not result["truncated"], result
                        with capture_scip(
                            output_fd, capture_parent, expected_uid=20001, deadline=time.monotonic() + 10
                        ) as artifact:
                            index = os.read(artifact.fd, artifact.byte_length + 1)
                            assert hashlib.sha256(index).hexdigest() == artifact.sha256
                            manifest = IndexManifest(
                                snapshot.snapshot.identity,
                                snapshot.tree,
                                artifact.sha256,
                                "scip-python",
                                "0.6.8",
                                0,
                                sources,
                            )
                            started = time.monotonic()
                            loaded = load_index(snapshot, manifest, index)
                            consumer_ms = (time.monotonic() - started) * 1000
                            assert (
                                loaded.status in (IndexStatus.PARTIAL, IndexStatus.VALIDATED)
                                and loaded.graph is not None
                            ), loaded
                            symbol = "scip-python python dev13-workspace 1.0 library/greet()."
                            assert loaded.graph.definitions(symbol)[0].path == "library.py"
                            assert {item.path for item in loaded.graph.references(symbol)} == {"main.py"}
                            assert loaded.graph.provenance_trust == "UNVERIFIED_INDEXER_ORIGIN"
                            print(
                                f"fixture attempt={attempt + 1} wall_ms={result['wallMs']} "
                                f"cpu_s={(after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime):.3f} "
                                f"child_peak_rss_kib={after.ru_maxrss} artifact_bytes={artifact.byte_length} "
                                f"consumer_ms={consumer_ms:.1f} sources={len(projection.files)} "
                                f"occurrences={len(loaded.graph.locations)} status={loaded.status}"
                            )
                    finally:
                        os.close(output_fd)
                    (workspace / "index.scip").unlink()
            assert parent.stat().st_mode & 0o777 == 0o700 and parent.stat().st_gid == original_gid
            for item in projection.files:
                file_fd = os.open(item.path, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    assert hashlib.sha256(os.read(file_fd, item.size + 1)).hexdigest() == item.sha256
                finally:
                    os.close(file_fd)
        assert not list(parent.iterdir())
