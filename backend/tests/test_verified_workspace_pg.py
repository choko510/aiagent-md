"""Real DB + actual disposable Git objects, no running Cell or authenticated transfer."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from aiagent_core import commands, control
from aiagent_core.commands import Scope
from aiagent_core.rpc import RpcClient
from aiagent_development.code_intelligence.git_proof import build_git_tree_proof
from aiagent_development.code_intelligence.git_reader import GitSnapshot
from aiagent_development.code_intelligence.models import AuthorizedSnapshot, Identity
from aiagent_development.code_intelligence.projection import prepare_projection
from aiagent_development.code_intelligence.projection_stream import ProjectionEncoder
from aiagent_development.contracts import InvalidEvidence, ScopeDenied, StaleReference
from aiagent_development.workspace_authority import register_verified_snapshot, resolve_git_commit_binding
from aiagent_domain import canonical_json, uuid7
from aiagent_domain.rpcwire import RpcError
from aiagent_gateways.context_keys import KeyRing
from aiagent_gateways.executors import ExecutorFailed, ExecutorUnknown
from aiagent_gateways.git_commit_dispatch import GitCommitReservationExecutor
from aiagent_gateways.git_operation import admit_git_operation
from aiagent_gateways.git_patch_artifacts import fetch_git_patch, git_patch_aad
from aiagent_gateways.git_receipts import GitReceiptError, GitReceiptStore
from aiagent_gateways.servers import build_tool_server
from aiagent_gateways.tool_gateway import ToolGateway
from psycopg.errors import CheckViolation, InsufficientPrivilege

pytestmark = [pytest.mark.pg, pytest.mark.skipif(os.name != "posix", reason="POSIX private staging")]


@pytest.fixture()
def verified_setup(tmp_path: Path, dbs, tenant):
    git = shutil.which("git")
    assert git
    root = tmp_path / "repo"
    root.mkdir()

    def run(*args: str) -> bytes:
        return subprocess.check_output([git, "-C", str(root), *args], stderr=subprocess.DEVNULL)

    run("init")
    run("config", "user.name", "Fixture")
    run("config", "user.email", "fixture@example.invalid")
    (root / "a.py").write_bytes(b"value = 42\n")
    run("add", "a.py")
    run("commit", "-m", "fixture")
    commit = run("rev-parse", "HEAD").decode().strip()
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    ids = commands.submit_task(dbs["api"], scope, "fixture", profile="sandbox")
    claim = control.claim_run(dbs["runtime"], scope.tenant_id, ids["runId"], "workspace-worker")
    binding_id = uuid7()
    dbs["owner"].one(
        "INSERT INTO agent.repository_bindings(tenant_id,id,owner_user_id,workspace_id,repository_ref,approved_commit,"
        "workspace_revision,approval_ref) VALUES (%s,%s,%s,%s,%s,%s,1,%s) RETURNING id",
        (
            claim.tenant_id,
            binding_id,
            claim.owner_user_id,
            scope.workspace_id,
            "fixture/repo",
            commit,
            "operator-fixture",
        ),
    )
    snapshot = GitSnapshot(
        AuthorizedSnapshot(Identity(claim.tenant_id, claim.owner_user_id, binding_id, "1", commit), root), Path(git)
    )
    manifest = prepare_projection(snapshot, snapshot.snapshot.identity, snapshot.tree)
    proof = build_git_tree_proof(snapshot)
    encoder = ProjectionEncoder(snapshot, manifest)
    wire = b"".join(encoder.chunks())
    assert encoder.receipt is not None
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    return claim, binding_id, manifest, proof, encoder.receipt, wire, parent


def test_git_binding_requires_explicit_branch_and_current_generation(dbs, verified_setup):
    claim, binding, manifest, _, _, _, _ = verified_setup
    workspace = str(
        dbs["owner"].one(
            "SELECT workspace_id FROM agent.repository_bindings WHERE tenant_id=%s AND id=%s",
            (claim.tenant_id, binding),
        )["workspace_id"]
    )

    def resolve(generation=1, repository="fixture/repo"):
        return resolve_git_commit_binding(
            dbs["tool_gateway"],
            claim,
            binding,
            workspace_id=workspace,
            repository=repository,
            generation=generation,
            parent=manifest.identity.commit,
            deadline=time.monotonic() + 5,
        )

    with pytest.raises(ScopeDenied):
        resolve()  # Existing bindings are not implicitly authorized to write.
    with pytest.raises(CheckViolation):
        dbs["owner"].one(
            "UPDATE agent.repository_bindings SET approved_branch='main',generation=generation+1 "
            "WHERE tenant_id=%s AND id=%s RETURNING id",
            (claim.tenant_id, binding),
        )
    dbs["owner"].one(
        "UPDATE agent.repository_bindings SET approved_branch='feat/approved',generation=generation+1 "
        "WHERE tenant_id=%s AND id=%s RETURNING id",
        (claim.tenant_id, binding),
    )
    assert resolve(2).branch == "feat/approved"
    with pytest.raises(ScopeDenied):
        resolve(1)
    with pytest.raises(ScopeDenied):
        resolve(2, "other/repo")
    dbs["owner"].one(
        "UPDATE agent.repository_bindings SET active=false,generation=generation+1 "
        "WHERE tenant_id=%s AND id=%s RETURNING id",
        (claim.tenant_id, binding),
    )
    with pytest.raises(ScopeDenied):
        resolve(3)


def test_git_operation_rejects_missing_canonical_attempt_and_patch(dbs, verified_setup):
    claim, binding, _, _, _, _, _ = verified_setup
    workspace = dbs["owner"].one(
        "SELECT workspace_id FROM agent.repository_bindings WHERE tenant_id=%s AND id=%s",
        (claim.tenant_id, binding),
    )["workspace_id"]
    with pytest.raises(InsufficientPrivilege), dbs["tool_gateway"].tx(tenant=claim.tenant_id) as c:
        c.execute(
            "SELECT agent.git_operation_admit(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                claim.tenant_id,
                claim.owner_user_id,
                workspace,
                claim.task_id,
                claim.run_id,
                claim.goal_revision,
                claim.lease_epoch,
                claim.worker_ref,
                claim.restore_incarnation,
                uuid7(),
                binding,
                uuid7(),
                uuid7(),
            ),
        )


def test_git_patch_reads_authenticated_scoped_bytes(dbs, verified_setup):
    claim, binding, manifest, _, _, _, _ = verified_setup
    dbs["owner"].one(
        "UPDATE agent.repository_bindings SET approved_branch='feat/approved',generation=generation+1 "
        "WHERE tenant_id=%s AND id=%s RETURNING id",
        (claim.tenant_id, binding),
    )
    workspace = str(
        dbs["owner"].one(
            "SELECT workspace_id FROM agent.repository_bindings WHERE tenant_id=%s AND id=%s",
            (claim.tenant_id, binding),
        )["workspace_id"]
    )
    artifact, cycle, content = uuid7(), "patch-" + uuid7(), b"approved patch bytes"
    digest, key, nonce = hashlib.sha256(content).hexdigest(), b"p" * 32, b"n" * 12
    parent, paths = manifest.identity.commit, ["a.py"]
    from psycopg.types.json import Jsonb

    dbs["owner"].one(
        "INSERT INTO agent.dev_cycles(tenant_id,id,owner_user_id,project_id,workspace_id,task_id,run_id,version,phase,cycle_json) "
        "VALUES (%s,%s,%s,'project',%s,%s,%s,1,'PATCH_READY',%s) RETURNING id",
        (
            claim.tenant_id,
            cycle,
            claim.owner_user_id,
            workspace,
            claim.task_id,
            claim.run_id,
            Jsonb(
                {
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
            ),
        ),
    )
    meta = {
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
        "byte_length": len(content),
        "key_id": "legacy",
    }
    keys = KeyRing.single(key)
    ciphertext = keys.cipher("legacy").encrypt(nonce, content, git_patch_aad(meta))
    dbs["owner"].one(
        "INSERT INTO agent.git_patch_artifacts(tenant_id,id,owner_user_id,workspace_id,binding_id,binding_generation,"
        "cycle_id,task_id,run_id,parent_sha,diff_sha256,paths,byte_length,key_id,nonce,ciphertext) "
        "VALUES (%s,%s,%s,%s,%s,2,%s,%s,%s,%s,%s,%s,%s,'legacy',%s,%s) RETURNING id",
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
            len(content),
            nonce,
            ciphertext,
        ),
    )

    def fetch(**kwargs):
        return fetch_git_patch(
            dbs["tool_gateway"],
            claim,
            keys,
            artifact,
            workspace_id=workspace,
            binding_id=binding,
            binding_generation=2,
            cycle_id=kwargs.get("cycle", cycle),
            plan_ref=kwargs.get("plan", "plan-fixture"),
            deadline=time.monotonic() + 5,
        )

    assert fetch().data == content
    # Real Gateway admission over authenticated RPC, stopping at UNKNOWN until
    # an isolated Git worker and durable result are connected.
    gateway_args = {
        "bindingId": binding,
        "bindingGeneration": 2,
        "patchId": artifact,
        "cycleId": cycle,
        "planRef": "plan-fixture",
        "branch": "feat/approved",
        "parent": parent,
        "patchSha256": digest,
        "paths": paths,
        "message": "approved fixture",
    }
    call_id = "development-git-" + uuid7()
    with dbs["model_gateway"].tx(tenant=claim.tenant_id) as c:
        control.append_head(
            c,
            claim,
            "assistant",
            {
                "kind": "assistant",
                "content": "",
                "tool_calls": [{"id": call_id, "name": "git.commit", "args": gateway_args}],
            },
            writer="development_git_adapter",
        )
    executor = GitCommitReservationExecutor(dbs["tool_gateway"], claim, keys)
    gateway = ToolGateway(dbs["tool_gateway"], {"git_commit": executor}, development_git_enabled=True)

    async def gateway_flow():
        server = build_tool_server({"development": b"d" * 32, "runtime": b"r" * 32}, "tcp:127.0.0.1:0", gateway)
        await server.start()
        try:
            endpoint = f"tcp:127.0.0.1:{server.port}"
            client = RpcClient(endpoint, "development", b"d" * 32)
            body = {"claim": claim.to_wire(), "toolCallId": call_id, "args": gateway_args}
            with pytest.raises(RpcError):
                await RpcClient(endpoint, "runtime", b"r" * 32).call("tool.development_git_commit", body)
            pending = await client.call("tool.development_git_commit", body)
            assert pending["status"] == "PENDING_APPROVAL"
            scope = Scope(claim.tenant_id, claim.owner_user_id, workspace)
            await asyncio.to_thread(
                commands.decide_approval,
                dbs["api"],
                scope,
                pending["approvalId"],
                "APPROVED",
                pending["requestSha256"],
                1,
            )
            result = await client.call("tool.development_git_commit", body)
            assert result["status"] == "UNKNOWN"
            repeated = await client.call("tool.development_git_commit", body)
            assert repeated["status"] == "UNKNOWN"
        finally:
            await server.close()

    asyncio.run(gateway_flow())
    rows = dbs["owner"].one(
        "SELECT count(*) AS n FROM agent.git_operations o JOIN agent.tool_attempts a "
        "ON a.tenant_id=o.tenant_id AND a.id=o.attempt_id JOIN agent.tool_intents i "
        "ON i.tenant_id=o.tenant_id AND i.id=o.intent_id WHERE o.tenant_id=%s AND i.invocation_key=%s "
        "AND a.status_code='UNKNOWN' AND i.status_code='UNKNOWN'",
        (claim.tenant_id, call_id),
    )
    assert rows["n"] == 1
    # SQL authorization fixture only: this is not a Gateway or Git E2E.
    intent, attempt, approval, effect = (uuid7() for _ in range(4))
    request_digest = "a" * 64
    effect_key = claim.tenant_id + ":" + intent
    arguments = {
        "bindingId": binding,
        "bindingGeneration": 2,
        "patchId": artifact,
        "cycleId": cycle,
        "planRef": "plan-fixture",
        "branch": "feat/approved",
        "parent": parent,
        "patchSha256": digest,
        "paths": paths,
        "message": "fixture",
    }
    with dbs["owner"].tx() as c:
        c.execute(
            "INSERT INTO agent.tool_intents(tenant_id,id,task_id,run_id,tool_key,invocation_key,request_sha256,"
            "safe_arguments_json,risk_class,goal_revision,status_code) VALUES (%s,%s,%s,%s,'git.commit',%s,%s,%s,"
            "'write_reversible',%s,'SENDING')",
            (
                claim.tenant_id,
                intent,
                claim.task_id,
                claim.run_id,
                intent,
                request_digest,
                Jsonb(arguments),
                claim.goal_revision,
            ),
        )
        c.execute(
            "INSERT INTO agent.tool_attempts(tenant_id,id,tool_intent_id,attempt_no,fence_token,worker_ref,"
            "run_lease_epoch,status_code) VALUES (%s,%s,%s,1,1,%s,%s,'SENDING')",
            (claim.tenant_id, attempt, intent, claim.worker_ref, claim.lease_epoch),
        )
        c.execute(
            "INSERT INTO agent.approval_records(tenant_id,id,tool_intent_id,task_id,run_id,owner_user_id,tool_key,"
            "request_sha256,goal_revision,safe_summary,decision_code,expires_at) VALUES "
            "(%s,%s,%s,%s,%s,%s,'git.commit',%s,%s,'fixture','APPROVED',now()+interval '1 hour')",
            (
                claim.tenant_id,
                approval,
                intent,
                claim.task_id,
                claim.run_id,
                claim.owner_user_id,
                request_digest,
                claim.goal_revision,
            ),
        )
        c.execute(
            "INSERT INTO agent.side_effect_records(tenant_id,id,tool_intent_id,effect_key,request_sha256,outcome_code,"
            "attempt_fence) VALUES (%s,%s,%s,%s,%s,'SENDING',1)",
            (claim.tenant_id, effect, intent, effect_key, request_digest),
        )

    def admit():
        return admit_git_operation(
            dbs["tool_gateway"],
            claim,
            workspace_id=workspace,
            attempt_id=attempt,
            binding_id=binding,
            patch_id=artifact,
            deadline=time.monotonic() + 5,
        )

    operation = admit()
    assert operation
    with pytest.raises(ExecutorUnknown):
        admit()

    def heartbeat():
        with dbs["tool_gateway"].tx(tenant=claim.tenant_id) as c:
            return c.execute(
                "SELECT agent.git_operation_heartbeat(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS expires_at",
                (
                    claim.tenant_id,
                    claim.owner_user_id,
                    workspace,
                    claim.task_id,
                    claim.run_id,
                    claim.goal_revision,
                    claim.lease_epoch,
                    claim.worker_ref,
                    claim.restore_incarnation,
                    operation,
                ),
            ).fetchone()["expires_at"]

    assert heartbeat() > time.time()

    # Receipt persistence unit fixture. This deliberately synthetic signature
    # and object verifier are not evidence of the Gateway/worker Git E2E.
    class FixtureObjectVerifier:
        def verify(self, result, patch, *, deadline):
            assert result["commit"] == "b" * 40 and patch.data == content and deadline > time.monotonic()

    signed_payload = {
        "operation": operation,
        "tenant": claim.tenant_id,
        "owner": claim.owner_user_id,
        "task": claim.task_id,
        "run": claim.run_id,
        "workspace": workspace,
        "goal": claim.goal_revision,
        "lease": claim.lease_epoch,
        "restore": claim.restore_incarnation,
        "worker_ref": claim.worker_ref,
        "intent": intent,
        "attempt": attempt,
        "effect_key": effect_key,
        "binding": binding,
        "generation": 2,
        "branch": "feat/approved",
        "parent": parent,
        "patch_id": artifact,
        "patch_sha256": digest,
        "patch_bytes": len(content),
        "paths": paths,
        "cycle_id": cycle,
        "plan_ref": "plan-fixture",
        "request_sha256": request_digest,
        "message": "fixture",
        "expires_at": int(time.time()) + 50,
        "commit": "b" * 40,
        "tree": "c" * 40,
        "cas": "CONFIRMED",
        "worker_id": "fixture-worker",
    }
    mac = hmac.new(
        b"s" * 32, b"aiagent-git-worker-result-v1\0" + canonical_json(signed_payload).encode(), hashlib.sha256
    ).hexdigest()
    envelope = {"payload": signed_payload, "mac": mac}
    receipt_store = GitReceiptStore(
        dbs["tool_gateway"],
        claim,
        keys,
        result_key=b"s" * 32,
        worker_id="fixture-worker",
        object_verifier=FixtureObjectVerifier(),
    )
    receipt_id = receipt_store.finish(operation, envelope, deadline=time.monotonic() + 5)
    with pytest.raises(InsufficientPrivilege):
        heartbeat()
    assert receipt_store.finish(operation, envelope, deadline=time.monotonic() + 5) == receipt_id
    restored, body_sha = receipt_store.fetch(operation, receipt_id, deadline=time.monotonic() + 5)
    assert (
        restored == signed_payload and body_sha == hashlib.sha256(canonical_json(signed_payload).encode()).hexdigest()
    )
    with pytest.raises(GitReceiptError):
        receipt_store.finish(
            operation, {"payload": {**signed_payload, "attempt": uuid7()}, "mac": mac}, deadline=time.monotonic() + 5
        )
    dbs["owner"].one(
        "UPDATE agent.approval_records SET decision_code='REVOKED' WHERE tenant_id=%s AND id=%s RETURNING id",
        (claim.tenant_id, approval),
    )
    with pytest.raises(ExecutorFailed):
        admit()
    with pytest.raises(GitReceiptError):
        receipt_store.fetch(operation, receipt_id, deadline=time.monotonic() + 5)
    with pytest.raises(ScopeDenied):
        fetch(cycle="other")
    with pytest.raises(ScopeDenied):
        fetch(plan="other")
    with pytest.raises(InvalidEvidence):
        fetch_git_patch(
            dbs["tool_gateway"],
            claim,
            KeyRing.single(b"q" * 32),
            artifact,
            workspace_id=workspace,
            binding_id=binding,
            binding_generation=2,
            cycle_id=cycle,
            plan_ref="plan-fixture",
            deadline=time.monotonic() + 5,
        )
    dbs["owner"].one(
        "UPDATE agent.git_patch_artifacts SET active=false WHERE tenant_id=%s AND id=%s RETURNING id",
        (claim.tenant_id, artifact),
    )
    with pytest.raises(ScopeDenied):
        fetch()


def test_register_actual_git_snapshot_and_retry_idempotently(dbs, verified_setup):
    claim, binding, manifest, proof, receipt, wire, parent = verified_setup
    args = (dbs["tool_gateway"], claim, binding, manifest, proof, receipt)
    result = register_verified_snapshot(*args, io.BytesIO(wire), parent, deadline=time.monotonic() + 10)
    repeated = register_verified_snapshot(*args, io.BytesIO(wire), parent, deadline=time.monotonic() + 10)
    assert result == repeated and result.commit == manifest.identity.commit and result.tree == manifest.tree
    assert result.hash_strength == "LEGACY_SHA1" and result.source_sha256 == receipt.sha256
    row = dbs["owner"].one(
        "SELECT count(*) AS n FROM agent.workspace_snapshot_attestations WHERE tenant_id=%s", (claim.tenant_id,)
    )
    assert row is not None and row["n"] == 1 and not list(parent.iterdir())


def test_reject_bad_proof_other_scope_and_stale_lease(dbs, verified_setup):
    claim, binding, manifest, proof, receipt, wire, parent = verified_setup
    with pytest.raises(InvalidEvidence):
        register_verified_snapshot(
            dbs["tool_gateway"],
            claim,
            binding,
            manifest,
            replace(proof, commit=proof.commit + b"x"),
            receipt,
            io.BytesIO(wire),
            parent,
            deadline=time.monotonic() + 10,
        )
    with pytest.raises((ScopeDenied, control.ControlError)):
        register_verified_snapshot(
            dbs["tool_gateway"],
            replace(claim, tenant_id=uuid7()),
            binding,
            manifest,
            proof,
            receipt,
            io.BytesIO(wire),
            parent,
            deadline=time.monotonic() + 10,
        )
    with pytest.raises(control.FencedOut):
        register_verified_snapshot(
            dbs["tool_gateway"],
            replace(claim, lease_epoch=claim.lease_epoch + 1),
            binding,
            manifest,
            proof,
            receipt,
            io.BytesIO(wire),
            parent,
            deadline=time.monotonic() + 10,
        )
    row = dbs["owner"].one(
        "SELECT count(*) AS n FROM agent.workspace_snapshot_attestations WHERE tenant_id=%s", (claim.tenant_id,)
    )
    assert row is not None and row["n"] == 0 and not list(parent.iterdir())


def test_binding_change_during_verification_is_rejected(dbs, verified_setup, monkeypatch):
    from aiagent_development import workspace_authority as module

    claim, binding, manifest, proof, receipt, wire, parent = verified_setup
    original = module.stage_git_projection

    @contextmanager
    def change_after_verify(*args, **kwargs):
        with original(*args, **kwargs) as staged:
            dbs["owner"].one(
                "UPDATE agent.repository_bindings SET generation=generation+1,workspace_revision=workspace_revision+1,"
                "approved_commit=%s WHERE tenant_id=%s AND id=%s RETURNING id",
                ("b" * 40, claim.tenant_id, binding),
            )
            yield staged

    monkeypatch.setattr(module, "stage_git_projection", change_after_verify)
    with pytest.raises(StaleReference, match="changed"):
        register_verified_snapshot(
            dbs["tool_gateway"],
            claim,
            binding,
            manifest,
            proof,
            receipt,
            io.BytesIO(wire),
            parent,
            deadline=time.monotonic() + 10,
        )
    assert not list(parent.iterdir())


def prepared_execution_fixture(dbs, verified_setup, *, on_prepared=None, expected_state=None, command_recipe=False):
    import asyncio
    from dataclasses import asdict

    from aiagent_development.contracts import Cycle, Phase, ProjectScope, RunFence, TaskPlan
    from aiagent_development.pg_store import PostgresCycleStore
    from aiagent_development.workspace_authority import approved_recipe_digest, prepare_execution
    from aiagent_domain import sha256_hex
    from aiagent_gateways.tool_gateway import ToolGateway
    from psycopg.types.json import Jsonb

    claim, binding, manifest, proof, receipt, wire, parent = verified_setup
    snapshot = register_verified_snapshot(
        dbs["tool_gateway"],
        claim,
        binding,
        manifest,
        proof,
        receipt,
        io.BytesIO(wire),
        parent,
        deadline=time.monotonic() + 10,
    )
    scope = ProjectScope(
        claim.tenant_id,
        claim.owner_user_id,
        "project",
        snapshot.workspace_id,
        "fixture/repo",
        "feature",
        snapshot.commit,
        str(snapshot.revision),
    )
    fence = RunFence(
        claim.task_id, claim.run_id, claim.goal_revision, claim.lease_epoch, claim.restore_incarnation, claim.worker_ref
    )
    plan = TaskPlan("plan", "change", ("unit",), claim.goal_revision)
    cycle = Cycle("fixture-cycle", scope, fence, Phase.PLANNED, "change", plan=plan)
    PostgresCycleStore(dbs["tool_gateway"]).create(cycle)
    args = {"argv": ["/usr/bin/true" if command_recipe else "approved-test"], "cwdRelative": "."}
    request_sha = sha256_hex({"tool": "sandbox_exec", "args": args})
    if command_recipe:
        from aiagent_sandbox.command_authority import command_template_digest

        template_sha = command_template_digest({"kind": "test", "test_id": "unit", **args})
    else:
        template_sha = "d" * 64
    recipe_id, changed_recipe, wrong_test, bad_digest = uuid7(), uuid7(), uuid7(), uuid7()
    for rid, req_sha, test_id in (
        (recipe_id, request_sha, "unit"),
        (changed_recipe, "f" * 64, "unit"),
        (wrong_test, request_sha, "not-in-plan"),
        (bad_digest, request_sha, "unit"),
    ):
        dbs["owner"].one(
            "INSERT INTO agent.execution_recipe_approvals(tenant_id,id,cycle_id,goal_revision,plan_sha256,plan_json,kind,test_id,"
            "request_sha256,template_sha256,recipe_sha256,toolchain_sha256,image_digest,approval_ref)"
            " VALUES (%s,%s,%s,%s,%s,%s,'test',%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                claim.tenant_id,
                rid,
                cycle.cycle_id,
                claim.goal_revision,
                sha256_hex(asdict(plan)),
                Jsonb(asdict(plan)),
                test_id,
                req_sha,
                template_sha,
                "e" * 64
                if rid == bad_digest
                else approved_recipe_digest(
                    kind="test",
                    test_id=test_id,
                    plan_sha256=sha256_hex(asdict(plan)),
                    request_sha256=req_sha,
                    template_sha256=template_sha,
                    toolchain_sha256="b" * 64,
                    image_digest="sha256:" + "c" * 64,
                ),
                "b" * 64,
                "sha256:" + "c" * 64,
                "operator-fixture",
            ),
        )
    with dbs["model_gateway"].tx() as c:
        control.append_head(
            c,
            claim,
            "assistant",
            {
                "kind": "assistant",
                "content": "",
                "tool_calls": [{"id": "prepare-call", "name": "sandbox_exec", "args": args}],
            },
            writer="test",
            request_key=os.urandom(32).hex(),
        )

    observed = []

    class FakeCell:
        def __init__(self):
            self.context = {}

        def destination(self):
            return None

        async def execute(self, key, _args, _permit):
            ctx = self.context[key]
            attempt = ctx["attempt_id"]
            deadline = time.monotonic() + 10
            with pytest.raises(StaleReference):
                prepare_execution(dbs["tool_gateway"], claim, attempt, uuid7(), recipe_id, deadline=deadline)
            with pytest.raises(ScopeDenied):
                prepare_execution(dbs["tool_gateway"], claim, attempt, snapshot.id, changed_recipe, deadline=deadline)
            with pytest.raises(ScopeDenied):
                prepare_execution(dbs["tool_gateway"], claim, attempt, snapshot.id, wrong_test, deadline=deadline)
            with pytest.raises(InvalidEvidence, match="recipe digest"):
                prepare_execution(dbs["tool_gateway"], claim, attempt, snapshot.id, bad_digest, deadline=deadline)
            dbs["owner"].one(
                "UPDATE agent.dev_cycles SET cycle_json=jsonb_set(cycle_json, '{plan,required_tests}', %s::jsonb)"
                " WHERE tenant_id=%s AND id=%s RETURNING id",
                ('["altered"]', claim.tenant_id, cycle.cycle_id),
            )
            with pytest.raises(StaleReference, match="Plan changed"):
                prepare_execution(dbs["tool_gateway"], claim, attempt, snapshot.id, recipe_id, deadline=deadline)
            dbs["owner"].one(
                "UPDATE agent.dev_cycles SET cycle_json=jsonb_set(cycle_json, '{plan,required_tests}', %s::jsonb)"
                " WHERE tenant_id=%s AND id=%s RETURNING id",
                ('["unit"]', claim.tenant_id, cycle.cycle_id),
            )
            with pytest.raises(control.FencedOut):
                prepare_execution(
                    dbs["tool_gateway"],
                    replace(claim, lease_epoch=claim.lease_epoch + 1),
                    attempt,
                    snapshot.id,
                    recipe_id,
                    deadline=deadline,
                )
            prepared = prepare_execution(dbs["tool_gateway"], claim, attempt, snapshot.id, recipe_id, deadline=deadline)
            assert (
                prepare_execution(dbs["tool_gateway"], claim, attempt, snapshot.id, recipe_id, deadline=deadline)
                == prepared
            )
            observed.append(prepared)
            if on_prepared is not None:
                on_prepared(claim, prepared, snapshot, parent)
            return {"fixture": "no real Cell execution"}

        async def query(self, key, permit=None):
            return None

    gateway = ToolGateway(dbs["tool_gateway"], {"sandbox_cell": FakeCell()})
    result = asyncio.run(gateway.submit(claim, "prepare-call", "sandbox_exec", args))
    assert result["status"] == "CONFIRMED" and len(observed) == 1
    prepared = observed[0]
    assert prepared.id != prepared.attempt_id and prepared.test_id == "unit"
    assert len(prepared.binding_sha256) == 64 and prepared.source_sha256 == receipt.sha256
    assert prepared.commit == snapshot.commit and prepared.tree == snapshot.tree
    row = dbs["owner"].one(
        "SELECT state, artifact_sha256 FROM agent.execution_instances WHERE tenant_id=%s AND id=%s",
        (claim.tenant_id, prepared.id),
    )
    assert row is not None
    if expected_state == "RUNNING_OR_PREPARED":
        assert row["state"] in ("RUNNING", "PREPARED")
    else:
        assert row["state"] == (expected_state or ("RUNNING" if on_prepared else "PREPARED"))
    assert (row["artifact_sha256"] is not None) == (expected_state == "PUBLISHED")
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege), dbs["tool_gateway"].tx(tenant=claim.tenant_id) as c:
        c.execute(
            "UPDATE agent.execution_instances SET state='PUBLISHED',artifact_sha256=%s,artifact_bytes=1"
            " WHERE tenant_id=%s AND id=%s",
            ("a" * 64, claim.tenant_id, prepared.id),
        )
    if expected_state != "FINISHED":
        with pytest.raises(psycopg.errors.CheckViolation), dbs["owner"].tx() as c:
            c.execute(
                "UPDATE agent.execution_instances SET state='PUBLISHED',artifact_sha256=%s,artifact_bytes=1"
                " WHERE tenant_id=%s AND id=%s",
                ("a" * 64, claim.tenant_id, prepared.id),
            )
    with pytest.raises(psycopg.errors.InsufficientPrivilege), dbs["runtime"].conn() as c:
        c.execute("SELECT * FROM agent.execution_instances")
    return claim, prepared, snapshot, parent


def test_source_session_issuance_is_fenced_and_single_use(dbs, verified_setup):
    from aiagent_gateways.execution_artifacts import ExecutionArtifactError
    from aiagent_gateways.execution_capture_proof import HmacCaptureVerifier
    from aiagent_gateways.execution_lifecycle import ExecutionLifecycle
    from aiagent_sandbox.source_ticket import verify_source_ticket

    _, _, manifest, _, receipt, _, _ = verified_setup
    issued = []

    def issue(claim, prepared, _snapshot, _parent):
        key = b"s" * 32
        verifier = HmacCaptureVerifier(dbs["tool_gateway"], b"r" * 32)
        lifecycle = ExecutionLifecycle(dbs["tool_gateway"], claim, verifier)
        with pytest.raises(ExecutionArtifactError, match="SOURCE_SESSION_DENIED"):
            lifecycle.issue_source_session(
                prepared, manifest, replace(receipt, sha256="f" * 64), key, deadline=time.monotonic() + 10
            )
        with pytest.raises(control.FencedOut):
            ExecutionLifecycle(
                dbs["tool_gateway"], replace(claim, lease_epoch=claim.lease_epoch + 1), verifier
            ).issue_source_session(prepared, manifest, receipt, key, deadline=time.monotonic() + 10)
        ticket = lifecycle.issue_source_session(prepared, manifest, receipt, key, deadline=time.monotonic() + 10)
        payload = verify_source_ticket(key, ticket)
        assert payload["execution"] == prepared.id and payload["attempt"] == prepared.attempt_id
        assert payload["snapshot"] == prepared.snapshot_id and payload["binding_sha256"] == prepared.binding_sha256
        assert payload["source_sha256"] == receipt.sha256
        with pytest.raises(ExecutionArtifactError, match="INDEX_RECIPE_DENIED"):
            lifecycle.running_index_binding(prepared, deadline=time.monotonic() + 10)
        with pytest.raises(ExecutionArtifactError, match="SOURCE_SESSION_DENIED"):
            lifecycle.issue_source_session(prepared, manifest, receipt, key, deadline=time.monotonic() + 10)
        with pytest.raises(ValueError):
            verify_source_ticket(b"x" * 32, ticket)
        issued.append(ticket)

    claim, prepared, _, _ = prepared_execution_fixture(dbs, verified_setup, on_prepared=issue)
    assert len(issued) == 1
    row = dbs["owner"].one(
        "SELECT s.session_id,e.state FROM agent.execution_source_sessions s JOIN agent.execution_instances e "
        "ON e.tenant_id=s.tenant_id AND e.id=s.execution_id WHERE s.tenant_id=%s AND s.execution_id=%s",
        (claim.tenant_id, prepared.id),
    )
    assert row is not None and str(row["session_id"]) == issued[0]["payload"]["session"] and row["state"] == "RUNNING"


def test_command_receipt_persists_and_rehydrates_canonical_scope(dbs, verified_setup, tmp_path):
    """DB binding test with a synthetic process result, not a command E2E."""
    import tempfile

    from aiagent_gateways.command_dispatch import AuthorizedCommandExecutor
    from aiagent_gateways.command_receipt_reader import CommandReceiptReader
    from aiagent_gateways.execution_artifacts import ExecutionArtifactError
    from aiagent_gateways.execution_capture_proof import HmacCaptureVerifier
    from aiagent_gateways.execution_lifecycle import ExecutionLifecycle
    from aiagent_sandbox.command_authority import issue_command_grant
    from aiagent_sandbox.command_receipt import sign_command_receipt
    from aiagent_sandbox.grant import verify

    _, _, manifest, git_proof, receipt, wire, _ = verified_setup

    def finish(claim, prepared, snapshot, parent):
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        lifecycle = ExecutionLifecycle(dbs["tool_gateway"], claim, HmacCaptureVerifier(dbs["tool_gateway"], b"c" * 32))
        ticket = lifecycle.issue_source_session(prepared, manifest, receipt, b"t" * 32, deadline=time.monotonic() + 10)
        binding, descriptor = lifecycle.running_command_binding(prepared, deadline=time.monotonic() + 10)
        token = issue_command_grant(b"t" * 32, b"g" * 32, ticket, binding, descriptor)
        grant = verify(b"g" * 32, token, descriptor["argv"], ".")
        result = {
            "status": "EXITED",
            "executionId": prepared.id,
            "attemptId": prepared.attempt_id,
            "exitCode": 0,
            "timedOut": False,
            "truncated": False,
            "stdout": "secret",
            "stderr": "",
        }
        body, proof = sign_command_receipt(b"c" * 32, grant, binding, descriptor, result)
        with tempfile.TemporaryFile(dir=tmp_path) as f:
            f.write(body)
            f.flush()
            fd = os.open(f"/proc/self/fd/{f.fileno()}", os.O_RDONLY)
            try:
                with pytest.raises(ExecutionArtifactError, match="COMMAND_RECEIPT_INVALID"):
                    lifecycle.finish_command(prepared, fd, b"0" * 64, b"c" * 32, deadline=time.monotonic() + 10)
                receipt_id, payload, digest = lifecycle.finish_command(
                    prepared, fd, proof, b"c" * 32, deadline=time.monotonic() + 10
                )
            finally:
                os.close(fd)
        reader = CommandReceiptReader(lifecycle, b"c" * 32)
        loaded, loaded_digest = reader(prepared.id, receipt_id)
        assert loaded == payload and loaded_digest == digest and loaded["test_id"] == "unit"
        with pytest.raises(ExecutionArtifactError):
            reader(prepared.id, uuid7())
        with pytest.raises(ExecutionArtifactError):
            CommandReceiptReader(lifecycle, b"x" * 32)(prepared.id, receipt_id)
        stale = ExecutionLifecycle(
            dbs["tool_gateway"],
            replace(claim, lease_epoch=claim.lease_epoch + 1),
            HmacCaptureVerifier(dbs["tool_gateway"], b"c" * 32),
        )
        with pytest.raises(control.FencedOut):
            CommandReceiptReader(stale, b"c" * 32)(prepared.id, receipt_id)
        executor = AuthorizedCommandExecutor(
            dbs["tool_gateway"],
            claim,
            snapshot.id,
            prepared.recipe_id,
            lifecycle,
            manifest,
            receipt,
            git_proof,
            [wire],
            "unix:/unused",
            b"r" * 32,
            b"t" * 32,
            b"g" * 32,
            b"c" * 32,
            parent,
        )

        def query():
            return asyncio.run(executor.query(f"{claim.tenant_id}:{prepared.intent_id}"))

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: query(), range(3)))
        assert all(
            item is not None
            and item["sent"] is True
            and item["receipt"] == "command:" + receipt_id
            and item["commandResult"]["executionId"] == prepared.id
            and item["commandResult"]["bodySha256"] == digest
            for item in results
        )
        dbs["owner"].one(
            "UPDATE agent.execution_command_receipts SET proof=%s WHERE tenant_id=%s AND execution_id=%s RETURNING receipt_id",
            (b"0" * 64, claim.tenant_id, prepared.id),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(query).result(timeout=10) is None
        dbs["owner"].one(
            "UPDATE agent.execution_command_receipts SET proof=%s WHERE tenant_id=%s AND execution_id=%s RETURNING receipt_id",
            (proof, claim.tenant_id, prepared.id),
        )
        assert b"secret" not in body

    prepared_execution_fixture(dbs, verified_setup, on_prepared=finish, expected_state="FINISHED", command_recipe=True)


def test_concurrent_source_session_issuance_allows_one(dbs, verified_setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from aiagent_gateways.execution_artifacts import ExecutionArtifactError
    from aiagent_gateways.execution_capture_proof import HmacCaptureVerifier
    from aiagent_gateways.execution_lifecycle import ExecutionLifecycle

    _, _, manifest, _, receipt, _, _ = verified_setup
    outcomes = []

    def issue(claim, prepared, _snapshot, _parent):
        lifecycle = ExecutionLifecycle(dbs["tool_gateway"], claim, HmacCaptureVerifier(dbs["tool_gateway"], b"v" * 32))
        barrier = Barrier(2)

        def candidate():
            barrier.wait(timeout=5)
            try:
                return lifecycle.issue_source_session(
                    prepared, manifest, receipt, b"s" * 32, deadline=time.monotonic() + 10
                )
            except ExecutionArtifactError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes.extend(pool.map(lambda _: candidate(), range(2)))

    claim, prepared, _, _ = prepared_execution_fixture(dbs, verified_setup, on_prepared=issue)
    assert sum(result is not None for result in outcomes) == 1
    row = dbs["owner"].one(
        "SELECT count(*) AS n FROM agent.execution_source_sessions WHERE tenant_id=%s AND execution_id=%s",
        (claim.tenant_id, prepared.id),
    )
    assert row is not None and row["n"] == 1


def test_distinct_attempts_cannot_start_same_test_on_same_snapshot(dbs, verified_setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from aiagent_development.workspace_authority import prepare_execution
    from aiagent_gateways.execution_artifacts import ExecutionArtifactError
    from aiagent_gateways.execution_capture_proof import HmacCaptureVerifier
    from aiagent_gateways.execution_lifecycle import ExecutionLifecycle

    _, _, manifest, _, receipt, _, _ = verified_setup
    outcomes = []

    def race(claim, prepared, snapshot, _parent):
        second_attempt = uuid7()
        dbs["owner"].one(
            "INSERT INTO agent.tool_attempts(tenant_id,id,tool_intent_id,attempt_no,fence_token,worker_ref,"
            "run_lease_epoch,status_code) SELECT tenant_id,%s,tool_intent_id,attempt_no+1,fence_token+1,"
            "worker_ref,run_lease_epoch,'SENDING' FROM agent.tool_attempts WHERE tenant_id=%s AND id=%s RETURNING id",
            (second_attempt, claim.tenant_id, prepared.attempt_id),
        )
        other = prepare_execution(
            dbs["tool_gateway"], claim, second_attempt, snapshot.id, prepared.recipe_id, deadline=time.monotonic() + 10
        )
        lifecycle = ExecutionLifecycle(dbs["tool_gateway"], claim, HmacCaptureVerifier(dbs["tool_gateway"], b"v" * 32))
        barrier = Barrier(2)

        def issue(candidate):
            barrier.wait(timeout=5)
            try:
                lifecycle.issue_source_session(candidate, manifest, receipt, b"s" * 32, deadline=time.monotonic() + 10)
                return "STARTED"
            except ExecutionArtifactError:
                return "DENIED"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes.extend(pool.map(issue, (prepared, other)))

    claim, _, snapshot, _ = prepared_execution_fixture(
        dbs, verified_setup, on_prepared=race, expected_state="RUNNING_OR_PREPARED"
    )
    assert sorted(outcomes) == ["DENIED", "STARTED"]
    rows = dbs["owner"].all(
        "SELECT state FROM agent.execution_instances WHERE tenant_id=%s AND snapshot_id=%s",
        (claim.tenant_id, snapshot.id),
    )
    assert sorted(row["state"] for row in rows) == ["PREPARED", "RUNNING"]


def test_ambiguous_source_registration_fences_execution_as_unknown(dbs, verified_setup):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from aiagent_gateways.execution_artifacts import ExecutionArtifactError
    from aiagent_gateways.execution_capture_proof import HmacCaptureVerifier
    from aiagent_gateways.execution_lifecycle import ExecutionLifecycle
    from aiagent_gateways.source_transfer import SourceRegistrationUnknown, register_issued_source

    _, _, manifest, proof, receipt, _, _ = verified_setup

    def issue(claim, prepared, _snapshot, _parent):
        verifier = HmacCaptureVerifier(dbs["tool_gateway"], b"v" * 32)
        lifecycle = ExecutionLifecycle(dbs["tool_gateway"], claim, verifier)
        ticket = lifecycle.issue_source_session(prepared, manifest, receipt, b"s" * 32, deadline=time.monotonic() + 10)
        with pytest.raises(ExecutionArtifactError, match="EXECUTION_UNKNOWN_DENIED"):
            lifecycle.mark_unknown(prepared.id, "f" * 64, deadline=time.monotonic() + 10)

        def failed_registration():
            with pytest.raises(SourceRegistrationUnknown):
                asyncio.run(
                    register_issued_source(
                        lifecycle,
                        prepared,
                        "unix:/nonexistent/source.sock",
                        b"r" * 32,
                        b"s" * 32,
                        ticket,
                        manifest,
                        proof,
                        deadline=time.monotonic() + 5,
                    )
                )

        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(failed_registration).result(timeout=10)
        stale = ExecutionLifecycle(dbs["tool_gateway"], replace(claim, lease_epoch=claim.lease_epoch + 1), verifier)
        stale.mark_unknown(prepared.id, prepared.binding_sha256, deadline=time.monotonic() + 10)
        lifecycle.mark_unknown(prepared.id, prepared.binding_sha256, deadline=time.monotonic() + 10)
        with pytest.raises(ExecutionArtifactError, match="SOURCE_SESSION_DENIED"):
            lifecycle.issue_source_session(prepared, manifest, receipt, b"s" * 32, deadline=time.monotonic() + 10)

    claim, prepared, _, _ = prepared_execution_fixture(dbs, verified_setup, on_prepared=issue, expected_state="UNKNOWN")
    row = dbs["owner"].one(
        "SELECT state FROM agent.execution_instances WHERE tenant_id=%s AND id=%s", (claim.tenant_id, prepared.id)
    )
    assert row is not None and row["state"] == "UNKNOWN"


@pytest.mark.skipif(sys.platform != "linux", reason="requires Unix socket and supervisor replay journal")
def test_db_issued_source_ticket_registers_over_supervisor_rpc(dbs, verified_setup, tmp_path):
    """Registration integration only: the test consumer is not a live authority bridge."""
    import asyncio

    from aiagent_domain.rpcwire import RpcError
    from aiagent_gateways.execution_capture_proof import HmacCaptureVerifier
    from aiagent_gateways.execution_lifecycle import ExecutionLifecycle
    from aiagent_gateways.source_transfer import register_source, send_source
    from aiagent_sandbox.source_rpc import SourceSessions
    from aiagent_sandbox.source_ticket import sign_source_ticket
    from aiagent_sandbox.supervisor import Supervisor, serve

    _, _, manifest, proof, receipt, wire, parent = verified_setup
    issued = []
    ticket_key, rpc_key = b"s" * 32, b"r" * 32

    def issue(claim, prepared, _snapshot, _parent):
        lifecycle = ExecutionLifecycle(dbs["tool_gateway"], claim, HmacCaptureVerifier(dbs["tool_gateway"], b"v" * 32))
        issued.append(
            lifecycle.issue_source_session(prepared, manifest, receipt, ticket_key, deadline=time.monotonic() + 30)
        )

    prepared_execution_fixture(dbs, verified_setup, on_prepared=issue)
    assert len(issued) == 1
    observed = []

    def staged(fd):
        f = os.open(manifest.files[0].path, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            observed.append(os.read(f, 1024))
        finally:
            os.close(f)

    async def exercise():
        supervisor = Supervisor(tmp_path / "supervisor", b"g" * 32, cell_uid=20001, cell_gid=20001)
        active = [False]
        sessions = SourceSessions(
            parent, ticket_key=ticket_key, registration_factory=lambda _: (lambda: active[0], staged)
        )
        endpoint = f"unix:{tmp_path / 'supervisor.sock'}"
        server = asyncio.create_task(serve(supervisor, {"tool_gateway": rpc_key}, endpoint, source_sessions=sessions))
        deadline = time.monotonic() + 10
        try:
            for _ in range(100):
                if (tmp_path / "supervisor.sock").exists():
                    break
                await asyncio.sleep(0.01)
            old_key = b"x" * 32
            old_ticket = sign_source_ticket(old_key, issued[0]["payload"])
            with pytest.raises(RpcError):
                await register_source(endpoint, rpc_key, old_key, old_ticket, manifest, proof, deadline=deadline)
            with pytest.raises(RpcError):
                await register_source(endpoint, rpc_key, ticket_key, issued[0], manifest, proof, deadline=deadline)
            active[0] = True
            with pytest.raises(RpcError):
                await register_source(
                    endpoint,
                    rpc_key,
                    ticket_key,
                    issued[0],
                    replace(manifest, total_bytes=manifest.total_bytes + 1),
                    proof,
                    deadline=deadline,
                )
            sid, transfer_key, binding = await register_source(
                endpoint, rpc_key, ticket_key, issued[0], manifest, proof, deadline=deadline
            )
            await send_source(endpoint, rpc_key, sid, transfer_key, binding, [wire], deadline=deadline)
            assert observed and not list(parent.iterdir())
            with pytest.raises(RpcError):
                await register_source(endpoint, rpc_key, ticket_key, issued[0], manifest, proof, deadline=deadline)
            with pytest.raises(ValueError):
                await register_source(endpoint, rpc_key, b"x" * 32, issued[0], manifest, proof, deadline=deadline)
            server.cancel()
            await asyncio.gather(server, return_exceptions=True)
            (tmp_path / "supervisor.sock").unlink(missing_ok=True)
            restarted = Supervisor(tmp_path / "supervisor", b"g" * 32, cell_uid=20001, cell_gid=20001)
            fresh_sessions = SourceSessions(
                parent, ticket_key=ticket_key, registration_factory=lambda _: (lambda: True, staged)
            )
            server = asyncio.create_task(
                serve(restarted, {"tool_gateway": rpc_key}, endpoint, source_sessions=fresh_sessions)
            )
            for _ in range(100):
                if (tmp_path / "supervisor.sock").exists():
                    break
                await asyncio.sleep(0.01)
            with pytest.raises(RpcError):
                await register_source(endpoint, rpc_key, ticket_key, issued[0], manifest, proof, deadline=deadline)
        finally:
            server.cancel()
            await asyncio.gather(server, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.skipif(sys.platform != "linux", reason="requires Unix socket and POSIX descriptors")
@pytest.mark.parametrize("tamper_receipt", [False, True])
def test_fixture_capture_transfer_drives_fenced_db_publication(dbs, verified_setup, tmp_path, tamper_receipt):
    """Synthetic capture and SCIP validator: exercises transport and real DB fences only."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from aiagent_development.code_intelligence.artifact_capture import capture_scip
    from aiagent_development.workspace_authority import _binding_payload
    from aiagent_gateways.context_keys import KeyRing
    from aiagent_gateways.execution_artifacts import PostgresExecutionArtifactStore
    from aiagent_gateways.execution_capture_proof import HmacCaptureVerifier
    from aiagent_gateways.execution_lifecycle import ExecutionLifecycle
    from aiagent_gateways.source_transfer import SourceCaptureUnknown, capture_finish_publish, register_issued_source
    from aiagent_sandbox.capture_signing import sign_capture
    from aiagent_sandbox.grant import Grant
    from aiagent_sandbox.source_rpc import CapturedArtifact, SourceSessions
    from aiagent_sandbox.supervisor import Supervisor, serve

    _, _, manifest, proof, receipt, wire, parent = verified_setup
    ticket_key, rpc_key, capture_key = b"t" * 32, b"r" * 32, b"c" * 32
    artifact = b"synthetic-test-index"
    outcome = []

    def issue(claim, prepared, snapshot, _parent):
        verifier = HmacCaptureVerifier(dbs["tool_gateway"], capture_key)
        lifecycle = ExecutionLifecycle(dbs["tool_gateway"], claim, verifier)
        ticket = lifecycle.issue_source_session(prepared, manifest, receipt, ticket_key, deadline=time.monotonic() + 30)
        execution = dbs["owner"].one(
            "SELECT * FROM agent.execution_instances WHERE tenant_id=%s AND id=%s", (claim.tenant_id, prepared.id)
        )
        snap = dbs["owner"].one(
            "SELECT * FROM agent.workspace_snapshot_attestations WHERE tenant_id=%s AND id=%s",
            (claim.tenant_id, snapshot.id),
        )
        recipe = dbs["owner"].one(
            "SELECT * FROM agent.execution_recipe_approvals WHERE tenant_id=%s AND id=%s",
            (claim.tenant_id, prepared.recipe_id),
        )
        binding = _binding_payload(execution, snap, recipe)
        grant = Grant(
            "fixture",
            claim.tenant_id,
            claim.task_id,
            claim.run_id,
            prepared.intent_id,
            claim.lease_epoch,
            ("fixture",),
            ".",
            1000,
            1024,
            int(time.time()) + 30,
            attempt_id=prepared.attempt_id,
            attempt_binding_version=1,
            execution_binding_version=1,
            execution_id=prepared.id,
            execution_binding_sha256=prepared.binding_sha256,
        )
        output = tmp_path / "output-capture"
        output.mkdir()
        (output / "index.scip").write_bytes(artifact)
        output_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with capture_scip(output_fd, parent, expected_uid=os.geteuid(), deadline=time.monotonic() + 10) as captured:
                signed = sign_capture(
                    capture_key,
                    grant,
                    binding,
                    captured.fd,
                    expected_sha256=captured.sha256,
                    size=captured.byte_length,
                    deadline=time.monotonic() + 10,
                )
        finally:
            os.close(output_fd)
        if tamper_receipt:
            signed = b"{}"

        class FixtureValidator:
            def verify(self, checked_claim, execution_id, content):
                assert checked_claim == claim and execution_id == prepared.id and content == artifact
                return "PARTIAL"

        store = PostgresExecutionArtifactStore(
            dbs["tool_gateway"], claim, KeyRing.single(b"k" * 32, "fixture"), capture=verifier, index=FixtureValidator()
        )
        gateway_parent = tmp_path / "gateway-capture"
        gateway_parent.mkdir(mode=0o700)

        async def exercise():
            sup = Supervisor(tmp_path / "capture-supervisor", b"g" * 32, cell_uid=20001, cell_gid=20001)
            sessions = SourceSessions(
                parent,
                ticket_key=ticket_key,
                registration_factory=lambda _: (lambda: True, lambda fd: CapturedArtifact(artifact, signed)),
            )
            endpoint = f"unix:{tmp_path / 'capture.sock'}"
            server = asyncio.create_task(serve(sup, {"tool_gateway": rpc_key}, endpoint, source_sessions=sessions))
            deadline = time.monotonic() + 20
            try:
                for _ in range(100):
                    if (tmp_path / "capture.sock").exists():
                        break
                    await asyncio.sleep(0.01)
                sid, key, source_binding = await register_issued_source(
                    lifecycle, prepared, endpoint, rpc_key, ticket_key, ticket, manifest, proof, deadline=deadline
                )

                async def execute():
                    return await capture_finish_publish(
                        lifecycle,
                        store,
                        prepared,
                        endpoint,
                        rpc_key,
                        ticket_key,
                        ticket,
                        sid,
                        key,
                        source_binding,
                        [wire],
                        gateway_parent,
                        deadline=deadline,
                        active=lambda: True,
                    )

                if tamper_receipt:
                    with pytest.raises(SourceCaptureUnknown):
                        await execute()
                else:
                    outcome.append(await execute())
            finally:
                server.cancel()
                await asyncio.gather(server, return_exceptions=True)

        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(lambda: asyncio.run(exercise())).result(timeout=25)

    expected = "UNKNOWN" if tamper_receipt else "PUBLISHED"
    claim, prepared, _, _ = prepared_execution_fixture(dbs, verified_setup, on_prepared=issue, expected_state=expected)
    row = dbs["owner"].one(
        "SELECT state,artifact_sha256 FROM agent.execution_instances WHERE tenant_id=%s AND id=%s",
        (claim.tenant_id, prepared.id),
    )
    assert row is not None and row["state"] == expected
    assert bool(outcome) is not tamper_receipt
    assert (row["artifact_sha256"] is None) is tamper_receipt


@pytest.mark.skipif(sys.platform != "linux", reason="requires Unix socket and supervisor journal")
def test_source_heartbeat_revokes_live_session_when_snapshot_changes(dbs, verified_setup, tmp_path):
    """Real DB and RPC heartbeat; the staged consumer is test-only."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from aiagent_core.rpc import RpcClient
    from aiagent_gateways.execution_capture_proof import HmacCaptureVerifier
    from aiagent_gateways.execution_lifecycle import ExecutionLifecycle
    from aiagent_gateways.execution_watch import pulse_source_authority
    from aiagent_gateways.source_transfer import register_issued_source, send_source
    from aiagent_sandbox.source_rpc import SourceAuthority, SourceSessions
    from aiagent_sandbox.supervisor import Supervisor, serve

    _, binding_id, manifest, proof, receipt, wire, parent = verified_setup

    def issue(claim, prepared, _snapshot, _parent):
        ticket_key, rpc_key = b"t" * 32, b"r" * 32
        lifecycle = ExecutionLifecycle(dbs["tool_gateway"], claim, HmacCaptureVerifier(dbs["tool_gateway"], b"v" * 32))
        ticket = lifecycle.issue_source_session(prepared, manifest, receipt, ticket_key, deadline=time.monotonic() + 30)

        async def exercise():
            staged = asyncio.Event()

            def factory(payload):
                authority = SourceAuthority(payload["expires_at"], heartbeat_seconds=2)

                async def consume(_fd):
                    staged.set()
                    await authority.revoked.wait()

                return authority, consume

            sup = Supervisor(tmp_path / "heartbeat-supervisor", b"g" * 32, cell_uid=20001, cell_gid=20001)
            sessions = SourceSessions(parent, ticket_key=ticket_key, registration_factory=factory)
            endpoint = f"unix:{tmp_path / 'heartbeat.sock'}"
            server = asyncio.create_task(serve(sup, {"tool_gateway": rpc_key}, endpoint, source_sessions=sessions))
            revoked, stop, ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
            pulse = None
            send = None
            deadline = time.monotonic() + 20
            try:
                for _ in range(100):
                    if (tmp_path / "heartbeat.sock").exists():
                        break
                    await asyncio.sleep(0.01)
                sid, key, source_binding = await register_issued_source(
                    lifecycle, prepared, endpoint, rpc_key, ticket_key, ticket, manifest, proof, deadline=deadline
                )
                scope = {
                    "tenantId": claim.tenant_id,
                    "sessionId": sid,
                    "executionId": prepared.id,
                    "attemptId": prepared.attempt_id,
                    "bindingSha256": prepared.binding_sha256,
                }
                wrong = await RpcClient(endpoint, "tool_gateway", rpc_key).call(
                    "source.heartbeat", {**scope, "attemptId": "wrong"}
                )
                assert wrong == {"renewed": False}
                pulse = asyncio.create_task(
                    pulse_source_authority(
                        lifecycle,
                        endpoint,
                        rpc_key,
                        revoked,
                        stop,
                        ready,
                        tenant_id=claim.tenant_id,
                        session_id=sid,
                        execution_id=prepared.id,
                        attempt_id=prepared.attempt_id,
                        binding_sha256=prepared.binding_sha256,
                        interval=0.05,
                    )
                )
                await asyncio.wait_for(ready.wait(), 5)
                send = asyncio.create_task(
                    send_source(endpoint, rpc_key, sid, key, source_binding, [wire], deadline=deadline)
                )
                await asyncio.wait_for(staged.wait(), 5)
                with pytest.raises((RuntimeError, ConnectionError, asyncio.IncompleteReadError)):
                    await send_source(endpoint, rpc_key, sid, key, source_binding, [wire], deadline=deadline)
                assert await RpcClient(endpoint, "tool_gateway", rpc_key).call("source.heartbeat", scope) == {
                    "renewed": True
                }
                await asyncio.to_thread(
                    dbs["owner"].one,
                    "UPDATE agent.repository_bindings SET generation=generation+1,workspace_revision=workspace_revision+1,"
                    "approved_commit=%s WHERE tenant_id=%s AND id=%s RETURNING id",
                    ("b" * 40, claim.tenant_id, binding_id),
                )
                await asyncio.wait_for(revoked.wait(), 5)
                with pytest.raises(RuntimeError, match="rejected"):
                    await asyncio.wait_for(send, 5)
            finally:
                stop.set()
                if send is not None and not send.done():
                    send.cancel()
                if pulse is not None:
                    await asyncio.gather(pulse, return_exceptions=True)
                server.cancel()
                await asyncio.gather(server, return_exceptions=True)

        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(lambda: asyncio.run(exercise())).result(timeout=25)

    prepared_execution_fixture(dbs, verified_setup, on_prepared=issue)


@pytest.mark.skipif(sys.platform != "linux", reason="requires Unix socket and supervisor journal")
def test_source_cancel_before_registration_persists_replay_fence(dbs, verified_setup, tmp_path):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from aiagent_domain.rpcwire import RpcError
    from aiagent_gateways.execution_capture_proof import HmacCaptureVerifier
    from aiagent_gateways.execution_lifecycle import ExecutionLifecycle
    from aiagent_gateways.source_transfer import cancel_issued_source, register_source
    from aiagent_sandbox.source_rpc import SourceAuthority, SourceSessions
    from aiagent_sandbox.supervisor import Supervisor, serve

    _, _, manifest, proof, receipt, _, parent = verified_setup

    def issue(claim, prepared, _snapshot, _parent):
        ticket_key, rpc_key = b"t" * 32, b"r" * 32
        lifecycle = ExecutionLifecycle(dbs["tool_gateway"], claim, HmacCaptureVerifier(dbs["tool_gateway"], b"v" * 32))
        ticket = lifecycle.issue_source_session(prepared, manifest, receipt, ticket_key, deadline=time.monotonic() + 30)

        async def exercise():
            sup = Supervisor(tmp_path / "cancel-supervisor", b"g" * 32, cell_uid=20001, cell_gid=20001)
            sessions = SourceSessions(
                parent,
                ticket_key=ticket_key,
                registration_factory=lambda p: (SourceAuthority(p["expires_at"]), lambda _fd: None),
            )
            endpoint = f"unix:{tmp_path / 'cancel.sock'}"
            server = asyncio.create_task(serve(sup, {"tool_gateway": rpc_key}, endpoint, source_sessions=sessions))
            deadline = time.monotonic() + 15
            try:
                for _ in range(100):
                    if (tmp_path / "cancel.sock").exists():
                        break
                    await asyncio.sleep(0.01)
                assert await cancel_issued_source(
                    lifecycle, prepared, endpoint, rpc_key, ticket_key, ticket, deadline=deadline
                )
                with pytest.raises(RpcError):
                    await register_source(endpoint, rpc_key, ticket_key, ticket, manifest, proof, deadline=deadline)
            finally:
                server.cancel()
                await asyncio.gather(server, return_exceptions=True)

        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(lambda: asyncio.run(exercise())).result(timeout=20)

    claim, prepared, _, _ = prepared_execution_fixture(dbs, verified_setup, on_prepared=issue, expected_state="UNKNOWN")
    row = dbs["owner"].one(
        "SELECT state FROM agent.execution_instances WHERE tenant_id=%s AND id=%s", (claim.tenant_id, prepared.id)
    )
    assert row is not None and row["state"] == "UNKNOWN"


def test_gateway_attempt_prepares_distinct_execution_only_with_approved_scope(dbs, verified_setup):
    prepared_execution_fixture(dbs, verified_setup)


def test_concurrent_first_snapshots_do_not_overwrite_same_workspace_revision(dbs, verified_setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from aiagent_development import workspace_authority as module

    claim, binding, manifest, proof, receipt, wire, parent = verified_setup
    scope = Scope(
        claim.tenant_id,
        claim.owner_user_id,
        str(
            dbs["owner"].one(
                "SELECT workspace_id FROM agent.tasks WHERE tenant_id=%s AND id=%s", (claim.tenant_id, claim.task_id)
            )["workspace_id"]
        ),
    )
    ids = commands.submit_task(dbs["api"], scope, "second fixture", profile="sandbox")
    other_claim = control.claim_run(dbs["runtime"], scope.tenant_id, ids["runId"], "second-worker")
    other_binding = uuid7()
    dbs["owner"].one(
        "INSERT INTO agent.repository_bindings(tenant_id,id,owner_user_id,workspace_id,repository_ref,approved_commit,"
        "workspace_revision,approval_ref) VALUES (%s,%s,%s,%s,%s,%s,1,%s) RETURNING id",
        (
            claim.tenant_id,
            other_binding,
            claim.owner_user_id,
            scope.workspace_id,
            "fixture/other",
            manifest.identity.commit,
            "operator-fixture",
        ),
    )
    git = shutil.which("git")
    assert git
    other_identity = Identity(claim.tenant_id, claim.owner_user_id, other_binding, "1", manifest.identity.commit)
    other_reader = GitSnapshot(AuthorizedSnapshot(other_identity, parent.parent / "repo"), Path(git))
    other_manifest = prepare_projection(other_reader, other_identity, other_reader.tree)
    other_proof = build_git_tree_proof(other_reader)
    encoder = ProjectionEncoder(other_reader, other_manifest)
    other_wire = b"".join(encoder.chunks())
    assert encoder.receipt is not None
    barrier = Barrier(2)
    original = module.stage_git_projection

    @contextmanager
    def synchronized(*args, **kwargs):
        with original(*args, **kwargs) as staged:
            barrier.wait(timeout=10)
            yield staged

    monkeypatch.setattr(module, "stage_git_projection", synchronized)

    def attempt(data):
        try:
            return register_verified_snapshot(dbs["tool_gateway"], *data, parent, deadline=time.monotonic() + 25)
        except StaleReference:
            return None

    first = (claim, binding, manifest, proof, receipt, io.BytesIO(wire))
    second = (other_claim, other_binding, other_manifest, other_proof, encoder.receipt, io.BytesIO(other_wire))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (first, second)))
    assert sum(result is not None for result in results) == 1
    count = dbs["owner"].one(
        "SELECT count(*) AS n FROM agent.workspace_snapshot_attestations WHERE tenant_id=%s", (claim.tenant_id,)
    )
    assert count is not None and count["n"] == 1 and not list(parent.iterdir())


@pytest.mark.parametrize("change", ["cancel", "workspace"])
def test_authority_change_during_proof_prevents_snapshot(dbs, verified_setup, monkeypatch, change):
    from aiagent_development import workspace_authority as module

    claim, binding, manifest, proof, receipt, wire, parent = verified_setup
    original = module.stage_git_projection

    @contextmanager
    def change_after_verify(*args, **kwargs):
        with original(*args, **kwargs) as staged:
            if change == "cancel":
                dbs["owner"].one(
                    "UPDATE agent.runs SET cancel_requested=true WHERE tenant_id=%s AND id=%s RETURNING id",
                    (claim.tenant_id, claim.run_id),
                )
            else:
                workspace = dbs["owner"].one(
                    "SELECT workspace_id FROM agent.tasks WHERE tenant_id=%s AND id=%s",
                    (claim.tenant_id, claim.task_id),
                )["workspace_id"]
                dbs["owner"].one(
                    "UPDATE agent.workspaces SET disabled_at=now() WHERE tenant_id=%s AND id=%s RETURNING id",
                    (claim.tenant_id, workspace),
                )
            yield staged

    monkeypatch.setattr(module, "stage_git_projection", change_after_verify)
    with pytest.raises((ScopeDenied, control.Cancelled)):
        register_verified_snapshot(
            dbs["tool_gateway"],
            claim,
            binding,
            manifest,
            proof,
            receipt,
            io.BytesIO(wire),
            parent,
            deadline=time.monotonic() + 10,
        )
    row = dbs["owner"].one(
        "SELECT count(*) AS n FROM agent.workspace_snapshot_attestations WHERE tenant_id=%s", (claim.tenant_id,)
    )
    assert row is not None and row["n"] == 0 and not list(parent.iterdir())


@contextmanager
def finished_artifact_fixture(dbs, verified_setup, *, running=False):
    import hashlib

    from aiagent_development.code_intelligence.artifact_capture import capture_scip
    from aiagent_gateways.context_keys import KeyRing
    from aiagent_gateways.execution_artifacts import PostgresExecutionArtifactStore

    def start_before_settlement(claim, prepared, _snapshot, _parent):
        from aiagent_gateways.execution_capture_proof import HmacCaptureVerifier
        from aiagent_gateways.execution_lifecycle import ExecutionLifecycle

        verifier = HmacCaptureVerifier(dbs["tool_gateway"], b"receipt-fixture-key" * 2)
        ExecutionLifecycle(dbs["tool_gateway"], claim, verifier).start(
            prepared.id, prepared.binding_sha256, deadline=time.monotonic() + 10
        )

    claim, prepared, snapshot, parent = prepared_execution_fixture(
        dbs, verified_setup, on_prepared=start_before_settlement if running else None
    )
    if not running:
        with dbs["owner"].tx() as c:
            c.execute(
                "UPDATE agent.execution_instances SET state='RUNNING' WHERE tenant_id=%s AND id=%s",
                (claim.tenant_id, prepared.id),
            )
            c.execute(
                "UPDATE agent.execution_instances SET state='FINISHED',capture_sha256=%s,capture_bytes=%s,"
                "capture_proof_sha256=%s WHERE tenant_id=%s AND id=%s",
                (
                    hashlib.sha256(b"synthetic-artifact\0").hexdigest(),
                    len(b"synthetic-artifact\0"),
                    hashlib.sha256(b"fake-cell-receipt").hexdigest(),
                    claim.tenant_id,
                    prepared.id,
                ),
            )

    output = parent.parent / "fixture-output"
    output.mkdir()
    (output / "index.scip").write_bytes(b"synthetic-artifact\0")
    output_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    class FakeCaptureVerifier:
        def verify(self, checked_claim, execution_id, digest, size, proof):
            assert checked_claim == claim and execution_id == prepared.id and proof == b"fake-cell-receipt"
            assert size == len(b"synthetic-artifact\0") and len(digest) == 64

    class FakeScipVerifier:
        def verify(self, checked_claim, execution_id, content):
            assert checked_claim == claim and execution_id == prepared.id and content == b"synthetic-artifact\0"
            return "PARTIAL"

    store = PostgresExecutionArtifactStore(
        dbs["tool_gateway"],
        claim,
        KeyRing.single(b"k" * 32, "fixture"),
        capture=FakeCaptureVerifier(),
        index=FakeScipVerifier(),
    )
    try:
        with capture_scip(output_fd, parent, expected_uid=os.geteuid(), deadline=time.monotonic() + 15) as captured:
            yield claim, prepared, snapshot, store, captured
    finally:
        os.close(output_fd)


def test_atomic_encrypted_artifact_publication_with_fake_verifiers(dbs, verified_setup):
    from aiagent_gateways.execution_artifacts import ExecutionArtifactError

    with finished_artifact_fixture(dbs, verified_setup) as (claim, prepared, _snapshot, store, captured):
        before = dbs["owner"].one(
            "SELECT count(*) AS n FROM agent.execution_artifacts WHERE tenant_id=%s", (claim.tenant_id,)
        )
        assert before is not None and before["n"] == 0
        first = store.publish(prepared.id, captured.fd, b"fake-cell-receipt", deadline=time.monotonic() + 15)
        second = store.publish(prepared.id, captured.fd, b"fake-cell-receipt", deadline=time.monotonic() + 15)
        assert first == second and first.coverage == "PARTIAL"
        read_ref, content = store.fetch(prepared.id, deadline=time.monotonic() + 15)
        assert read_ref == first and content == b"synthetic-artifact\0"
        row = dbs["owner"].one(
            "SELECT ciphertext FROM agent.execution_artifacts WHERE tenant_id=%s AND id=%s", (claim.tenant_id, first.id)
        )
        assert row is not None and b"synthetic-artifact" not in bytes(row["ciphertext"])
        with pytest.raises(ExecutionArtifactError):
            store.fetch(uuid7(), deadline=time.monotonic() + 15)


def test_cancel_before_artifact_publication_is_denied(dbs, verified_setup):
    from aiagent_gateways.execution_artifacts import ExecutionArtifactError

    with finished_artifact_fixture(dbs, verified_setup) as (claim, prepared, snapshot, store, captured):
        scope = Scope(claim.tenant_id, claim.owner_user_id, snapshot.workspace_id)
        commands.cancel(dbs["api"], scope, claim.task_id)
        with pytest.raises((ExecutionArtifactError, control.Cancelled)):
            store.publish(prepared.id, captured.fd, b"fake-cell-receipt", deadline=time.monotonic() + 15)
        count = dbs["owner"].one(
            "SELECT count(*) AS n FROM agent.execution_artifacts WHERE tenant_id=%s", (claim.tenant_id,)
        )
        assert count is not None and count["n"] == 0


def test_atomic_publication_is_idempotent_under_concurrency(dbs, verified_setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    with finished_artifact_fixture(dbs, verified_setup) as (claim, prepared, _snapshot, store, captured):
        barrier = Barrier(2)
        original = store._capture

        class SynchronizedCapture:
            def verify(self, *args):
                original.verify(*args)
                barrier.wait(timeout=10)

        store._capture = SynchronizedCapture()

        def publish(_index):
            return store.publish(prepared.id, captured.fd, b"fake-cell-receipt", deadline=time.monotonic() + 20)

        with ThreadPoolExecutor(max_workers=2) as pool:
            refs = list(pool.map(publish, range(2)))
        assert refs[0] == refs[1]
        count = dbs["owner"].one(
            "SELECT count(*) AS n FROM agent.execution_artifacts WHERE tenant_id=%s", (claim.tenant_id,)
        )
        assert count is not None and count["n"] == 1


def test_cancel_commit_wins_race_against_publication(dbs, verified_setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from aiagent_gateways.execution_artifacts import ExecutionArtifactError

    with finished_artifact_fixture(dbs, verified_setup) as (claim, prepared, _snapshot, store, captured):
        reached_verifier = Event()
        original = store._capture

        class SignallingCapture:
            def verify(self, *args):
                original.verify(*args)
                reached_verifier.set()

        store._capture = SignallingCapture()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with dbs["owner"].tx() as c:
                c.execute(
                    "SELECT id FROM agent.tasks WHERE tenant_id=%s AND id=%s FOR UPDATE",
                    (claim.tenant_id, claim.task_id),
                )
                c.execute(
                    "SELECT id FROM agent.runs WHERE tenant_id=%s AND id=%s FOR UPDATE", (claim.tenant_id, claim.run_id)
                )
                future = pool.submit(
                    store.publish, prepared.id, captured.fd, b"fake-cell-receipt", deadline=time.monotonic() + 20
                )
                assert reached_verifier.wait(timeout=10)
                c.execute(
                    "UPDATE agent.runs SET cancel_requested=true,status_code='CANCELLING' WHERE tenant_id=%s AND id=%s",
                    (claim.tenant_id, claim.run_id),
                )
            with pytest.raises(ExecutionArtifactError):
                future.result(timeout=15)
        count = dbs["owner"].one(
            "SELECT count(*) AS n FROM agent.execution_artifacts WHERE tenant_id=%s", (claim.tenant_id,)
        )
        assert count is not None and count["n"] == 0


def test_snapshot_change_and_invalid_index_deny_publication(dbs, verified_setup):
    from aiagent_gateways.execution_artifacts import ExecutionArtifactError

    with finished_artifact_fixture(dbs, verified_setup) as (claim, prepared, snapshot, store, captured):

        class InvalidIndex:
            def verify(self, *_args):
                return "INVALID"

        valid_index = store._index
        store._index = InvalidIndex()
        with pytest.raises(ExecutionArtifactError, match="INVALID_SCIP"):
            store.publish(prepared.id, captured.fd, b"fake-cell-receipt", deadline=time.monotonic() + 15)
        store._index = valid_index
        dbs["owner"].one(
            "UPDATE agent.repository_bindings SET generation=generation+1,workspace_revision=workspace_revision+1,"
            "approved_commit=%s WHERE tenant_id=%s AND id=%s RETURNING id",
            ("b" * 40, claim.tenant_id, snapshot.binding_id),
        )
        with pytest.raises(ExecutionArtifactError, match="PUBLICATION_DENIED"):
            store.publish(prepared.id, captured.fd, b"fake-cell-receipt", deadline=time.monotonic() + 15)
        count = dbs["owner"].one(
            "SELECT count(*) AS n FROM agent.execution_artifacts WHERE tenant_id=%s", (claim.tenant_id,)
        )
        assert count is not None and count["n"] == 0


def test_published_artifact_denies_cross_tenant_stale_lease_and_ciphertext_tampering(dbs, verified_setup):
    from aiagent_gateways.context_keys import KeyRing
    from aiagent_gateways.execution_artifacts import ExecutionArtifactError, PostgresExecutionArtifactStore

    with finished_artifact_fixture(dbs, verified_setup) as (claim, prepared, _snapshot, store, captured):
        ref = store.publish(prepared.id, captured.fd, b"fake-cell-receipt", deadline=time.monotonic() + 15)
        for altered in (replace(claim, tenant_id=uuid7()), replace(claim, lease_epoch=claim.lease_epoch + 1)):
            other = PostgresExecutionArtifactStore(
                dbs["tool_gateway"],
                altered,
                KeyRing.single(b"k" * 32, "fixture"),
                capture=store._capture,
                index=store._index,
            )
            with pytest.raises(control.ControlError):
                other.fetch(prepared.id, deadline=time.monotonic() + 15)
        row = dbs["owner"].one(
            "SELECT ciphertext FROM agent.execution_artifacts WHERE tenant_id=%s AND id=%s", (claim.tenant_id, ref.id)
        )
        ciphertext = bytes(row["ciphertext"])
        changed = bytes([ciphertext[0] ^ 1]) + ciphertext[1:]
        dbs["owner"].one(
            "UPDATE agent.execution_artifacts SET ciphertext=%s WHERE tenant_id=%s AND id=%s RETURNING id",
            (changed, claim.tenant_id, ref.id),
        )
        with pytest.raises(ExecutionArtifactError, match="INTEGRITY"):
            store.fetch(prepared.id, deadline=time.monotonic() + 15)


def test_legacy_git_snapshot_cannot_claim_validated_artifact(dbs, verified_setup):
    from aiagent_gateways.execution_artifacts import ExecutionArtifactError

    with finished_artifact_fixture(dbs, verified_setup) as (claim, prepared, _snapshot, store, captured):

        class MisleadingIndex:
            def verify(self, *_args):
                return "VALIDATED"

        store._index = MisleadingIndex()
        with pytest.raises(ExecutionArtifactError, match="PUBLICATION_DENIED"):
            store.publish(prepared.id, captured.fd, b"fake-cell-receipt", deadline=time.monotonic() + 15)
        row = dbs["owner"].one(
            "SELECT count(*) AS n FROM agent.execution_artifacts WHERE tenant_id=%s", (claim.tenant_id,)
        )
        assert row is not None and row["n"] == 0


def test_signed_capture_proof_is_bound_to_real_execution_row(dbs, verified_setup):
    import hashlib
    import hmac
    import json

    from aiagent_domain import canonical_json
    from aiagent_gateways.execution_artifacts import ExecutionArtifactError
    from aiagent_gateways.execution_capture_proof import HmacCaptureVerifier

    with finished_artifact_fixture(dbs, verified_setup) as (claim, prepared, _snapshot, _store, captured):
        key = b"fixture-key-" + b"x" * 32
        payload = {
            "version": 1,
            "tenant": claim.tenant_id,
            "owner": claim.owner_user_id,
            "task": claim.task_id,
            "run": claim.run_id,
            "goal": claim.goal_revision,
            "lease": claim.lease_epoch,
            "execution": prepared.id,
            "attempt": prepared.attempt_id,
            "binding_sha256": prepared.binding_sha256,
            "artifact_sha256": captured.sha256,
            "artifact_bytes": captured.byte_length,
            "issued_at": int(time.time()),
        }

        def sign(value):
            mac = hmac.new(key, canonical_json(value).encode("utf-8"), hashlib.sha256).hexdigest()
            return json.dumps({"payload": value, "mac": mac}).encode()

        verifier = HmacCaptureVerifier(dbs["tool_gateway"], key)
        verifier.verify(claim, prepared.id, captured.sha256, captured.byte_length, sign(payload))
        for field, value in (
            ("attempt", uuid7()),
            ("binding_sha256", "a" * 64),
            ("tenant", uuid7()),
            ("execution", uuid7()),
            ("artifact_sha256", "b" * 64),
            ("issued_at", 0),
            ("version", True),
        ):
            with pytest.raises(ExecutionArtifactError, match="CAPTURE_PROOF_INVALID"):
                verifier.verify(
                    claim, prepared.id, captured.sha256, captured.byte_length, sign({**payload, field: value})
                )


def test_real_run_cancel_sets_authority_watch_event(dbs, verified_setup):
    import asyncio

    from aiagent_gateways.execution_watch import watch_run_authority

    claim, _binding, _manifest, _proof, _receipt, _wire, _parent = verified_setup
    workspace = dbs["owner"].one(
        "SELECT workspace_id FROM agent.tasks WHERE tenant_id=%s AND id=%s", (claim.tenant_id, claim.task_id)
    )["workspace_id"]

    async def run_watch():
        revoked, stop, ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
        watcher = asyncio.create_task(
            watch_run_authority(dbs["tool_gateway"], claim, revoked, stop, ready, interval=0.05)
        )
        await asyncio.wait_for(ready.wait(), timeout=5)
        assert not revoked.is_set()
        await asyncio.to_thread(
            commands.cancel, dbs["api"], Scope(claim.tenant_id, claim.owner_user_id, str(workspace)), claim.task_id
        )
        await asyncio.wait_for(revoked.wait(), timeout=5)
        await asyncio.wait_for(watcher, timeout=5)

    asyncio.run(run_watch())


def test_real_run_cancellation_reaps_local_test_process(dbs, verified_setup):
    """DB + local synthetic child, not a remote Cell E2E."""
    import asyncio
    import sys

    from aiagent_gateways.execution_watch import watch_run_authority
    from aiagent_sandbox.supervisor import _run_with_revocation

    claim, _binding, _manifest, _proof, _receipt, _wire, _parent = verified_setup
    workspace = dbs["owner"].one(
        "SELECT workspace_id FROM agent.tasks WHERE tenant_id=%s AND id=%s", (claim.tenant_id, claim.task_id)
    )["workspace_id"]

    async def exercise():
        revoked, stop, ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
        watcher = asyncio.create_task(
            watch_run_authority(dbs["tool_gateway"], claim, revoked, stop, ready, interval=0.05)
        )
        await asyncio.wait_for(ready.wait(), 5)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; print('partial',flush=True); time.sleep(30)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        executing = asyncio.create_task(_run_with_revocation(proc, 20, revoked))
        try:
            await asyncio.to_thread(
                commands.cancel, dbs["api"], Scope(claim.tenant_id, claim.owner_user_id, str(workspace)), claim.task_id
            )
            out, err, _timeout, _truncated, was_revoked = await asyncio.wait_for(executing, 5)
            assert was_revoked and out == err == b"" and proc.returncode is not None
        finally:
            stop.set()
            await asyncio.gather(watcher, return_exceptions=True)
            if not executing.done():
                executing.cancel()
                await asyncio.gather(executing, return_exceptions=True)

    asyncio.run(exercise())


def test_execution_start_race_allows_one_dispatch_reservation(dbs, verified_setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from aiagent_gateways.execution_artifacts import ExecutionArtifactError
    from aiagent_gateways.execution_capture_proof import HmacCaptureVerifier
    from aiagent_gateways.execution_lifecycle import ExecutionLifecycle

    def race_before_settlement(claim, prepared, _snapshot, _parent):
        lifecycle = ExecutionLifecycle(dbs["tool_gateway"], claim, HmacCaptureVerifier(dbs["tool_gateway"], b"k" * 32))
        barrier = Barrier(2)

        def start(_):
            barrier.wait(timeout=10)
            try:
                lifecycle.start(prepared.id, prepared.binding_sha256, deadline=time.monotonic() + 20)
                return "STARTED"
            except ExecutionArtifactError:
                return "DENIED"

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(start, range(2))) == ["DENIED", "STARTED"]

    prepared_execution_fixture(dbs, verified_setup, on_prepared=race_before_settlement)


def test_execution_start_rejects_already_settled_attempt(dbs, verified_setup):
    from aiagent_gateways.execution_artifacts import ExecutionArtifactError
    from aiagent_gateways.execution_capture_proof import HmacCaptureVerifier
    from aiagent_gateways.execution_lifecycle import ExecutionLifecycle

    claim, prepared, _snapshot, _parent = prepared_execution_fixture(dbs, verified_setup)
    lifecycle = ExecutionLifecycle(dbs["tool_gateway"], claim, HmacCaptureVerifier(dbs["tool_gateway"], b"k" * 32))
    with pytest.raises(ExecutionArtifactError, match="EXECUTION_START_DENIED"):
        lifecycle.start(prepared.id, prepared.binding_sha256, deadline=time.monotonic() + 10)


@pytest.mark.parametrize("cancel_before_finish", [False, True])
def test_real_capture_signer_interoperates_with_canonical_db_verifier(dbs, verified_setup, cancel_before_finish):
    """Synthetic FINISHED transition; no real Cell transport is claimed."""
    from aiagent_development.workspace_authority import _binding_payload
    from aiagent_domain import sha256_hex
    from aiagent_gateways.execution_artifacts import ExecutionArtifactError
    from aiagent_gateways.execution_capture_proof import HmacCaptureVerifier
    from aiagent_gateways.execution_lifecycle import ExecutionLifecycle
    from aiagent_sandbox.capture_signing import sign_capture
    from aiagent_sandbox.grant import Grant

    with finished_artifact_fixture(dbs, verified_setup, running=True) as (claim, prepared, snapshot, store, captured):
        execution = dbs["owner"].one(
            "SELECT * FROM agent.execution_instances WHERE tenant_id=%s AND id=%s", (claim.tenant_id, prepared.id)
        )
        snap = dbs["owner"].one(
            "SELECT * FROM agent.workspace_snapshot_attestations WHERE tenant_id=%s AND id=%s",
            (claim.tenant_id, snapshot.id),
        )
        recipe = dbs["owner"].one(
            "SELECT * FROM agent.execution_recipe_approvals WHERE tenant_id=%s AND id=%s",
            (claim.tenant_id, prepared.recipe_id),
        )
        binding = _binding_payload(execution, snap, recipe)
        assert sha256_hex(binding) == prepared.binding_sha256
        grant = Grant(
            "fixture",
            claim.tenant_id,
            claim.task_id,
            claim.run_id,
            prepared.intent_id,
            claim.lease_epoch,
            ("fixture",),
            ".",
            1000,
            1024,
            int(time.time()) + 30,
            attempt_id=prepared.attempt_id,
            attempt_binding_version=1,
            execution_binding_version=1,
            execution_id=prepared.id,
            execution_binding_sha256=prepared.binding_sha256,
        )
        key = b"receipt-fixture-key" * 2
        proof = sign_capture(
            key,
            grant,
            binding,
            captured.fd,
            expected_sha256=captured.sha256,
            size=captured.byte_length,
            deadline=time.monotonic() + 10,
        )
        verifier = HmacCaptureVerifier(dbs["tool_gateway"], key)
        lifecycle = ExecutionLifecycle(dbs["tool_gateway"], claim, verifier)
        with pytest.raises(ExecutionArtifactError, match="EXECUTION_START_DENIED"):
            lifecycle.start(prepared.id, prepared.binding_sha256, deadline=time.monotonic() + 10)
        with pytest.raises(ExecutionArtifactError, match="CAPTURE_PROOF_INVALID"):
            lifecycle.finish(prepared.id, captured.fd, b"{}", deadline=time.monotonic() + 10)
        with pytest.raises(control.FencedOut):
            ExecutionLifecycle(dbs["tool_gateway"], replace(claim, lease_epoch=claim.lease_epoch + 1), verifier).finish(
                prepared.id, captured.fd, proof, deadline=time.monotonic() + 10
            )
        if cancel_before_finish:
            original = verifier.verify_running

            def cancel_after_verification(*args):
                result = original(*args)
                dbs["owner"].one(
                    "UPDATE agent.runs SET cancel_requested=true,status_code='CANCELLING' "
                    "WHERE tenant_id=%s AND id=%s RETURNING id",
                    (claim.tenant_id, claim.run_id),
                )
                return result

            verifier.verify_running = cancel_after_verification
            with pytest.raises((ExecutionArtifactError, control.Cancelled)):
                lifecycle.finish(prepared.id, captured.fd, proof, deadline=time.monotonic() + 10)
            row = dbs["owner"].one(
                "SELECT state FROM agent.execution_instances WHERE tenant_id=%s AND id=%s",
                (claim.tenant_id, prepared.id),
            )
            assert row["state"] == "RUNNING"
            return
        lifecycle.finish(prepared.id, captured.fd, proof, deadline=time.monotonic() + 10)
        lifecycle.finish(prepared.id, captured.fd, proof, deadline=time.monotonic() + 10)
        import hashlib
        import hmac
        import json

        from aiagent_domain import canonical_json

        alternate = json.loads(proof)
        alternate["payload"]["issued_at"] -= 1
        alternate["mac"] = hmac.new(key, canonical_json(alternate["payload"]).encode(), hashlib.sha256).hexdigest()
        alternate_proof = json.dumps(alternate, sort_keys=True, separators=(",", ":")).encode()
        with pytest.raises(ExecutionArtifactError, match="EXECUTION_FINISH_DENIED"):
            lifecycle.finish(prepared.id, captured.fd, alternate_proof, deadline=time.monotonic() + 10)
        verifier.verify(claim, prepared.id, captured.sha256, captured.byte_length, proof)
        # Actual signer -> actual Gateway verifier -> encrypted PostgreSQL publication.
        # The Cell and SCIP validation are still fixture-only here.
        store._capture = verifier
        with pytest.raises(ExecutionArtifactError, match="ARTIFACT_PUBLICATION_DENIED"):
            store.publish(prepared.id, captured.fd, alternate_proof, deadline=time.monotonic() + 10)
        published = store.publish(prepared.id, captured.fd, proof, deadline=time.monotonic() + 10)
        repeated = store.publish(prepared.id, captured.fd, proof, deadline=time.monotonic() + 10)
        fetched, content = store.fetch(prepared.id, deadline=time.monotonic() + 10)
        assert published == repeated == fetched
        assert published.sha256 == captured.sha256 and content == b"synthetic-artifact\0"
