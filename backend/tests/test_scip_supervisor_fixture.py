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
    from aiagent_core.rpc import RpcClient
    from aiagent_development.code_intelligence.indexer_recipe import (
        approved_recipe_digest,
        measure_python_scip_toolchain,
        python_scip_recipe_digest,
    )
    from aiagent_development.code_intelligence.projection import projection_manifest_digest
    from aiagent_domain.rpcwire import RpcError
    from aiagent_gateways.source_transfer import register_source, send_source_for_capture
    from aiagent_sandbox.scip_authority import issue_index_grant
    from aiagent_sandbox.scip_execution import scip_registration_factory
    from aiagent_sandbox.source_rpc import SourceSessions
    from aiagent_sandbox.source_ticket import sign_source_ticket
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
    identity = Identity("tenant", "owner", "fixture", "1", git_run("rev-parse", "HEAD").decode().strip())
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
        gateway_parent = root / "gateway"
        for parent in (source_parent, output_parent, capture_parent, gateway_parent):
            parent.mkdir(mode=0o700)
        sup = Supervisor(
            root / "cells",
            b"g" * 32,
            limits=Limits(cpu_s=60, memory_bytes=8 * 1024**3, nproc=64),
            require_netns=True,
            cell_uid=20001,
            cell_gid=20001,
        )
        execution, attempt, session_id = uuid7(), uuid7(), uuid7()
        snapshot_id, recipe_id = uuid7(), uuid7()
        plan_sha, request_sha, toolchain_sha = "a" * 64, "b" * 64, measure_python_scip_toolchain()
        image_digest = "sha256:" + "d" * 64
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
            "snapshot": snapshot_id,
            "recipe_id": recipe_id,
            "repository_binding": identity.binding_id,
            "workspace_revision": 1,
            "commit": identity.commit,
            "tree": manifest.tree,
            "source_sha256": encoder.receipt.sha256,
            "manifest_sha256": projection_manifest_digest(manifest),
            "test_id": None,
            "plan_sha256": plan_sha,
            "request_sha256": request_sha,
            "toolchain_sha256": toolchain_sha,
            "image_digest": image_digest,
            "recipe_sha256": approved_recipe_digest(
                kind="index",
                test_id=None,
                plan_sha256=plan_sha,
                request_sha256=request_sha,
                template_sha256=python_scip_recipe_digest(),
                toolchain_sha256=toolchain_sha,
                image_digest=image_digest,
            ),
        }
        results: list[tuple[bytes, bytes]] = []
        payload = {
            "version": 1,
            "direction": "gateway-to-supervisor",
            "session": session_id,
            "tenant": "tenant",
            "owner": "owner",
            "task": "task",
            "run": "run",
            "goal": 1,
            "lease": 1,
            "execution": execution,
            "attempt": attempt,
            "snapshot": snapshot_id,
            "recipe": recipe_id,
            "binding_sha256": sha256_hex(binding),
            "repository_binding": identity.binding_id,
            "revision": identity.revision,
            "commit": identity.commit,
            "tree": manifest.tree,
            "manifest_sha256": projection_manifest_digest(manifest),
            "source_sha256": encoder.receipt.sha256,
            "source_bytes": encoder.receipt.byte_length,
            "expires_at": int(time.time()) + 40,
        }
        ticket = sign_source_ticket(b"t" * 32, payload)
        grant = issue_index_grant(b"t" * 32, b"g" * 32, ticket, binding)

        async def exercise() -> None:
            deadline = time.monotonic() + 60
            sessions = SourceSessions(
                source_parent,
                ticket_key=b"t" * 32,
                registration_factory=scip_registration_factory(
                    sup,
                    source_parent,
                    output_parent,
                    capture_parent,
                    b"r" * 32,
                    heartbeat_seconds=5,
                ),
            )
            endpoint = f"unix:{root / 'supervisor.sock'}"
            task = asyncio.create_task(serve(sup, {"tool_gateway": b"k" * 32}, endpoint, source_sessions=sessions))
            heartbeat = None
            try:
                for _ in range(200):
                    if (root / "supervisor.sock").exists():
                        break
                    await asyncio.sleep(0.01)
                with pytest.raises(RpcError):
                    await register_source(
                        endpoint,
                        b"k" * 32,
                        b"t" * 32,
                        ticket,
                        manifest,
                        git_proof,
                        deadline=deadline,
                        execution_binding=binding,
                        grant={**grant, "mac": "0" * 64},
                    )
                with pytest.raises(RpcError):
                    await register_source(
                        endpoint,
                        b"k" * 32,
                        b"t" * 32,
                        ticket,
                        manifest,
                        git_proof,
                        deadline=deadline,
                        execution_binding={**binding, "toolchain_sha256": "f" * 64},
                        grant=grant,
                    )
                wrong_toolchain = "f" * 64
                wrong_binding = {
                    **binding,
                    "toolchain_sha256": wrong_toolchain,
                    "recipe_sha256": approved_recipe_digest(
                        kind="index",
                        test_id=None,
                        plan_sha256=plan_sha,
                        request_sha256=request_sha,
                        template_sha256=python_scip_recipe_digest(),
                        toolchain_sha256=wrong_toolchain,
                        image_digest=image_digest,
                    ),
                }
                wrong_ticket = sign_source_ticket(
                    b"t" * 32, {**payload, "session": uuid7(), "binding_sha256": sha256_hex(wrong_binding)}
                )
                wrong_grant = issue_index_grant(b"t" * 32, b"g" * 32, wrong_ticket, wrong_binding)
                with pytest.raises(RpcError):
                    await register_source(
                        endpoint,
                        b"k" * 32,
                        b"t" * 32,
                        wrong_ticket,
                        manifest,
                        git_proof,
                        deadline=deadline,
                        execution_binding=wrong_binding,
                        grant=wrong_grant,
                    )
                sid, transfer_key, source_binding = await register_source(
                    endpoint,
                    b"k" * 32,
                    b"t" * 32,
                    ticket,
                    manifest,
                    git_proof,
                    deadline=deadline,
                    execution_binding=binding,
                    grant=grant,
                )
                scope = {
                    "tenantId": "tenant",
                    "sessionId": sid,
                    "executionId": execution,
                    "attemptId": attempt,
                    "bindingSha256": sha256_hex(binding),
                }

                async def pulse() -> None:
                    while True:
                        result = await RpcClient(endpoint, "tool_gateway", b"k" * 32).call("source.heartbeat", scope)
                        if not result.get("renewed"):
                            return
                        await asyncio.sleep(0.1)

                first = await RpcClient(endpoint, "tool_gateway", b"k" * 32).call("source.heartbeat", scope)
                assert first == {"renewed": True}
                heartbeat = asyncio.create_task(pulse())
                async with send_source_for_capture(
                    endpoint,
                    b"k" * 32,
                    sid,
                    transfer_key,
                    source_binding,
                    chunks,
                    gateway_parent,
                    deadline=deadline,
                    active=lambda: True,
                ) as (artifact_fd, signed_receipt):
                    results.append((os.read(artifact_fd, 4 * 1024 * 1024 + 1), signed_receipt))
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
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
        assert (
            not list(source_parent.iterdir())
            and not list(output_parent.iterdir())
            and not list(gateway_parent.iterdir())
        )


def test_source_startup_rejects_unsafe_isolation_and_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from aiagent_sandbox.supervisor import _configured_source_sessions

    root = tmp_path / "source-root"
    root.mkdir(mode=0o700)
    os.chown(root, -1, 20001)
    root.chmod(0o710)
    for name in ("source", "output", "capture", "journal", "keys"):
        (root / name).mkdir(mode=0o700)
    key_paths = {}
    for name, value in (("grant", b"g"), ("rpc", b"r"), ("ticket", b"t"), ("capture", b"c")):
        path = root / "keys" / name
        path.write_text((value * 32).hex())
        path.chmod(0o400)
        key_paths[name] = path
    for name, value in {
        "AIAGENT_SANDBOX_SOURCE_MODE": "enabled",
        "AIAGENT_SANDBOX_SOURCE_ROOT": str(root),
        "AIAGENT_SANDBOX_REPLAY_JOURNAL": str(root / "journal" / "consumed"),
        "AIAGENT_SANDBOX_SOURCE_TICKET_KEY_FILE": str(key_paths["ticket"]),
        "AIAGENT_SANDBOX_CAPTURE_KEY_FILE": str(key_paths["capture"]),
        "AIAGENT_SANDBOX_GRANT_KEY_FILE": str(key_paths["grant"]),
        "AIAGENT_SANDBOX_RPC_KEY_FILE": str(key_paths["rpc"]),
    }.items():
        monkeypatch.setenv(name, value)
    sup = Supervisor(tmp_path / "cells-config", b"g" * 32, require_netns=False, cell_uid=20001, cell_gid=20001)
    with pytest.raises(RuntimeError, match="isolation unavailable"):
        _configured_source_sessions(sup, b"r" * 32)
    sup.require_netns = True
    key_paths["ticket"].chmod(0o644)
    with pytest.raises(RuntimeError, match="key unavailable"):
        _configured_source_sessions(sup, b"r" * 32)
    key_paths["ticket"].chmod(0o400)
    key_paths["capture"].chmod(0o600)
    key_paths["capture"].write_text((b"t" * 32).hex())
    with pytest.raises(RuntimeError, match="keys must be distinct"):
        _configured_source_sessions(sup, b"r" * 32)
