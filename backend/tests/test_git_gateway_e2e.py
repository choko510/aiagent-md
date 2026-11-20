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
import time
from pathlib import Path

import pytest
from aiagent_core import commands, control
from aiagent_core.commands import Scope
from aiagent_core.rpc import RpcClient
from aiagent_development.contracts import (
    Cycle,
    EffectUnknown,
    PatchArtifact,
    Phase,
    ProjectScope,
    RunFence,
    ScopeDenied,
    TaskPlan,
)
from aiagent_development.pg_store import PostgresCycleStore, PostgresRunAuthority, PostgresWorkspacePort
from aiagent_development.workflow import DevelopmentWorkflow
from aiagent_domain import uuid7
from aiagent_domain.rpcwire import RpcError
from aiagent_gateways.context_keys import KeyRing
from aiagent_gateways.git_cas_fence_rpc import build_git_cas_fence_server
from aiagent_gateways.git_commit_dispatch import GitCommitTestExecutor
from aiagent_gateways.git_commit_evidence import GitCommitEvidenceReader
from aiagent_gateways.git_head_reader import RpcGitHeadReader
from aiagent_gateways.git_object_verifier import RpcGitObjectVerifier
from aiagent_gateways.git_patch_artifacts import git_patch_aad
from aiagent_gateways.git_provider_adapter import ToolGatewayGitProvider
from aiagent_gateways.git_receipts import GitReceiptError, GitReceiptStore
from aiagent_gateways.servers import build_tool_server
from aiagent_gateways.tool_gateway import ToolGateway
from test_verified_workspace_pg import verified_setup  # noqa: F401

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        sys.platform != "linux" or os.environ.get("AIAGENT_GIT_GATEWAY_E2E") != "1", reason="opt-in Git Gateway E2E"
    ),
]


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "cancel_before_cas_admission",
        "lease_expired_before_admission",
        "approval_revoked_before_admission",
        "admission_response_lost",
        "cancel_after_cas_admission",
        "binding_changed_before_admission",
        "goal_changed_before_admission",
        "worker_response_lost",
        "receipt_response_lost",
        "workflow_commit",
    ],
)
def test_real_gateway_to_git_receipt(dbs, verified_setup, tmp_path: Path, scenario: str, monkeypatch):  # noqa: F811
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
    project = ProjectScope(
        claim.tenant_id, claim.owner_user_id, "fixture", workspace, "fixture/repo", "feat/fixture", parent, "1"
    )
    fence = RunFence(
        claim.task_id, claim.run_id, claim.goal_revision, claim.lease_epoch, claim.restore_incarnation, claim.worker_ref
    )
    approved_patch = PatchArtifact(artifact, digest, tuple(paths), parent, "1", fence)
    cycle_store = PostgresCycleStore(dbs["tool_gateway"])
    cycle_store.create(
        Cycle(
            cycle,
            project,
            fence,
            Phase.PATCH_READY,
            "fixture-change",
            version=1,
            plan=TaskPlan("plan-fixture", "fixture-change", ("unit",), claim.goal_revision),
            patch=approved_patch,
        )
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
            if scenario not in ("success", "workflow_commit", "worker_response_lost", "receipt_response_lost"):
                original_handler = fence_server.methods["git.enter_cas"].handler

                async def fault_at_admission(caller, payload):
                    if scenario == "cancel_before_cas_admission":
                        await asyncio.to_thread(
                            commands.cancel,
                            dbs["api"],
                            Scope(claim.tenant_id, claim.owner_user_id, workspace),
                            claim.task_id,
                        )
                    elif scenario == "lease_expired_before_admission":
                        await asyncio.to_thread(
                            dbs["owner"].one,
                            "UPDATE agent.runs SET lease_expires_at=clock_timestamp()-interval '1 second' "
                            "WHERE tenant_id=%s AND id=%s RETURNING id",
                            (claim.tenant_id, claim.run_id),
                        )
                    elif scenario == "approval_revoked_before_admission":
                        await asyncio.to_thread(
                            dbs["owner"].one,
                            "UPDATE agent.approval_records SET decision_code='REVOKED' "
                            "WHERE tenant_id=%s AND run_id=%s RETURNING id",
                            (claim.tenant_id, claim.run_id),
                        )
                    elif scenario == "binding_changed_before_admission":
                        await asyncio.to_thread(
                            dbs["owner"].one,
                            "UPDATE agent.repository_bindings SET generation=generation+1 "
                            "WHERE tenant_id=%s AND id=%s RETURNING id",
                            (claim.tenant_id, binding),
                        )
                    elif scenario == "goal_changed_before_admission":
                        await asyncio.to_thread(
                            commands.steer,
                            dbs["api"],
                            Scope(claim.tenant_id, claim.owner_user_id, workspace),
                            claim.task_id,
                            "revised fixture goal",
                            claim.goal_revision,
                        )
                    result = await original_handler(caller, payload)
                    if scenario == "cancel_after_cas_admission":
                        await asyncio.to_thread(
                            commands.cancel,
                            dbs["api"],
                            Scope(claim.tenant_id, claim.owner_user_id, workspace),
                            claim.task_id,
                        )
                    if scenario == "admission_response_lost":
                        raise RpcError("FIXTURE_RESPONSE_LOST")
                    return result

                fence_server.methods["git.enter_cas"].handler = fault_at_admission
            await fence_server.start()
            server = build_tool_server({"development": b"d" * 32, "runtime": b"r" * 32}, "tcp:127.0.0.1:0", gateway)
            await server.start()
            try:
                if scenario == "worker_response_lost":
                    original_call = RpcClient.call

                    async def lose_worker_response(self, method, payload):
                        response = await original_call(self, method, payload)
                        if method == "git.execute":
                            raise RpcError("FIXTURE_WORKER_RESPONSE_LOST")
                        return response

                    monkeypatch.setattr(RpcClient, "call", lose_worker_response)
                if scenario == "receipt_response_lost":
                    original_fetch = GitReceiptStore.fetch

                    def lose_receipt_response(self, *args, **kwargs):
                        original_fetch(self, *args, **kwargs)
                        raise GitReceiptError("fixture receipt response lost")

                    monkeypatch.setattr(GitReceiptStore, "fetch", lose_receipt_response)
                scope = ProjectScope(
                    claim.tenant_id,
                    claim.owner_user_id,
                    "fixture",
                    workspace,
                    "fixture/repo",
                    "feat/fixture",
                    parent,
                    "1",
                )
                head_reader = RpcGitHeadReader(
                    dbs["tool_gateway"],
                    claim,
                    binding_id=binding,
                    generation=2,
                    endpoint=worker_endpoint,
                    rpc_key=b"r" * 32,
                    grant_key=b"g" * 32,
                    worker_id="fixture-worker",
                )
                assert await asyncio.to_thread(head_reader, scope, deadline=time.monotonic() + 5) == parent
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
                if scenario == "workflow_commit":
                    receipt_store = GitReceiptStore(
                        dbs["tool_gateway"],
                        claim,
                        keys,
                        result_key=b"s" * 32,
                        worker_id="fixture-worker",
                        object_verifier=RpcGitObjectVerifier(worker_endpoint, b"r" * 32, b"g" * 32, "fixture-worker"),
                        require_fenced=True,
                    )
                    provider = ToolGatewayGitProvider(
                        dbs["tool_gateway"],
                        claim,
                        endpoint=f"tcp:127.0.0.1:{server.port}",
                        rpc_key=b"d" * 32,
                        head_reader=head_reader,
                        receipt_reader=GitCommitEvidenceReader(dbs["tool_gateway"], claim, receipt_store),
                    )
                    wrong_patch = PatchArtifact(artifact, "0" * 64, tuple(paths), parent, "1", fence)
                    with pytest.raises(ScopeDenied):
                        await asyncio.to_thread(
                            provider.commit_patch, scope, wrong_patch, parent, call_id, "approved fixture"
                        )
                    assert (
                        dbs["owner"].one(
                            "SELECT count(*) AS n FROM agent.git_operations WHERE tenant_id=%s", (claim.tenant_id,)
                        )["n"]
                        == 0
                    )
                    workflow = DevelopmentWorkflow(
                        runs=PostgresRunAuthority(dbs["tool_gateway"]),
                        workspaces=PostgresWorkspacePort(dbs["tool_gateway"]),
                        gateway=provider,
                        git=provider,
                        store=cycle_store,
                        reviewers=None,
                        sandbox=None,
                    )  # type: ignore[arg-type]
                    committed = await asyncio.to_thread(
                        workflow.commit, scope, cycle, message="approved fixture", intent_ref=call_id
                    )
                    assert committed.phase == Phase.COMMITTED and committed.commit is not None
                    loaded = await asyncio.to_thread(cycle_store.load, scope, cycle)
                    assert loaded.phase == Phase.COMMITTED and loaded.commit == committed.commit
                    assert run("rev-parse", "refs/heads/feat/fixture").decode().strip() == committed.commit.commit_sha
                    return
                result = await client.call("tool.development_git_commit", body)
                if scenario != "success":
                    assert result["status"] == "UNKNOWN", result
                    actual = run("rev-parse", "refs/heads/feat/fixture").decode().strip()
                    if scenario not in ("cancel_after_cas_admission", "worker_response_lost", "receipt_response_lost"):
                        assert actual == parent
                    row = dbs["owner"].one(
                        "SELECT o.state,o.candidate_commit,a.status_code AS attempt_status FROM agent.git_operations o "
                        "JOIN agent.tool_attempts a ON a.tenant_id=o.tenant_id AND a.id=o.attempt_id "
                        "JOIN agent.tool_intents i ON i.tenant_id=o.tenant_id AND i.id=o.intent_id "
                        "WHERE o.tenant_id=%s AND i.invocation_key=%s",
                        (claim.tenant_id, call_id),
                    )
                    expected_state = (
                        "RECEIPT_PERSISTED"
                        if scenario == "receipt_response_lost"
                        else "CAS_PENDING"
                        if scenario in ("admission_response_lost", "cancel_after_cas_admission", "worker_response_lost")
                        else "ADMITTED"
                    )
                    assert row["state"] == expected_state and row["attempt_status"] == "UNKNOWN"
                    if scenario in ("worker_response_lost", "receipt_response_lost"):
                        assert actual == row["candidate_commit"] and actual != parent
                        assert len(list(journal.glob("*.confirmed"))) == 1
                    if scenario == "receipt_response_lost":
                        receipts = dbs["owner"].one(
                            "SELECT count(*) AS n FROM agent.git_operation_receipts r "
                            "JOIN agent.git_operations o ON o.tenant_id=r.tenant_id AND o.id=r.operation_id "
                            "JOIN agent.tool_intents i ON i.tenant_id=o.tenant_id AND i.id=o.intent_id "
                            "WHERE i.tenant_id=%s AND i.invocation_key=%s",
                            (claim.tenant_id, call_id),
                        )
                        assert receipts["n"] == 1
                    if scenario in ("cancel_after_cas_admission", "worker_response_lost"):
                        assert actual in (parent, row["candidate_commit"])
                        receipts = dbs["owner"].one(
                            "SELECT count(*) AS n FROM agent.git_operation_receipts r "
                            "JOIN agent.git_operations o ON o.tenant_id=r.tenant_id AND o.id=r.operation_id "
                            "JOIN agent.tool_intents i ON i.tenant_id=o.tenant_id AND i.id=o.intent_id "
                            "WHERE i.tenant_id=%s AND i.invocation_key=%s",
                            (claim.tenant_id, call_id),
                        )
                        assert receipts["n"] == 0
                    if scenario in ("admission_response_lost", "worker_response_lost", "receipt_response_lost"):
                        replay = await client.call("tool.development_git_commit", body)
                        assert replay["status"] == "UNKNOWN"
                        count = dbs["owner"].one(
                            "SELECT count(*) AS n FROM agent.git_operations o JOIN agent.tool_intents i "
                            "ON i.tenant_id=o.tenant_id AND i.id=o.intent_id "
                            "WHERE i.tenant_id=%s AND i.invocation_key=%s",
                            (claim.tenant_id, call_id),
                        )
                        assert count["n"] == 1
                        assert run("rev-parse", "refs/heads/feat/fixture").decode().strip() == actual
                    return
                assert result["status"] == "CONFIRMED", result
                output = result["result"]
                actual = run("rev-parse", "refs/heads/feat/fixture").decode().strip()
                assert actual == output["commitSha"] and run("rev-parse", actual + "^").decode().strip() == parent
                assert await asyncio.to_thread(head_reader, scope, deadline=time.monotonic() + 5) == actual
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
                # Re-read after the real Gateway settled its Intent and Attempt.
                receipt_store = GitReceiptStore(
                    dbs["tool_gateway"],
                    claim,
                    keys,
                    result_key=b"s" * 32,
                    worker_id="fixture-worker",
                    object_verifier=RpcGitObjectVerifier(worker_endpoint, b"r" * 32, b"g" * 32, "fixture-worker"),
                    require_fenced=True,
                )
                receipt, receipt_sha = await asyncio.to_thread(
                    receipt_store.fetch, output["operationId"], output["receiptId"], deadline=time.monotonic() + 3
                )
                assert receipt["attempt"] == output["toolAttemptId"] and receipt["commit"] == actual
                assert receipt_sha == output["bodySha256"]
                run_fence = RunFence(
                    claim.task_id,
                    claim.run_id,
                    claim.goal_revision,
                    claim.lease_epoch,
                    claim.restore_incarnation,
                    claim.worker_ref,
                )
                scope = ProjectScope(
                    claim.tenant_id,
                    claim.owner_user_id,
                    "fixture",
                    workspace,
                    "fixture/repo",
                    "feat/fixture",
                    parent,
                    "1",
                )
                patch_ref = PatchArtifact(artifact, digest, tuple(paths), parent, "1", run_fence)
                reader = GitCommitEvidenceReader(dbs["tool_gateway"], claim, receipt_store)
                evidence = await asyncio.to_thread(
                    reader.read,
                    scope,
                    patch_ref,
                    output["operationId"],
                    output["receiptId"],
                    deadline=time.monotonic() + 3,
                )
                assert evidence.commit_sha == actual and evidence.receipt_ref == "git:" + output["receiptId"]
                wrong_patch = PatchArtifact(artifact, "0" * 64, tuple(paths), parent, "1", run_fence)
                with pytest.raises(EffectUnknown):
                    await asyncio.to_thread(
                        reader.read,
                        scope,
                        wrong_patch,
                        output["operationId"],
                        output["receiptId"],
                        deadline=time.monotonic() + 3,
                    )
                with pytest.raises(EffectUnknown):
                    await asyncio.to_thread(
                        reader.read, scope, patch_ref, output["operationId"], uuid7(), deadline=time.monotonic() + 3
                    )
            finally:
                await server.close()
                await fence_server.close()

        asyncio.run(exercise())
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
