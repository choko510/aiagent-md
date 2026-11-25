"""Opt-in test-profile Gateway to isolated Git worker and canonical receipt E2E.

The CAS revocation fence is not proven; this is not a development or production claim.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from aiagent_core import commands, control
from aiagent_core.commands import Scope
from aiagent_core.rpc import RpcClient
from aiagent_development.code_intelligence.git_proof import build_git_tree_proof
from aiagent_development.code_intelligence.git_reader import GitSnapshot
from aiagent_development.code_intelligence.indexer_recipe import approved_recipe_digest
from aiagent_development.code_intelligence.models import AuthorizedSnapshot, Identity
from aiagent_development.code_intelligence.projection import prepare_projection
from aiagent_development.code_intelligence.projection_stream import ProjectionEncoder
from aiagent_development.contracts import (
    Cycle,
    EffectUnknown,
    ExecutionRequest,
    PatchArtifact,
    Phase,
    ProjectScope,
    RunFence,
    ScopeDenied,
    StaleReference,
    StateConflict,
    TaskPlan,
)
from aiagent_development.factory import ToolGatewaySandboxAdapter
from aiagent_development.pg_store import PostgresCycleStore, PostgresRunAuthority, PostgresWorkspacePort
from aiagent_development.workflow import DevelopmentWorkflow
from aiagent_development.workspace_authority import register_verified_snapshot
from aiagent_domain import sha256_hex, uuid7
from aiagent_domain.rpcwire import RpcError
from aiagent_gateways.command_dispatch import AuthorizedCommandExecutor
from aiagent_gateways.command_receipt_reader import CommandReceiptReader
from aiagent_gateways.context_keys import KeyRing
from aiagent_gateways.development_authorization import DevelopmentCommandAuthorization
from aiagent_gateways.execution_artifacts import ExecutionArtifactError
from aiagent_gateways.execution_capture_proof import HmacCaptureVerifier
from aiagent_gateways.execution_lifecycle import ExecutionLifecycle
from aiagent_gateways.git_cas_fence_rpc import build_git_cas_fence_server
from aiagent_gateways.git_commit_dispatch import GitCommitTestExecutor
from aiagent_gateways.git_commit_evidence import GitCommitEvidenceReader
from aiagent_gateways.git_head_reader import RpcGitHeadReader
from aiagent_gateways.git_object_verifier import RpcGitObjectVerifier
from aiagent_gateways.git_patch_artifacts import git_patch_aad
from aiagent_gateways.git_provider_adapter import ToolGatewayGitProvider
from aiagent_gateways.git_receipts import GitReceiptError, GitReceiptStore
from aiagent_gateways.git_recovery_inspection import GitRecoveryUnavailable, inspect_unknown_git_operation
from aiagent_gateways.git_snapshot_promotion import GitSnapshotPromotionUnknown, promote_git_snapshot
from aiagent_gateways.servers import build_tool_server
from aiagent_gateways.tool_gateway import ToolGateway
from aiagent_sandbox.command_authority import command_template_digest
from aiagent_sandbox.git_worker_rpc import sign_git_recovery_grant
from psycopg.types.json import Jsonb
from stack import wait_port
from test_cell_e2e import _supervisor_answers
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
        "lease_expired_after_admission",
        "approval_revoked_after_admission",
        "binding_changed_after_admission",
        "goal_changed_after_admission",
        "binding_changed_before_admission",
        "goal_changed_before_admission",
        "worker_response_lost",
        "receipt_response_lost",
        "worker_crash_after_cas",
        "workflow_commit",
        "workflow_response_lost",
        "workflow_recovery_cancel",
        "workflow_recovery_concurrent",
        "workflow_recovery_uncertain",
        "workflow_recovery_tamper",
        "workflow_nonzero",
        "workflow_timeout",
        "workflow_truncation",
    ],
)
def test_real_gateway_to_git_receipt(dbs, verified_setup, tmp_path: Path, scenario: str, monkeypatch):  # noqa: F811
    claim, binding, manifest, base_proof, base_receipt, base_wire, projection_parent = verified_setup
    old_snapshot = None
    if scenario.startswith("workflow_"):
        old_snapshot = register_verified_snapshot(
            dbs["tool_gateway"],
            claim,
            binding,
            manifest,
            base_proof,
            base_receipt,
            io.BytesIO(base_wire),
            projection_parent,
            deadline=time.monotonic() + 15,
        )
    git = shutil.which("git")
    assert git is not None
    repo = tmp_path / "repo"

    def run(*args: str) -> bytes:
        return subprocess.check_output([git, "-C", str(repo), *args], stderr=subprocess.DEVNULL)

    parent = manifest.identity.commit
    run("branch", "feat/fixture", parent)
    program = {
        "workflow_nonzero": "raise SystemExit(7)\n",
        "workflow_timeout": "import time\ntime.sleep(35)\n",
        "workflow_truncation": "print('x' * 200000)\n",
    }.get(scenario, "value = 43\n")
    (repo / "a.py").write_text(program, encoding="utf-8")
    patch = run("diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--no-renames")
    digest = hashlib.sha256(patch).hexdigest()
    workspace = str(
        dbs["owner"].one(
            "UPDATE agent.repository_bindings SET approved_branch='feat/fixture',"
            "generation=generation+1,workspace_revision=workspace_revision+1 "
            "WHERE tenant_id=%s AND id=%s RETURNING workspace_id",
            (claim.tenant_id, binding),
        )["workspace_id"]
    )
    if scenario.startswith("workflow_"):
        # Branch approval creates a new binding generation; its base snapshot
        # must be independently reverified before it can be promoted.
        prior_snapshot = old_snapshot
        base_identity = Identity(claim.tenant_id, claim.owner_user_id, binding, "2", parent)
        base_snapshot = GitSnapshot(AuthorizedSnapshot(base_identity, repo.resolve()), Path(git).resolve())
        base_manifest2 = prepare_projection(base_snapshot, base_identity, base_snapshot.tree)
        base_proof2 = build_git_tree_proof(base_snapshot)
        base_encoder2 = ProjectionEncoder(base_snapshot, base_manifest2)
        base_source2 = b"".join(base_encoder2.chunks())
        assert base_encoder2.receipt is not None and prior_snapshot is not None
        old_snapshot = register_verified_snapshot(
            dbs["tool_gateway"],
            claim,
            binding,
            base_manifest2,
            base_proof2,
            base_encoder2.receipt,
            io.BytesIO(base_source2),
            projection_parent,
            deadline=time.monotonic() + 15,
        )
        assert old_snapshot.id != prior_snapshot.id and old_snapshot.revision == 2
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
        claim.tenant_id, claim.owner_user_id, "fixture", workspace, "fixture/repo", "feat/fixture", parent, "2"
    )
    fence = RunFence(
        claim.task_id, claim.run_id, claim.goal_revision, claim.lease_epoch, claim.restore_incarnation, claim.worker_ref
    )
    approved_patch = PatchArtifact(artifact, digest, tuple(paths), parent, "2", fence)
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
            f"FIXTURE_GIT_RECOVERY={'1' if scenario == 'worker_crash_after_cas' else '0'}",
            "-e",
            f"FIXTURE_CRASH_AFTER_CAS={'1' if scenario == 'worker_crash_after_cas' else '0'}",
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
            if scenario not in (
                "success",
                "workflow_commit",
                "workflow_response_lost",
                "workflow_recovery_cancel",
                "workflow_recovery_concurrent",
                "workflow_recovery_uncertain",
                "workflow_recovery_tamper",
                "workflow_nonzero",
                "workflow_timeout",
                "workflow_truncation",
                "worker_response_lost",
                "receipt_response_lost",
                "worker_crash_after_cas",
            ):
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
                    if scenario == "lease_expired_after_admission":
                        await asyncio.to_thread(
                            dbs["owner"].one,
                            "UPDATE agent.runs SET lease_expires_at=clock_timestamp()-interval '1 second' "
                            "WHERE tenant_id=%s AND id=%s RETURNING id",
                            (claim.tenant_id, claim.run_id),
                        )
                    if scenario == "approval_revoked_after_admission":
                        await asyncio.to_thread(
                            dbs["owner"].one,
                            "UPDATE agent.approval_records SET decision_code='REVOKED' "
                            "WHERE tenant_id=%s AND run_id=%s RETURNING id",
                            (claim.tenant_id, claim.run_id),
                        )
                    if scenario == "binding_changed_after_admission":
                        await asyncio.to_thread(
                            dbs["owner"].one,
                            "UPDATE agent.repository_bindings SET generation=generation+1 "
                            "WHERE tenant_id=%s AND id=%s RETURNING id",
                            (claim.tenant_id, binding),
                        )
                    if scenario == "goal_changed_after_admission":
                        await asyncio.to_thread(
                            commands.steer,
                            dbs["api"],
                            Scope(claim.tenant_id, claim.owner_user_id, workspace),
                            claim.task_id,
                            "revised fixture goal",
                            claim.goal_revision,
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
                if scenario in ("receipt_response_lost", "workflow_recovery_uncertain"):
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
                    "2",
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
                    cycle_id=cycle,
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
                if scenario.startswith("workflow_"):
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
                    wrong_patch = PatchArtifact(artifact, "0" * 64, tuple(paths), parent, "2", fence)
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
                    if scenario == "workflow_recovery_uncertain":
                        with pytest.raises(EffectUnknown):
                            await asyncio.to_thread(
                                workflow.commit, scope, cycle, message="approved fixture", intent_ref=call_id
                            )
                        with pytest.raises(EffectUnknown):
                            await asyncio.to_thread(workflow.reconcile_commit, scope, cycle)
                        still_unknown = await asyncio.to_thread(cycle_store.load, scope, cycle)
                        assert still_unknown.phase == Phase.UNKNOWN_EXTERNAL_EFFECT
                        record = dbs["owner"].one(
                            "SELECT o.state,a.status_code FROM agent.git_operations o "
                            "JOIN agent.tool_attempts a ON a.tenant_id=o.tenant_id AND a.id=o.attempt_id "
                            "JOIN agent.tool_intents i ON i.tenant_id=o.tenant_id AND i.id=o.intent_id "
                            "WHERE o.tenant_id=%s AND i.invocation_key=%s",
                            (claim.tenant_id, call_id),
                        )
                        assert record["state"] == "RECEIPT_PERSISTED" and record["status_code"] == "UNKNOWN"
                        return
                    if scenario in (
                        "workflow_response_lost",
                        "workflow_recovery_cancel",
                        "workflow_recovery_concurrent",
                        "workflow_recovery_tamper",
                    ):
                        original_commit = provider.commit_patch

                        def lose_workflow_response(*args, **kwargs):
                            original_commit(*args, **kwargs)
                            raise EffectUnknown("fixture lost the confirmed Gateway response")

                        monkeypatch.setattr(provider, "commit_patch", lose_workflow_response)
                        with pytest.raises(EffectUnknown):
                            await asyncio.to_thread(
                                workflow.commit, scope, cycle, message="approved fixture", intent_ref=call_id
                            )
                        unknown = await asyncio.to_thread(cycle_store.load, scope, cycle)
                        assert unknown.phase == Phase.UNKNOWN_EXTERNAL_EFFECT and unknown.commit_intent_ref == call_id
                        if scenario == "workflow_recovery_tamper":
                            dbs["owner"].one(
                                "UPDATE agent.git_operation_receipts r SET body=r.body || %s::bytea "
                                "FROM agent.git_operations o JOIN agent.tool_intents i "
                                "ON i.tenant_id=o.tenant_id AND i.id=o.intent_id "
                                "WHERE r.tenant_id=o.tenant_id AND r.operation_id=o.id "
                                "AND i.tenant_id=%s AND i.invocation_key=%s RETURNING r.receipt_id",
                                (b" ", claim.tenant_id, call_id),
                            )
                            with pytest.raises(EffectUnknown):
                                await asyncio.to_thread(workflow.reconcile_commit, scope, cycle)
                            assert (
                                await asyncio.to_thread(cycle_store.load, scope, cycle)
                            ).phase == Phase.UNKNOWN_EXTERNAL_EFFECT
                            return
                        with pytest.raises(EffectUnknown):
                            await asyncio.to_thread(
                                provider.recover_commit, scope, cycle, approved_patch, call_id + "-other"
                            )
                        with pytest.raises(EffectUnknown):
                            await asyncio.to_thread(
                                provider.recover_commit, scope, cycle + "-other", approved_patch, call_id
                            )
                        if scenario == "workflow_response_lost":
                            verified = await asyncio.to_thread(
                                provider.recover_commit, scope, cycle, approved_patch, call_id
                            )
                            forged = replace(verified, diff_sha256="0" * 64)
                            with pytest.raises(StateConflict, match="receipt mismatch"):
                                await asyncio.to_thread(
                                    cycle_store.save,
                                    unknown,
                                    replace(unknown, version=unknown.version + 1, phase=Phase.COMMITTED, commit=forged),
                                )
                            with pytest.raises(StateConflict, match="recovery authority"):
                                await asyncio.to_thread(
                                    cycle_store.save,
                                    unknown,
                                    replace(
                                        unknown,
                                        version=unknown.version + 1,
                                        phase=Phase.COMMITTED,
                                        commit=verified,
                                        commit_intent_ref=call_id + "-other",
                                    ),
                                )
                        if scenario == "workflow_recovery_cancel":
                            original_save = cycle_store.save

                            def cancel_before_save(old, updated):
                                if old.phase == Phase.UNKNOWN_EXTERNAL_EFFECT and updated.phase == Phase.COMMITTED:
                                    commands.cancel(
                                        dbs["api"],
                                        Scope(claim.tenant_id, claim.owner_user_id, workspace),
                                        claim.task_id,
                                    )
                                return original_save(old, updated)

                            monkeypatch.setattr(cycle_store, "save", cancel_before_save)
                            with pytest.raises(EffectUnknown, match="could not be fenced"):
                                await asyncio.to_thread(workflow.reconcile_commit, scope, cycle)
                            assert (
                                await asyncio.to_thread(cycle_store.load, scope, cycle)
                            ).phase == Phase.UNKNOWN_EXTERNAL_EFFECT
                            return
                        if scenario == "workflow_recovery_concurrent":
                            outcomes = await asyncio.gather(
                                asyncio.to_thread(workflow.reconcile_commit, scope, cycle),
                                asyncio.to_thread(workflow.reconcile_commit, scope, cycle),
                                return_exceptions=True,
                            )
                            successes = [item for item in outcomes if isinstance(item, Cycle)]
                            assert len(successes) == 1 and sum(isinstance(item, Exception) for item in outcomes) == 1
                            committed = successes[0]
                        else:
                            committed = await asyncio.to_thread(workflow.reconcile_commit, scope, cycle)
                    else:
                        committed = await asyncio.to_thread(
                            workflow.commit, scope, cycle, message="approved fixture", intent_ref=call_id
                        )
                    assert committed.phase == Phase.COMMITTED and committed.commit is not None
                    loaded = await asyncio.to_thread(cycle_store.load, scope, cycle)
                    assert loaded.phase == Phase.COMMITTED and loaded.commit == committed.commit
                    assert run("rev-parse", "refs/heads/feat/fixture").decode().strip() == committed.commit.commit_sha
                    new_identity = Identity(
                        claim.tenant_id, claim.owner_user_id, binding, "3", committed.commit.commit_sha
                    )
                    snapshot = GitSnapshot(AuthorizedSnapshot(new_identity, repo.resolve()), Path(git).resolve())
                    new_manifest = prepare_projection(snapshot, new_identity, snapshot.tree)
                    new_proof = build_git_tree_proof(snapshot)
                    encoder = ProjectionEncoder(snapshot, new_manifest)
                    source_chunks = list(encoder.chunks())
                    source = b"".join(source_chunks)
                    assert encoder.receipt is not None and old_snapshot is not None
                    operation_row = dbs["owner"].one(
                        "SELECT o.id FROM agent.git_operations o JOIN agent.tool_intents i "
                        "ON i.tenant_id=o.tenant_id AND i.id=o.intent_id WHERE i.tenant_id=%s AND i.invocation_key=%s",
                        (claim.tenant_id, call_id),
                    )
                    with pytest.raises(GitSnapshotPromotionUnknown):
                        await asyncio.to_thread(
                            promote_git_snapshot,
                            dbs["tool_gateway"],
                            claim,
                            receipt_store,
                            str(operation_row["id"]),
                            committed.commit.receipt_ref.removeprefix("git:"),
                            replace(new_manifest, identity=manifest.identity),
                            new_proof,
                            encoder.receipt,
                            io.BytesIO(source),
                            projection_parent,
                            deadline=time.monotonic() + 10,
                        )
                    assert (
                        dbs["owner"].one(
                            "SELECT approved_commit FROM agent.repository_bindings WHERE tenant_id=%s AND id=%s",
                            (claim.tenant_id, binding),
                        )["approved_commit"]
                        == parent
                    )
                    promoted = await asyncio.to_thread(
                        promote_git_snapshot,
                        dbs["tool_gateway"],
                        claim,
                        receipt_store,
                        str(operation_row["id"]),
                        committed.commit.receipt_ref.removeprefix("git:"),
                        new_manifest,
                        new_proof,
                        encoder.receipt,
                        io.BytesIO(source),
                        projection_parent,
                        deadline=time.monotonic() + 10,
                    )
                    assert promoted.commit == committed.commit.commit_sha and promoted.revision == 3
                    assert promoted.id != old_snapshot.id
                    with pytest.raises(GitSnapshotPromotionUnknown):
                        await asyncio.to_thread(
                            promote_git_snapshot,
                            dbs["tool_gateway"],
                            claim,
                            receipt_store,
                            str(operation_row["id"]),
                            committed.commit.receipt_ref.removeprefix("git:"),
                            new_manifest,
                            new_proof,
                            encoder.receipt,
                            io.BytesIO(source),
                            projection_parent,
                            deadline=time.monotonic() + 10,
                        )
                    loaded_after = await asyncio.to_thread(cycle_store.load, scope, cycle)
                    assert loaded_after.scope == scope and loaded_after.commit == committed.commit
                    # Actual command execution is dispatched under a separately
                    # approved recipe against the newly promoted snapshot.
                    assert control.heartbeat(dbs["runtime"], claim, lease_s=120)
                    command_name = "aiagent-git-command-e2e-" + secrets.token_hex(5)
                    image = subprocess.check_output(
                        ["docker", "image", "inspect", "codex-dev13-scip-cell-fixture:latest", "--format", "{{.Id}}"],
                        text=True,
                    ).strip()
                    launched = subprocess.run(
                        [
                            "docker",
                            "run",
                            "-d",
                            "--name",
                            command_name,
                            "-p",
                            "127.0.0.1::7000",
                            "--cap-add",
                            "SYS_ADMIN",
                            "--security-opt",
                            "seccomp=unconfined",
                            "--security-opt",
                            "apparmor=unconfined",
                            "--read-only",
                            "--tmpfs",
                            "/tmp:exec,mode=1777,size=256m",
                            "--pids-limit",
                            "256",
                            "--memory",
                            "3g",
                            "-v",
                            f"{backend}:/w:ro",
                            "-w",
                            "/w",
                            "-e",
                            "PYTHONDONTWRITEBYTECODE=1",
                            "-e",
                            "PYTHONPATH=/w/packages/domain/src:/w/packages/core/src:/w/packages/gateways/src:/w/packages/sandbox/src:/w/packages/development/src",
                            image,
                            "python",
                            "/w/packages/development/scripts/scip_cell_fixture/serve_command.py",
                        ],
                        capture_output=True,
                        text=True,
                    )
                    assert launched.returncode == 0, launched.stderr
                    try:
                        port = int(
                            subprocess.check_output(["docker", "port", command_name, "7000/tcp"], text=True)
                            .strip()
                            .rsplit(":", 1)[1]
                        )
                        await asyncio.to_thread(wait_port, port, 60)
                        assert await asyncio.to_thread(_supervisor_answers, port, 30)
                        binary = "/usr/local/bin/python3.13"
                        toolchain = subprocess.check_output(
                            [
                                "docker",
                                "exec",
                                command_name,
                                "python",
                                "-c",
                                "from aiagent_development.code_intelligence.indexer_recipe import measure_command_toolchain; "
                                "print(measure_command_toolchain('/usr/local/bin/python3.13'))",
                            ],
                            text=True,
                        ).strip()
                        argv = [binary, "-B", "a.py"]
                        command_args = {"argv": argv, "cwdRelative": "."}
                        descriptor = {"kind": "test", "test_id": "unit", **command_args}
                        assert loaded_after.plan is not None
                        request_sha = sha256_hex({"tool": "sandbox_exec", "args": command_args})
                        plan_sha = sha256_hex(asdict(loaded_after.plan))
                        template, recipe_id = command_template_digest(descriptor), uuid7()
                        recipe_sha = approved_recipe_digest(
                            kind="test",
                            test_id="unit",
                            plan_sha256=plan_sha,
                            request_sha256=request_sha,
                            template_sha256=template,
                            toolchain_sha256=toolchain,
                            image_digest=image,
                        )
                        dbs["owner"].one(
                            "INSERT INTO agent.execution_recipe_approvals(tenant_id,id,cycle_id,goal_revision,"
                            "plan_sha256,plan_json,kind,test_id,request_sha256,template_sha256,recipe_sha256,toolchain_sha256,"
                            "image_digest,approval_ref) VALUES (%s,%s,%s,%s,%s,%s,'test','unit',%s,%s,%s,%s,%s,%s) RETURNING id",
                            (
                                claim.tenant_id,
                                recipe_id,
                                cycle,
                                claim.goal_revision,
                                plan_sha,
                                Jsonb(asdict(loaded_after.plan)),
                                request_sha,
                                template,
                                recipe_sha,
                                toolchain,
                                image,
                                "operator-test-fixture",
                            ),
                        )
                        lifecycle = ExecutionLifecycle(
                            dbs["tool_gateway"], claim, HmacCaptureVerifier(dbs["tool_gateway"], b"c" * 32)
                        )
                        with pytest.raises(ExecutionArtifactError, match="COMMAND_SNAPSHOT_DENIED"):
                            await asyncio.to_thread(
                                lifecycle.check_snapshot_target, workspace, 2, parent, deadline=time.monotonic() + 5
                            )
                        command_executor = AuthorizedCommandExecutor(
                            dbs["tool_gateway"],
                            claim,
                            promoted.id,
                            recipe_id,
                            lifecycle,
                            new_manifest,
                            encoder.receipt,
                            new_proof,
                            source_chunks,
                            f"tcp:127.0.0.1:{port}",
                            b"r" * 32,
                            b"t" * 32,
                            b"g" * 32,
                            b"c" * 32,
                            projection_parent,
                        )
                        command_gateway = ToolGateway(dbs["tool_gateway"], {"sandbox_cell": command_executor})
                        command_adapter = ToolGatewaySandboxAdapter(
                            dbs["model_gateway"],
                            command_gateway,
                            claim,
                            receipt_reader=CommandReceiptReader(lifecycle, b"c" * 32),
                        )
                        auth = DevelopmentCommandAuthorization(
                            dbs["tool_gateway"], claim, recipe_id=recipe_id, snapshot_id=promoted.id, cycle_id=cycle
                        )
                        test_workflow = DevelopmentWorkflow(
                            runs=PostgresRunAuthority(dbs["tool_gateway"]),
                            workspaces=PostgresWorkspacePort(dbs["tool_gateway"]),
                            gateway=auth,
                            git=provider,
                            store=cycle_store,
                            reviewers=None,
                            sandbox=command_adapter,
                        )  # type: ignore[arg-type]
                        request = ExecutionRequest(
                            scope,
                            fence,
                            "plan-fixture",
                            "test",
                            tuple(argv),
                            toolchain,
                            "post-commit-test",
                            test_id="unit",
                            target_commit_sha=committed.commit.commit_sha,
                            snapshot_revision=3,
                        )
                        with pytest.raises(StaleReference):
                            await asyncio.to_thread(
                                test_workflow.execute, scope, cycle, replace(request, snapshot_revision=2)
                            )
                        with pytest.raises(StaleReference):
                            await asyncio.to_thread(
                                test_workflow.execute, scope, cycle, replace(request, target_commit_sha=parent)
                            )
                        completed = await asyncio.to_thread(test_workflow.execute, scope, cycle, request)
                        restored = await asyncio.to_thread(cycle_store.load, scope, cycle)
                        assert completed == restored and len(restored.tests) == 1
                        evidence = restored.tests[0]
                        await asyncio.to_thread(test_workflow._ensure_review_head, restored)
                        with pytest.raises(StaleReference):
                            await asyncio.to_thread(
                                head_reader, replace(scope, project_id="other"), deadline=time.monotonic() + 5
                            )
                        assert (
                            evidence.executed
                            and (
                                evidence.exit_code == (7 if scenario == "workflow_nonzero" else 0)
                                if scenario
                                in (
                                    "workflow_commit",
                                    "workflow_response_lost",
                                    "workflow_recovery_concurrent",
                                    "workflow_nonzero",
                                )
                                else evidence.exit_code is not None
                            )
                            and evidence.timed_out is (scenario == "workflow_timeout")
                            and evidence.truncated is (scenario == "workflow_truncation")
                            and evidence.test_id == "unit"
                            and evidence.source_commit_sha == committed.commit.commit_sha
                            and evidence.snapshot_revision == 3
                        )
                        if scenario not in (
                            "workflow_commit",
                            "workflow_response_lost",
                            "workflow_recovery_concurrent",
                        ):
                            assert not DevelopmentWorkflow._gate(restored, ())
                        with pytest.raises(StateConflict):
                            await asyncio.to_thread(test_workflow.execute, scope, cycle, request)
                        attempts = dbs["owner"].one(
                            "SELECT count(*) AS n FROM agent.execution_instances "
                            "WHERE tenant_id=%s AND run_id=%s AND recipe_id=%s",
                            (claim.tenant_id, claim.run_id, recipe_id),
                        )
                        assert attempts["n"] == 1
                    finally:
                        subprocess.run(["docker", "rm", "-f", command_name], capture_output=True, check=False)
                    return
                result = await client.call("tool.development_git_commit", body)
                if scenario != "success":
                    assert result["status"] == "UNKNOWN", result
                    actual = run("rev-parse", "refs/heads/feat/fixture").decode().strip()
                    if scenario not in (
                        "cancel_after_cas_admission",
                        "lease_expired_after_admission",
                        "approval_revoked_after_admission",
                        "binding_changed_after_admission",
                        "goal_changed_after_admission",
                        "worker_response_lost",
                        "receipt_response_lost",
                        "worker_crash_after_cas",
                    ):
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
                        if scenario
                        in (
                            "admission_response_lost",
                            "cancel_after_cas_admission",
                            "lease_expired_after_admission",
                            "approval_revoked_after_admission",
                            "binding_changed_after_admission",
                            "goal_changed_after_admission",
                            "worker_response_lost",
                            "worker_crash_after_cas",
                        )
                        else "ADMITTED"
                    )
                    assert row["state"] == expected_state and row["attempt_status"] == "UNKNOWN"
                    if scenario in ("worker_response_lost", "receipt_response_lost", "worker_crash_after_cas"):
                        assert actual == row["candidate_commit"] and actual != parent
                        assert len(list(journal.glob("*.confirmed"))) == 1
                    if scenario == "worker_crash_after_cas":
                        # Explicitly test read-only journal inspection after a
                        # real container restart. It cannot settle UNKNOWN.
                        record = dbs["owner"].one(
                            "SELECT o.*,i.id AS canonical_intent FROM agent.git_operations o "
                            "JOIN agent.tool_intents i ON i.tenant_id=o.tenant_id AND i.id=o.intent_id "
                            "WHERE i.tenant_id=%s AND i.invocation_key=%s",
                            (claim.tenant_id, call_id),
                        )
                        recovery = {
                            "operation": str(record["id"]),
                            "tenant": claim.tenant_id,
                            "owner": claim.owner_user_id,
                            "task": claim.task_id,
                            "run": claim.run_id,
                            "workspace": workspace,
                            "goal": claim.goal_revision,
                            "lease": claim.lease_epoch,
                            "restore": claim.restore_incarnation,
                            "worker_ref": claim.worker_ref,
                            "intent": str(record["intent_id"]),
                            "attempt": str(record["attempt_id"]),
                            "effect_key": record["effect_key"],
                            "binding": binding,
                            "generation": 2,
                            "branch": "feat/fixture",
                            "parent": parent,
                            "patch_id": artifact,
                            "patch_sha256": digest,
                            "patch_bytes": len(patch),
                            "paths": paths,
                            "message": "approved fixture",
                            "cycle_id": cycle,
                            "plan_ref": "plan-fixture",
                            "request_sha256": record["request_sha256"],
                            "expires_at": int(time.time()) + 9,
                        }
                        restarted = subprocess.run(["docker", "start", name], capture_output=True, text=True)
                        assert restarted.returncode == 0, restarted.stderr
                        recover_client = RpcClient(worker_endpoint, "tool_gateway", b"r" * 32, timeout_s=1)
                        evidence = None
                        for _ in range(40):
                            try:
                                evidence = await recover_client.call(
                                    "git.recovery_inspect", {"grant": sign_git_recovery_grant(b"g" * 32, recovery)}
                                )
                                break
                            except (OSError, RpcError, asyncio.IncompleteReadError):
                                await asyncio.sleep(0.1)
                        assert evidence is not None and evidence["commit"] == actual
                        assert evidence["parent"] == parent and evidence["diff_sha256"] == digest
                        observation = await inspect_unknown_git_operation(
                            dbs["tool_gateway"],
                            claim,
                            keys,
                            str(record["id"]),
                            endpoint=worker_endpoint,
                            rpc_key=b"r" * 32,
                            grant_key=b"g" * 32,
                            journal_key=b"j" * 32,
                            result_key=b"s" * 32,
                            worker_id="fixture-worker",
                            deadline=time.monotonic() + 8,
                        )
                        assert observation.commit == actual and observation.attempt_id == str(record["attempt_id"])
                        with pytest.raises(GitRecoveryUnavailable):
                            await inspect_unknown_git_operation(
                                dbs["tool_gateway"],
                                claim,
                                keys,
                                str(record["id"]),
                                endpoint=worker_endpoint,
                                rpc_key=b"r" * 32,
                                grant_key=b"g" * 32,
                                journal_key=b"x" * 32,
                                result_key=b"s" * 32,
                                worker_id="fixture-worker",
                                deadline=time.monotonic() + 8,
                            )
                        with pytest.raises(GitRecoveryUnavailable):
                            await inspect_unknown_git_operation(
                                dbs["tool_gateway"],
                                claim,
                                keys,
                                str(record["id"]),
                                endpoint=worker_endpoint,
                                rpc_key=b"r" * 32,
                                grant_key=b"g" * 32,
                                journal_key=b"j" * 32,
                                result_key=b"x" * 32,
                                worker_id="fixture-worker",
                                deadline=time.monotonic() + 8,
                            )
                        with pytest.raises(GitRecoveryUnavailable):
                            await inspect_unknown_git_operation(
                                dbs["tool_gateway"],
                                replace(claim, lease_epoch=claim.lease_epoch + 1),
                                keys,
                                str(record["id"]),
                                endpoint=worker_endpoint,
                                rpc_key=b"r" * 32,
                                grant_key=b"g" * 32,
                                journal_key=b"j" * 32,
                                result_key=b"s" * 32,
                                worker_id="fixture-worker",
                                deadline=time.monotonic() + 8,
                            )
                        (journal / (str(record["id"]) + ".prepared")).write_bytes(b"tampered journal")
                        with pytest.raises(GitRecoveryUnavailable):
                            await inspect_unknown_git_operation(
                                dbs["tool_gateway"],
                                claim,
                                keys,
                                str(record["id"]),
                                endpoint=worker_endpoint,
                                rpc_key=b"r" * 32,
                                grant_key=b"g" * 32,
                                journal_key=b"j" * 32,
                                result_key=b"s" * 32,
                                worker_id="fixture-worker",
                                deadline=time.monotonic() + 8,
                            )
                        with pytest.raises(RpcError):
                            await recover_client.call(
                                "git.recovery_inspect", {"grant": {"payload": recovery, "mac": "0" * 64}}
                            )
                        with pytest.raises(RpcError):
                            await recover_client.call(
                                "git.recovery_inspect",
                                {"grant": sign_git_recovery_grant(b"g" * 32, {**recovery, "attempt": uuid7()})},
                            )
                        after = dbs["owner"].one(
                            "SELECT o.state,a.status_code FROM agent.git_operations o "
                            "JOIN agent.tool_attempts a ON a.tenant_id=o.tenant_id AND a.id=o.attempt_id "
                            "WHERE o.tenant_id=%s AND o.id=%s",
                            (claim.tenant_id, record["id"]),
                        )
                        assert after["state"] == "CAS_PENDING" and after["status_code"] == "UNKNOWN"
                    if scenario == "receipt_response_lost":
                        receipts = dbs["owner"].one(
                            "SELECT count(*) AS n FROM agent.git_operation_receipts r "
                            "JOIN agent.git_operations o ON o.tenant_id=r.tenant_id AND o.id=r.operation_id "
                            "JOIN agent.tool_intents i ON i.tenant_id=o.tenant_id AND i.id=o.intent_id "
                            "WHERE i.tenant_id=%s AND i.invocation_key=%s",
                            (claim.tenant_id, call_id),
                        )
                        assert receipts["n"] == 1
                    if scenario in (
                        "cancel_after_cas_admission",
                        "lease_expired_after_admission",
                        "approval_revoked_after_admission",
                        "binding_changed_after_admission",
                        "goal_changed_after_admission",
                        "worker_response_lost",
                        "worker_crash_after_cas",
                    ):
                        assert actual in (parent, row["candidate_commit"])
                        receipts = dbs["owner"].one(
                            "SELECT count(*) AS n FROM agent.git_operation_receipts r "
                            "JOIN agent.git_operations o ON o.tenant_id=r.tenant_id AND o.id=r.operation_id "
                            "JOIN agent.tool_intents i ON i.tenant_id=o.tenant_id AND i.id=o.intent_id "
                            "WHERE i.tenant_id=%s AND i.invocation_key=%s",
                            (claim.tenant_id, call_id),
                        )
                        assert receipts["n"] == 0
                    if scenario in (
                        "admission_response_lost",
                        "worker_response_lost",
                        "receipt_response_lost",
                        "worker_crash_after_cas",
                    ):
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
                    "2",
                )
                patch_ref = PatchArtifact(artifact, digest, tuple(paths), parent, "2", run_fence)
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
                wrong_patch = PatchArtifact(artifact, "0" * 64, tuple(paths), parent, "2", run_fence)
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
