"""Opt-in Docker Git worker fixture; no canonical Gateway authority is simulated."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from aiagent_core.rpc import RpcClient
from aiagent_domain import uuid7
from aiagent_gateways.git_object_verifier import RpcGitObjectVerifier
from aiagent_gateways.git_patch_artifacts import VerifiedGitPatch
from aiagent_sandbox.binary_transfer import encode_chunks
from aiagent_sandbox.git_worker_rpc import (
    git_transfer_binding,
    git_transfer_key,
    sign_git_worker_grant,
    verify_git_worker_result,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("AIAGENT_GIT_WORKER_E2E") != "1",
    reason="opt-in Docker Git worker fixture",
)


def test_git_worker_in_networkless_docker(tmp_path: Path):
    git = shutil.which("git")
    assert git is not None
    backend = Path(__file__).resolve().parents[1]
    repo, journal, bridge = (tmp_path / name for name in ("repo", "journal", "bridge"))
    for path in (repo, journal, bridge):
        path.mkdir(mode=0o700)

    def run(*args: str) -> bytes:
        return subprocess.check_output([git, "-C", str(repo), *args], stderr=subprocess.DEVNULL)

    run("init", "-b", "feat/fixture")
    run("config", "user.name", "Fixture")
    run("config", "user.email", "fixture@example.invalid")
    (repo / "a.txt").write_text("before\n", encoding="utf-8")
    run("add", "a.txt")
    run("commit", "-m", "base")
    parent = run("rev-parse", "HEAD").decode().strip()
    (repo / "a.txt").write_text("after\n", encoding="utf-8")
    patch = run("diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--no-renames")
    tenant, binding, operation, intent, attempt = (uuid7() for _ in range(5))
    payload = {
        "operation": operation,
        "tenant": tenant,
        "owner": uuid7(),
        "task": uuid7(),
        "run": uuid7(),
        "workspace": uuid7(),
        "goal": 1,
        "lease": 1,
        "restore": 1,
        "worker_ref": "fixture",
        "intent": intent,
        "attempt": attempt,
        "effect_key": tenant + ":" + intent,
        "binding": binding,
        "patch_id": uuid7(),
        "cycle_id": "fixture",
        "plan_ref": "fixture",
        "generation": 1,
        "branch": "feat/fixture",
        "parent": parent,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "patch_bytes": len(patch),
        "paths": ["a.txt"],
        "message": "fixture",
        "request_sha256": "a" * 64,
        "expires_at": int(time.time()) + 50,
    }
    name = "aiagent-git-worker-fixture-" + secrets.token_hex(5)
    image = "codex-dev13-scip-cell-fixture:latest"
    result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "32",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{backend}:/w:ro",
            "-v",
            f"{repo}:/repo:rw",
            "-v",
            f"{journal}:/journal:rw",
            "-v",
            f"{bridge}:/bridge:rw",
            "-e",
            "AIAGENT_PROFILE=test",
            "-e",
            f"FIXTURE_TENANT={tenant}",
            "-e",
            f"FIXTURE_BINDING={binding}",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "PYTHONPATH=/w/.linuxdeps:/w/packages/domain/src:/w/packages/core/src:/w/packages/development/src:/w/packages/sandbox/src",
            image,
            "python",
            "/w/packages/sandbox/tests/fixtures/serve_git_worker.py",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    try:

        async def exercise():
            socket = bridge / "worker.sock"
            for _ in range(200):
                if socket.exists():
                    break
                await asyncio.sleep(0.1)
            assert socket.exists(), subprocess.run(["docker", "logs", name], capture_output=True, text=True).stderr
            client = RpcClient("unix:" + str(socket), "tool_gateway", b"r" * 32)
            await client.call("git.begin", {"grant": sign_git_worker_grant(b"g" * 32, payload)})
            frames = list(encode_chunks(git_transfer_key(b"t" * 32, operation), git_transfer_binding(payload), [patch]))
            await client.call("git.chunk", {"operation": operation, "frame": base64.b64encode(frames[0]).decode()})
            response = await client.call(
                "git.execute", {"operation": operation, "end": base64.b64encode(frames[1]).decode()}
            )
            verified = verify_git_worker_result(b"s" * 32, response, payload, "fixture-worker")
            verifier = RpcGitObjectVerifier("unix:" + str(socket), b"r" * 32, b"g" * 32, "fixture-worker")
            patch_ref = VerifiedGitPatch(payload["patch_id"], parent, payload["patch_sha256"], ("a.txt",), patch)
            await asyncio.to_thread(verifier.verify, verified, patch_ref, deadline=time.monotonic() + 10)
            assert run("rev-parse", "HEAD").decode().strip() == verified["commit"]
            assert run("rev-parse", verified["commit"] + "^").decode().strip() == parent
            assert (journal / (operation + ".prepared")).is_file()
            assert (journal / (operation + ".confirmed")).is_file()

        asyncio.run(exercise())
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
