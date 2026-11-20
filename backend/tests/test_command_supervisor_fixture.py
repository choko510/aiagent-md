"""Opt-in actual command Cell fixture; no canonical DB or production adapter claim."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import pytest
from aiagent_core.rpc import RpcClient
from aiagent_development.code_intelligence.git_proof import build_git_tree_proof
from aiagent_development.code_intelligence.git_reader import GitSnapshot
from aiagent_development.code_intelligence.indexer_recipe import approved_recipe_digest, measure_command_toolchain
from aiagent_development.code_intelligence.models import AuthorizedSnapshot, Identity
from aiagent_development.code_intelligence.projection import prepare_projection, projection_manifest_digest
from aiagent_development.code_intelligence.projection_stream import ProjectionEncoder
from aiagent_domain import sha256_hex, uuid7
from aiagent_gateways.source_transfer import register_source, send_source_for_capture
from aiagent_sandbox.command_authority import command_template_digest, issue_command_grant
from aiagent_sandbox.command_execution import command_registration_factory
from aiagent_sandbox.command_receipt import verify_command_receipt
from aiagent_sandbox.grant import sign, verify
from aiagent_sandbox.source_rpc import SourceSessions
from aiagent_sandbox.source_ticket import sign_source_ticket
from aiagent_sandbox.supervisor import Limits, Supervisor, serve

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("AIAGENT_SCIP_FIXTURE") != "1" or os.geteuid() != 0,
    reason="requires disposable privileged Cell fixture",
)


def test_real_approved_commands_produce_scoped_signed_outcomes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIAGENT_PROFILE", "test")
    git = shutil.which("git")
    assert git
    repo = tmp_path / "command-repo"
    repo.mkdir()

    def git_run(*args: str) -> bytes:
        return subprocess.check_output([git, "-C", str(repo), *args], stderr=subprocess.DEVNULL)

    git_run("init")
    git_run("config", "user.name", "Fixture")
    git_run("config", "user.email", "fixture@example.invalid")
    (repo / "app.py").write_text(
        "import pathlib, sys, time\n"
        "mode = sys.argv[1]\n"
        "if mode == 'fail': raise SystemExit(7)\n"
        "if mode == 'timeout': time.sleep(5)\n"
        "if mode == 'overflow': print('x' * 100000, flush=True)\n"
        "if mode == 'readonly':\n"
        " try: pathlib.Path('app.py').write_text('changed')\n"
        " except PermissionError: pass\n"
        " else: raise SystemExit(9)\n"
        "print('sensitive-output')\n"
    )
    git_run("add", ".")
    git_run("commit", "-m", "fixture")
    commit = git_run("rev-parse", "HEAD").decode().strip()
    identity = Identity("tenant", "owner", "repository", "1", commit)
    snapshot = GitSnapshot(AuthorizedSnapshot(identity, repo), Path(git))
    manifest = prepare_projection(snapshot, identity, snapshot.tree)
    git_proof = build_git_tree_proof(snapshot)
    encoder = ProjectionEncoder(snapshot, manifest)
    chunks = list(encoder.chunks())
    assert encoder.receipt is not None
    binary = "/usr/local/bin/python3.13"
    toolchain = measure_command_toolchain(binary)

    with tempfile.TemporaryDirectory(prefix="codex-command-cell-", dir="/tmp") as directory:
        root = Path(directory)
        os.chown(root, -1, 20001)
        root.chmod(0o710)
        source, output, gateway = (root / name for name in ("source", "output", "gateway"))
        for path in (source, output, gateway):
            path.mkdir(mode=0o700)
        supervisor = Supervisor(
            root / "cells",
            b"g" * 32,
            limits=Limits(cpu_s=60, memory_bytes=2 * 1024**3, nproc=64),
            require_netns=True,
            cell_uid=20001,
            cell_gid=20001,
        )

        async def exercise() -> None:
            sessions = SourceSessions(
                source,
                ticket_key=b"t" * 32,
                registration_factory=command_registration_factory(supervisor, source, output, b"c" * 32, (binary,)),
            )
            endpoint = f"unix:{root / 'supervisor.sock'}"
            server = asyncio.create_task(
                serve(supervisor, {"tool_gateway": b"r" * 32}, endpoint, source_sessions=sessions)
            )
            try:
                for _ in range(300):
                    if (root / "supervisor.sock").exists():
                        break
                    await asyncio.sleep(0.01)
                for kind, mode, test_id in (
                    ("build", "success", None),
                    ("static-check", "readonly", None),
                    ("test", "success", "unit"),
                    ("test", "fail", "failure"),
                    ("test", "timeout", "timeout"),
                    ("test", "overflow", "overflow"),
                ):
                    execution, attempt, session_id, snapshot_id, recipe_id = (uuid7() for _ in range(5))
                    descriptor = {
                        "kind": kind,
                        "test_id": test_id,
                        "argv": [binary, "-B", "app.py", mode],
                        "cwdRelative": ".",
                    }
                    request_sha = sha256_hex(
                        {"tool": "sandbox_exec", "args": {"argv": descriptor["argv"], "cwdRelative": "."}}
                    )
                    recipe_sha = approved_recipe_digest(
                        kind=kind,
                        test_id=test_id,
                        plan_sha256="a" * 64,
                        request_sha256=request_sha,
                        template_sha256=command_template_digest(descriptor),
                        toolchain_sha256=toolchain,
                        image_digest="sha256:" + "b" * 64,
                    )
                    binding = {
                        "execution": execution,
                        "tenant": identity.tenant_id,
                        "owner": identity.owner_id,
                        "task": "task",
                        "run": "run",
                        "workspace": "workspace",
                        "goal_revision": 1,
                        "lease_epoch": 1,
                        "intent": "intent",
                        "attempt": attempt,
                        "snapshot": snapshot_id,
                        "recipe_id": recipe_id,
                        "repository_binding": identity.binding_id,
                        "workspace_revision": 1,
                        "commit": identity.commit,
                        "tree": manifest.tree,
                        "manifest_sha256": projection_manifest_digest(manifest),
                        "source_sha256": encoder.receipt.sha256,
                        "test_id": test_id,
                        "plan_sha256": "a" * 64,
                        "request_sha256": request_sha,
                        "toolchain_sha256": toolchain,
                        "image_digest": "sha256:" + "b" * 64,
                        "recipe_sha256": recipe_sha,
                    }
                    payload = {
                        "version": 1,
                        "direction": "gateway-to-supervisor",
                        "session": session_id,
                        "tenant": identity.tenant_id,
                        "owner": identity.owner_id,
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
                        "revision": "1",
                        "commit": identity.commit,
                        "tree": manifest.tree,
                        "manifest_sha256": projection_manifest_digest(manifest),
                        "source_sha256": encoder.receipt.sha256,
                        "source_bytes": encoder.receipt.byte_length,
                        "expires_at": int(time.time()) + 35,
                    }
                    ticket = sign_source_ticket(b"t" * 32, payload)
                    token = issue_command_grant(b"t" * 32, b"g" * 32, ticket, binding, descriptor)
                    if mode == "timeout":
                        token = sign(b"g" * 32, replace(verify(b"g" * 32, token, descriptor["argv"], "."), wall_ms=200))
                    deadline = time.monotonic() + 30
                    sid, transfer_key, transfer_binding = await register_source(
                        endpoint,
                        b"r" * 32,
                        b"t" * 32,
                        ticket,
                        manifest,
                        git_proof,
                        deadline=deadline,
                        execution_binding=binding,
                        grant=token,
                        command_descriptor=descriptor,
                    )
                    scope = {
                        "tenantId": identity.tenant_id,
                        "sessionId": sid,
                        "executionId": execution,
                        "attemptId": attempt,
                        "bindingSha256": sha256_hex(binding),
                    }
                    first = await RpcClient(endpoint, "tool_gateway", b"r" * 32).call("source.heartbeat", scope)
                    assert first == {"renewed": True}

                    async def pulse() -> None:
                        while True:
                            answer = await RpcClient(endpoint, "tool_gateway", b"r" * 32).call(
                                "source.heartbeat", scope
                            )
                            if not answer.get("renewed"):
                                return
                            await asyncio.sleep(0.1)

                    heartbeat = asyncio.create_task(pulse())
                    try:
                        async with send_source_for_capture(
                            endpoint,
                            b"r" * 32,
                            sid,
                            transfer_key,
                            transfer_binding,
                            chunks,
                            gateway,
                            deadline=deadline,
                            active=lambda: True,
                        ) as (fd, receipt):
                            body = os.read(fd, 16385)
                            assert b"sensitive-output" not in body
                            result = verify_command_receipt(b"c" * 32, body, receipt, binding, descriptor)
                            assert result["kind"] == kind and result["test_id"] == test_id
                            assert result["timed_out"] is (mode == "timeout")
                            assert result["truncated"] is (mode == "overflow")
                            if mode == "fail":
                                assert result["exit_code"] == 7
                            elif mode in ("success", "readonly"):
                                assert result["exit_code"] == 0
                    finally:
                        heartbeat.cancel()
                        await asyncio.gather(heartbeat, return_exceptions=True)
                    assert not list(source.iterdir()) and not list(output.iterdir()) and not list(gateway.iterdir())
            finally:
                server.cancel()
                await asyncio.gather(server, return_exceptions=True)

        asyncio.run(exercise())
    assert hashlib.sha256((repo / "app.py").read_bytes()).hexdigest() == manifest.files[0].sha256
