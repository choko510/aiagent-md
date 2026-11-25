"""Isolated Git RPC contract test; the authority callback is a test fixture."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from aiagent_core.rpc import RpcClient
from aiagent_development.git_commit_worker import IsolatedGitCommitWorker
from aiagent_domain import uuid7
from aiagent_domain.rpcwire import RpcError
from aiagent_gateways.git_object_verifier import RpcGitObjectVerifier
from aiagent_gateways.git_patch_artifacts import VerifiedGitPatch
from aiagent_gateways.git_worker_transfer import register_git_transfer, send_git_patch_frames
from aiagent_sandbox.binary_transfer import encode_chunks
from aiagent_sandbox.git_operation_journal import GitOperationJournal
from aiagent_sandbox.git_worker_rpc import (
    GitWorkerAuthority,
    GitWorkerService,
    build_git_worker_server,
    git_transfer_binding,
    git_transfer_key,
    sign_git_head_grant,
    sign_git_worker_grant,
    verify_git_worker_result,
)


def test_local_git_cas_guard_serializes_watchdog_cancellation():
    authority = GitWorkerAuthority(5)
    payload = {
        "operation": uuid7(),
        "attempt": uuid7(),
        "request_sha256": "a" * 64,
        "expires_at": int(time.time()) + 30,
    }
    assert authority.register(payload) and authority.renew(payload)
    started, finished = threading.Event(), threading.Event()
    outcomes: list[bool] = []

    def cancel() -> None:
        started.set()
        outcomes.append(authority.cancel(payload))
        finished.set()

    with authority.cas_guard(payload):
        thread = threading.Thread(target=cancel)
        thread.start()
        assert started.wait(1) and not finished.wait(0.05)
    thread.join(timeout=1)
    assert finished.is_set() and outcomes == [True] and not authority(payload)


@pytest.mark.skipif(shutil.which("git") is None, reason="Git unavailable")
@pytest.mark.parametrize(
    "scenario", ["normal", "revoked_after_prepare", "bad_transfer", "heartbeat", "heartbeat_cancel", "fence_error"]
)
def test_real_socket_git_worker_uses_scoped_grant_and_durable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
):
    monkeypatch.setenv("AIAGENT_PROFILE", "test")
    git = shutil.which("git")
    assert git is not None
    repo, journal_dir = tmp_path / "repo", tmp_path / "journal"
    repo.mkdir()
    journal_dir.mkdir(mode=0o700)

    def run(*args: str) -> bytes:
        return subprocess.check_output([git, "-C", str(repo), *args], stderr=subprocess.DEVNULL)  # noqa: S603

    run("init", "-b", "feat/fixture")
    run("config", "user.name", "Fixture")
    run("config", "user.email", "fixture@example.invalid")
    (repo / "a.txt").write_text("before\n", encoding="utf-8")
    run("add", "a.txt")
    run("commit", "-m", "base")
    parent = run("rev-parse", "HEAD").decode().strip()
    (repo / "a.txt").write_text("after\n", encoding="utf-8")
    patch = run("diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--no-renames")
    tenant, run_id, intent, attempt, binding, operation = (uuid7() for _ in range(6))
    payload = {
        "operation": operation,
        "tenant": tenant,
        "owner": uuid7(),
        "task": uuid7(),
        "run": run_id,
        "workspace": uuid7(),
        "goal": 1,
        "lease": 1,
        "restore": 1,
        "worker_ref": "fixture-worker",
        "intent": intent,
        "attempt": attempt,
        "effect_key": tenant + ":" + intent,
        "binding": binding,
        "patch_id": uuid7(),
        "cycle_id": "fixture-cycle",
        "plan_ref": "fixture-plan",
        "generation": 2,
        "branch": "feat/fixture",
        "parent": parent,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "patch_bytes": len(patch),
        "paths": ["a.txt"],
        "message": "approved fixture",
        "request_sha256": "a" * 64,
        "expires_at": int(time.time()) + 50,
    }
    journal = GitOperationJournal(journal_dir.resolve(), b"j" * 32)
    worker = IsolatedGitCommitWorker(repo.resolve(), Path(git).resolve(), "feat/fixture")

    def refuse_admission(_payload, _journal):
        raise RuntimeError("CAS admission response lost")

    service = GitWorkerService(
        worker,
        journal,
        tenant=tenant,
        binding=binding,
        branch="feat/fixture",
        grant_key=b"g" * 32,
        transfer_key=b"t" * 32,
        result_key=b"s" * 32,
        worker_id="fixture-worker",
        authority=GitWorkerAuthority(5)
        if scenario.startswith("heartbeat")
        else lambda _p: scenario != "revoked_after_prepare" or not (journal_dir / (operation + ".prepared")).exists(),
        cas_admitter=refuse_admission if scenario == "fence_error" else None,
    )

    async def exercise():
        endpoint = "unix:" + str(tmp_path / "worker.sock")
        server = build_git_worker_server(endpoint, b"r" * 32, service, allowed_uid=os.getuid())
        await server.start()
        try:
            client = RpcClient(endpoint, "tool_gateway", b"r" * 32)
            head_payload = {
                name: payload[name]
                for name in (
                    "tenant",
                    "owner",
                    "workspace",
                    "task",
                    "run",
                    "goal",
                    "lease",
                    "restore",
                    "worker_ref",
                    "binding",
                    "generation",
                    "branch",
                )
            }
            head_payload["expires_at"] = int(time.time()) + 5
            head_grant = sign_git_head_grant(b"g" * 32, head_payload)
            assert (await client.call("git.head", {"grant": head_grant}))["head"] == parent
            with pytest.raises(RpcError):
                await client.call(
                    "git.head", {"grant": {"payload": {**head_payload, "branch": "main"}, "mac": head_grant["mac"]}}
                )
            grant = sign_git_worker_grant(b"g" * 32, payload)
            patch_ref = VerifiedGitPatch(payload["patch_id"], parent, payload["patch_sha256"], ("a.txt",), patch)
            with pytest.raises(RpcError):
                await client.call("git.begin", {"grant": {"payload": payload, "mac": "0" * 64}})
            await register_git_transfer(
                endpoint, b"r" * 32, b"g" * 32, payload, patch_ref, deadline=time.monotonic() + 10
            )
            with pytest.raises(RpcError):
                await client.call("git.begin", {"grant": grant})
            frames = list(encode_chunks(git_transfer_key(b"t" * 32, operation), git_transfer_binding(payload), [patch]))
            pulse = {"operation": operation, "attempt": attempt, "request_sha256": payload["request_sha256"]}
            if scenario.startswith("heartbeat"):
                with pytest.raises(RpcError):
                    await client.call(
                        "git.chunk", {"operation": operation, "frame": base64.b64encode(frames[0]).decode()}
                    )
                assert (await client.call("git.heartbeat", pulse))["renewed"] is True
            if scenario == "bad_transfer":
                frames[0] = frames[0][:-1] + bytes([frames[0][-1] ^ 1])
            if scenario == "bad_transfer":
                await client.call("git.chunk", {"operation": operation, "frame": base64.b64encode(frames[0]).decode()})
                terminator = base64.b64encode(frames[1]).decode()
            else:
                terminator = await send_git_patch_frames(
                    endpoint, b"r" * 32, b"t" * 32, payload, patch_ref, deadline=time.monotonic() + 10
                )
            if scenario == "heartbeat_cancel":
                assert (await client.call("git.cancel", pulse))["cancelled"] is True
            if scenario in ("bad_transfer", "revoked_after_prepare", "heartbeat_cancel", "fence_error"):
                with pytest.raises(RpcError, match="GIT_RESULT_UNKNOWN"):
                    await client.call("git.execute", {"operation": operation, "end": terminator})
                assert run("rev-parse", "HEAD").decode().strip() == parent
                assert (journal_dir / (operation + ".prepared")).exists() == (
                    scenario in ("revoked_after_prepare", "fence_error")
                )
                with pytest.raises(RpcError):
                    await client.call("git.begin", {"grant": grant})
                return
            result = await client.call("git.execute", {"operation": operation, "end": terminator})
            verified = verify_git_worker_result(b"s" * 32, result, payload, "fixture-worker")
            verifier = RpcGitObjectVerifier(endpoint, b"r" * 32, b"g" * 32, "fixture-worker")
            await asyncio.to_thread(verifier.verify, verified, patch_ref, deadline=time.monotonic() + 10)
            assert run("rev-parse", "HEAD").decode().strip() == verified["commit"]
            assert run("rev-parse", verified["commit"] + "^").decode().strip() == parent
            assert journal.read(operation, "prepared")["commit"] == verified["commit"]
            assert journal.read(operation, "confirmed")["tree"] == verified["tree"]
            with pytest.raises(RpcError):
                await client.call("git.begin", {"grant": grant})
            with pytest.raises(ValueError):
                verify_git_worker_result(b"x" * 32, result, payload, "fixture-worker")
        finally:
            await server.close()

    try:
        asyncio.run(exercise())
        monkeypatch.setenv("AIAGENT_PROFILE", "development")
        with pytest.raises(RpcError, match="GIT_CAS_FENCE_UNAVAILABLE"):
            service.execute(operation, "")
    finally:
        journal.close()
