"""Real DB + actual disposable Git objects, no running Cell or authenticated transfer."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from aiagent_core import commands, control
from aiagent_core.commands import Scope
from aiagent_development.code_intelligence.git_proof import build_git_tree_proof
from aiagent_development.code_intelligence.git_reader import GitSnapshot
from aiagent_development.code_intelligence.models import AuthorizedSnapshot, Identity
from aiagent_development.code_intelligence.projection import prepare_projection
from aiagent_development.code_intelligence.projection_stream import ProjectionEncoder
from aiagent_development.contracts import InvalidEvidence, ScopeDenied, StaleReference
from aiagent_development.workspace_authority import register_verified_snapshot
from aiagent_domain import uuid7

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


def prepared_execution_fixture(dbs, verified_setup):
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
    args = {"argv": ["approved-test"], "cwdRelative": "."}
    request_sha = sha256_hex({"tool": "sandbox_exec", "args": args})
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
                "d" * 64,
                "e" * 64
                if rid == bad_digest
                else approved_recipe_digest(
                    kind="test",
                    test_id=test_id,
                    plan_sha256=sha256_hex(asdict(plan)),
                    request_sha256=req_sha,
                    template_sha256="d" * 64,
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
    assert row is not None and row["state"] == "PREPARED" and row["artifact_sha256"] is None
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege), dbs["tool_gateway"].tx(tenant=claim.tenant_id) as c:
        c.execute(
            "UPDATE agent.execution_instances SET state='PUBLISHED',artifact_sha256=%s,artifact_bytes=1"
            " WHERE tenant_id=%s AND id=%s",
            ("a" * 64, claim.tenant_id, prepared.id),
        )
    with pytest.raises(psycopg.errors.CheckViolation), dbs["owner"].tx() as c:
        c.execute(
            "UPDATE agent.execution_instances SET state='PUBLISHED',artifact_sha256=%s,artifact_bytes=1"
            " WHERE tenant_id=%s AND id=%s",
            ("a" * 64, claim.tenant_id, prepared.id),
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege), dbs["runtime"].conn() as c:
        c.execute("SELECT * FROM agent.execution_instances")
    return claim, prepared, snapshot, parent


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
def finished_artifact_fixture(dbs, verified_setup):
    from aiagent_development.code_intelligence.artifact_capture import capture_scip
    from aiagent_gateways.context_keys import KeyRing
    from aiagent_gateways.execution_artifacts import PostgresExecutionArtifactStore

    claim, prepared, snapshot, parent = prepared_execution_fixture(dbs, verified_setup)
    with dbs["owner"].tx() as c:
        c.execute(
            "UPDATE agent.execution_instances SET state='RUNNING' WHERE tenant_id=%s AND id=%s",
            (claim.tenant_id, prepared.id),
        )
        c.execute(
            "UPDATE agent.execution_instances SET state='FINISHED' WHERE tenant_id=%s AND id=%s",
            (claim.tenant_id, prepared.id),
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
