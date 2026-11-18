"""Opt-in real scip-python fixture in a disposable Docker Cell.

The image and temporary data are test-only. This does not exercise the future
authenticated brokers, canonical Execution authority or a separate-kernel VM.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
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
from aiagent_domain import canonical_json, sha256_hex, uuid7
from aiagent_sandbox.grant import Grant, sign
from aiagent_sandbox.projected_output import projected_output
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
        output_parent = fixture_root / "output"
        output_parent.mkdir(mode=0o700)
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
                    execution = uuid7()
                    attempt_id = uuid.uuid4().hex
                    binding = {
                        "execution": execution,
                        "attempt": attempt_id,
                        "intent": "intent",
                        "tenant": "tenant",
                        "owner": "owner",
                        "task": "task",
                        "run": "run",
                        "goal_revision": 1,
                        "lease_epoch": 1,
                    }
                    provisional = Grant(
                        uuid.uuid4().hex,
                        "tenant",
                        "task",
                        "run",
                        "intent",
                        1,
                        (),
                        ".",
                        40000,
                        8 * 1024**3,
                        int(time.time()) + 60,
                        attempt_id=attempt_id,
                        attempt_binding_version=1,
                        execution_binding_version=1,
                        execution_id=execution,
                        execution_binding_sha256=sha256_hex(binding),
                    )
                    with projected_output(output_parent, provisional, cell_uid=20001, cell_gid=20001) as output_fd:
                        argv = list(python_scip_argv(fd, output_fd))
                        token = sign(b"g" * 32, replace(provisional, argv=tuple(argv)))

                        async def run_indexer():
                            async with supervisor._execute_and_capture(
                                token,
                                argv,
                                ".",
                                source_fd=fd,
                                output_parent=output_parent,
                                prepared_output_fd=output_fd,
                                source_cwd=True,
                                binding=binding,
                                receipt_key=b"r" * 32,
                                private_parent=capture_parent,
                                deadline=time.monotonic() + 60,
                                heartbeat_seconds=10,
                                revoked=asyncio.Event(),
                            ) as (result, captured_fd, proof):
                                return result, os.read(captured_fd, 4 * 1024 * 1024 + 1), proof

                        before = resource.getrusage(resource.RUSAGE_CHILDREN)
                        result, index, capture_proof = asyncio.run(run_indexer())
                        after = resource.getrusage(resource.RUSAGE_CHILDREN)
                        assert result["status"] == "EXITED" and result["exitCode"] == 0
                        assert not result["timedOut"] and not result["truncated"]
                        envelope = json.loads(capture_proof)
                        expected = hmac.new(
                            b"r" * 32, canonical_json(envelope["payload"]).encode(), hashlib.sha256
                        ).hexdigest()
                        assert hmac.compare_digest(envelope["mac"], expected)
                        digest = hashlib.sha256(index).hexdigest()
                        assert envelope["payload"]["execution"] == execution
                        assert envelope["payload"]["artifact_sha256"] == digest
                        manifest = IndexManifest(
                            snapshot.snapshot.identity,
                            snapshot.tree,
                            digest,
                            "scip-python",
                            "0.6.8",
                            0,
                            sources,
                        )
                        started = time.monotonic()
                        loaded = load_index(snapshot, manifest, index)
                        consumer_ms = (time.monotonic() - started) * 1000
                        assert (
                            loaded.status in (IndexStatus.PARTIAL, IndexStatus.VALIDATED) and loaded.graph is not None
                        ), loaded
                        symbol = "scip-python python dev13-workspace 1.0 library/greet()."
                        assert loaded.graph.definitions(symbol)[0].path == "library.py"
                        assert {item.path for item in loaded.graph.references(symbol)} == {"main.py"}
                        assert loaded.graph.provenance_trust == "UNVERIFIED_INDEXER_ORIGIN"
                        print(
                            f"fixture attempt={attempt + 1} wall_ms={result['wallMs']} "
                            f"cpu_s={(after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime):.3f} "
                            f"child_peak_rss_kib={after.ru_maxrss} artifact_bytes={len(index)} "
                            f"consumer_ms={consumer_ms:.1f} sources={len(projection.files)} "
                            f"occurrences={len(loaded.graph.locations)} status={loaded.status}"
                        )
                    assert not list(output_parent.iterdir())
            assert parent.stat().st_mode & 0o777 == 0o700 and parent.stat().st_gid == original_gid
            for item in projection.files:
                file_fd = os.open(item.path, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    assert hashlib.sha256(os.read(file_fd, item.size + 1)).hexdigest() == item.sha256
                finally:
                    os.close(file_fd)
        assert not list(parent.iterdir())


def test_source_rpc_to_real_supervisor_and_signed_scip_capture(tmp_path: Path) -> None:
    """Exercise the real data path with test-only session registration and keys.

    This is not canonical Authority, an Artifact broker, or a production E2E.
    """
    from aiagent_gateways.source_transfer import send_source
    from aiagent_sandbox.source_rpc import SourceSession, SourceSessions
    from aiagent_sandbox.supervisor import serve

    git = shutil.which("git")
    assert git
    repo = tmp_path / "repo_rpc"
    repo.mkdir()

    def git_run(*args: str) -> bytes:
        return subprocess.check_output([git, "-C", str(repo), *args], stderr=subprocess.DEVNULL)

    git_run("init")
    git_run("config", "user.name", "Fixture")
    git_run("config", "user.email", "fixture@example.invalid")
    (repo / "library.py").write_bytes(b"def greet(name: str) -> str:\n    return 'hello ' + name\n")
    (repo / "main.py").write_bytes(b"from library import greet\nresult = greet('world')\n")
    git_run("add", ".")
    git_run("commit", "-m", "synthetic fixture")
    identity = Identity("tenant", "owner", "fixture", "revision", git_run("rev-parse", "HEAD").decode().strip())
    snapshot = GitSnapshot(AuthorizedSnapshot(identity, repo), Path(git), GitLimits(total_seconds=180))
    manifest = prepare_projection(snapshot, identity, snapshot.tree)
    git_proof = build_git_tree_proof(snapshot)
    encoder = ProjectionEncoder(snapshot, manifest)
    chunks = list(encoder.chunks())
    assert encoder.receipt is not None

    with tempfile.TemporaryDirectory(prefix="codex-scip-rpc-", dir="/tmp") as accessible:
        root = Path(accessible)
        os.chown(root, -1, 20001)
        root.chmod(0o710)
        source_parent, output_parent, capture_parent = (root / name for name in ("source", "output", "capture"))
        for parent in (source_parent, output_parent, capture_parent):
            parent.mkdir(mode=0o700)
        sup = Supervisor(
            root / "cells",
            b"g" * 32,
            limits=Limits(cpu_s=60, memory_bytes=8 * 1024**3, nproc=64),
            require_netns=True,
            cell_uid=20001,
            cell_gid=20001,
        )
        execution, attempt = uuid7(), uuid.uuid4().hex
        binding = {
            "execution": execution,
            "attempt": attempt,
            "intent": "intent",
            "tenant": "tenant",
            "owner": "owner",
            "task": "task",
            "run": "run",
            "goal_revision": 1,
            "lease_epoch": 1,
        }
        results: list[tuple[bytes, bytes]] = []

        async def staged(fd: int) -> None:
            provisional = Grant(
                uuid.uuid4().hex,
                "tenant",
                "task",
                "run",
                "intent",
                1,
                (),
                ".",
                40000,
                8 * 1024**3,
                int(time.time()) + 60,
                attempt_id=attempt,
                attempt_binding_version=1,
                execution_binding_version=1,
                execution_id=execution,
                execution_binding_sha256=sha256_hex(binding),
            )
            with cell_source_traversal(source_parent, cell_gid=20001):
                with projected_output(output_parent, provisional, cell_uid=20001, cell_gid=20001) as output_fd:
                    argv = list(python_scip_argv(fd, output_fd))
                    token = sign(b"g" * 32, replace(provisional, argv=tuple(argv)))
                    async with sup._execute_and_capture(
                        token,
                        argv,
                        ".",
                        source_fd=fd,
                        output_parent=output_parent,
                        prepared_output_fd=output_fd,
                        source_cwd=True,
                        binding=binding,
                        receipt_key=b"r" * 32,
                        private_parent=capture_parent,
                        deadline=time.monotonic() + 60,
                        heartbeat_seconds=10,
                        revoked=asyncio.Event(),
                    ) as (result, captured_fd, signed_receipt):
                        assert result["status"] == "EXITED" and result["exitCode"] == 0
                        results.append((os.read(captured_fd, 4 * 1024 * 1024 + 1), signed_receipt))

        async def exercise() -> None:
            deadline = time.monotonic() + 60
            session = SourceSession(
                uuid.uuid4().hex,
                execution,
                attempt,
                sha256_hex(binding),
                "snapshot",
                b"t" * 32,
                manifest,
                encoder.receipt,
                git_proof,
                identity,
                deadline,
                lambda: True,
                staged,
            )
            sessions = SourceSessions(source_parent)
            sessions.register(session)
            endpoint = f"unix:{root / 'supervisor.sock'}"
            task = asyncio.create_task(serve(sup, {"tool_gateway": b"k" * 32}, endpoint, source_sessions=sessions))
            try:
                for _ in range(200):
                    if (root / "supervisor.sock").exists():
                        break
                    await asyncio.sleep(0.01)
                await send_source(
                    endpoint,
                    b"k" * 32,
                    session.session_id,
                    session.key,
                    session.transfer_binding(),
                    chunks,
                    deadline=deadline,
                )
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        asyncio.run(exercise())
        assert len(results) == 1
        index, signed_receipt = results[0]
        envelope = json.loads(signed_receipt)
        mac = hmac.new(b"r" * 32, canonical_json(envelope["payload"]).encode(), hashlib.sha256).hexdigest()
        assert hmac.compare_digest(envelope["mac"], mac)
        assert envelope["payload"]["execution"] == execution
        assert envelope["payload"]["artifact_sha256"] == hashlib.sha256(index).hexdigest()
        assert 0 < len(index) <= 4 * 1024 * 1024
        index_manifest = IndexManifest(
            identity,
            snapshot.tree,
            hashlib.sha256(index).hexdigest(),
            "scip-python",
            "0.6.8",
            0,
            tuple(SourceProvenance(f.path, f.blob_id, f.sha256) for f in manifest.files),
        )
        loaded = load_index(snapshot, index_manifest, index)
        assert loaded.status in (IndexStatus.PARTIAL, IndexStatus.VALIDATED) and loaded.graph is not None
        symbol = "scip-python python dev13-workspace 1.0 library/greet()."
        assert loaded.graph.definitions(symbol)[0].path == "library.py"
        assert {item.path for item in loaded.graph.references(symbol)} == {"main.py"}
        assert not list(source_parent.iterdir()) and not list(output_parent.iterdir())
