"""Opt-in test-profile Gateway to isolated Git worker and canonical receipt E2E.

The CAS revocation fence is not proven; this is not a development or production claim.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from aiagent_core import commands, control
from aiagent_core.commands import Scope
from aiagent_core.rpc import RpcClient
from aiagent_domain import uuid7
from aiagent_gateways.context_keys import KeyRing
from aiagent_gateways.git_cas_fence_rpc import build_git_cas_fence_server
from aiagent_gateways.git_commit_dispatch import GitCommitTestExecutor
from aiagent_gateways.git_patch_artifacts import git_patch_aad
from aiagent_gateways.servers import build_tool_server
from aiagent_gateways.tool_gateway import ToolGateway
from psycopg.types.json import Jsonb
from test_verified_workspace_pg import verified_setup  # noqa: F401

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        sys.platform != "linux" or os.environ.get("AIAGENT_GIT_GATEWAY_E2E") != "1", reason="opt-in Git Gateway E2E"
    ),
]


def test_real_gateway_to_git_receipt(dbs, verified_setup, tmp_path: Path):  # noqa: F811
    claim, binding, manifest, _, _, _, _ = verified_setup
    git = shutil.which("git")
    assert git is not None
    repo = tmp_path / "repo"

    def run(*args: str) -> bytes:
        return subprocess.check_output([git, "-C", str(repo), *args], stderr=subprocess.DEVNULL)

    parent = manifest.identity.commit
    run("branch", "feat/fixture", parent)
    (repo / "a.py").write_text("value = 43\n", encoding="utf-8")
    patch = run("diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--no-renames")
    digest = hashlib.sha256(patch).hexdigest()
    workspace = str(
        dbs["owner"].one(
            "UPDATE agent.repository_bindings SET approved_branch='feat/fixture',"
            "generation=generation+1 WHERE tenant_id=%s AND id=%s RETURNING workspace_id",
            (claim.tenant_id, binding),
        )["workspace_id"]
    )
    artifact, cycle, nonce = uuid7(), "git-e2e-" + uuid7(), secrets.token_bytes(12)
    paths, key_id = ["a.py"], "legacy"
    metadata = {
        "tenant_id": claim.tenant_id,
        "id": artifact,
        "owner_user_id": claim.owner_user_id,
        "workspace_id": workspace,
        "binding_id": binding,
        "binding_generation": 2,
        "cycle_id": cycle,
        "task_id": claim.task_id,
        "run_id": claim.run_id,
        "parent_sha": parent,
        "diff_sha256": digest,
        "paths": paths,
        "byte_length": len(patch),
        "key_id": key_id,
    }
    keys = KeyRing.single(b"p" * 32)
    ciphertext = keys.cipher(key_id).encrypt(nonce, patch, git_patch_aad(metadata))
    cycle_json = {
        "patch": {"artifact_ref": artifact, "parent_sha": parent, "diff_sha256": digest, "paths": paths},
        "plan": {"plan_ref": "plan-fixture", "goal_revision": claim.goal_revision},
        "run": {
            "task_id": claim.task_id,
            "run_id": claim.run_id,
            "goal_revision": claim.goal_revision,
            "lease_epoch": claim.lease_epoch,
            "restore_incarnation": claim.restore_incarnation,
            "worker_ref": claim.worker_ref,
        },
    }
    dbs["owner"].one(
        "INSERT INTO agent.dev_cycles(tenant_id,id,owner_user_id,project_id,workspace_id,task_id,run_id,"
        "version,phase,cycle_json) VALUES (%s,%s,%s,'fixture',%s,%s,%s,1,'PATCH_READY',%s) RETURNING id",
        (claim.tenant_id, cycle, claim.owner_user_id, workspace, claim.task_id, claim.run_id, Jsonb(cycle_json)),
    )
    dbs["owner"].one(
        "INSERT INTO agent.git_patch_artifacts(tenant_id,id,owner_user_id,workspace_id,binding_id,"
        "binding_generation,cycle_id,task_id,run_id,parent_sha,diff_sha256,paths,byte_length,key_id,nonce,ciphertext) "
        "VALUES (%s,%s,%s,%s,%s,2,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (
            claim.tenant_id,
            artifact,
            claim.owner_user_id,
            workspace,
            binding,
            cycle,
            claim.task_id,
            claim.run_id,
            parent,
            digest,
            paths,
            len(patch),
            key_id,
            nonce,
            ciphertext,
        ),
    )
    journal, bridge = tmp_path / "journal", tmp_path / "bridge"
    journal.mkdir(mode=0o700)
    bridge.mkdir(mode=0o700)
    backend = Path(__file__).resolve().parents[1]
    name = "aiagent-git-gateway-e2e-" + secrets.token_hex(5)
    started = subprocess.run(
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
            f"FIXTURE_TENANT={claim.tenant_id}",
            "-e",
            f"FIXTURE_BINDING={binding}",
            "-e",
            "FIXTURE_GIT_FENCE=1",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "PYTHONPATH=/w/.linuxdeps:/w/packages/domain/src:/w/packages/core/src:/w/packages/development/src:/w/packages/sandbox/src",
            "codex-dev13-scip-cell-fixture:latest",
            "python",
            "/w/packages/sandbox/tests/fixtures/serve_git_worker.py",
        ],
        capture_output=True,
        text=True,
    )
    assert started.returncode == 0, started.stderr
    try:

        async def exercise():
            socket = bridge / "worker.sock"
            for _ in range(200):
                if socket.exists():
                    break
                await asyncio.sleep(0.1)
            assert socket.exists(), subprocess.run(["docker", "logs", name], capture_output=True, text=True).stderr
            worker_endpoint = "unix:" + str(socket)
            executor = GitCommitTestExecutor(
                dbs["tool_gateway"],
                claim,
                keys,
                endpoint=worker_endpoint,
                rpc_key=b"r" * 32,
                grant_key=b"g" * 32,
                transfer_key=b"t" * 32,
                result_key=b"s" * 32,
                worker_id="fixture-worker",
                require_fenced=True,
            )
            gateway = ToolGateway(dbs["tool_gateway"], {"git_commit": executor}, development_git_enabled=True)
            args = {
                "bindingId": binding,
                "bindingGeneration": 2,
                "patchId": artifact,
                "cycleId": cycle,
                "planRef": "plan-fixture",
                "branch": "feat/fixture",
                "parent": parent,
                "patchSha256": digest,
                "paths": paths,
                "message": "approved fixture",
            }
            call_id = "git-e2e-" + uuid7()
            with dbs["model_gateway"].tx(tenant=claim.tenant_id) as c:
                control.append_head(
                    c,
                    claim,
                    "assistant",
                    {
                        "kind": "assistant",
                        "content": "",
                        "tool_calls": [{"id": call_id, "name": "git.commit", "args": args}],
                    },
                    writer="development_git_adapter",
                )
            fence_server = build_git_cas_fence_server(
                "unix:" + str(bridge / "fence.sock"),
                b"f" * 32,
                dbs["tool_gateway"],
                claim,
                keys,
                b"j" * 32,
                allowed_uid=os.getuid(),
            )
            await fence_server.start()
            server = build_tool_server({"development": b"d" * 32, "runtime": b"r" * 32}, "tcp:127.0.0.1:0", gateway)
            await server.start()
            try:
                client = RpcClient(f"tcp:127.0.0.1:{server.port}", "development", b"d" * 32)
                body = {"claim": claim.to_wire(), "toolCallId": call_id, "args": args}
                pending = await client.call("tool.development_git_commit", body)
                assert pending["status"] == "PENDING_APPROVAL"
                await asyncio.to_thread(
                    commands.decide_approval,
                    dbs["api"],
                    Scope(claim.tenant_id, claim.owner_user_id, workspace),
                    pending["approvalId"],
                    "APPROVED",
                    pending["requestSha256"],
                    1,
                )
                result = await client.call("tool.development_git_commit", body)
                assert result["status"] == "CONFIRMED", result
                output = result["result"]
                actual = run("rev-parse", "refs/heads/feat/fixture").decode().strip()
                assert actual == output["commitSha"] and run("rev-parse", actual + "^").decode().strip() == parent
                row = dbs["owner"].one(
                    "SELECT o.state,o.candidate_commit,o.candidate_tree,o.prepared_journal_sha256,"
                    "a.status_code AS attempt_status,r.receipt_id FROM agent.git_operations o "
                    "JOIN agent.tool_attempts a ON a.tenant_id=o.tenant_id AND a.id=o.attempt_id "
                    "JOIN agent.git_operation_receipts r ON r.tenant_id=o.tenant_id AND r.operation_id=o.id "
                    "WHERE o.tenant_id=%s AND o.id=%s",
                    (claim.tenant_id, output["operationId"]),
                )
                assert row["state"] == "RECEIPT_PERSISTED" and row["attempt_status"] == "CONFIRMED"
                assert str(row["receipt_id"]) == output["receiptId"]
                assert row["candidate_commit"] == actual
                assert row["candidate_tree"] == run("rev-parse", actual + "^{tree}").decode().strip()
                journal_bytes = (journal / (output["operationId"] + ".prepared")).read_bytes()
                assert hashlib.sha256(journal_bytes).hexdigest() == row["prepared_journal_sha256"]
            finally:
                await server.close()
                await fence_server.close()

        asyncio.run(exercise())
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
