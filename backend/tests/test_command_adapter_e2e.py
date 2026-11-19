"""Opt-in real DB, Gateway, Supervisor and signed-command Adapter integration."""

from __future__ import annotations

import io
import os
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pytest
from aiagent_core import commands, control
from aiagent_core.commands import Scope
from aiagent_development.code_intelligence.git_proof import build_git_tree_proof
from aiagent_development.code_intelligence.git_reader import GitSnapshot
from aiagent_development.code_intelligence.indexer_recipe import approved_recipe_digest
from aiagent_development.code_intelligence.models import AuthorizedSnapshot, Identity
from aiagent_development.code_intelligence.projection import prepare_projection
from aiagent_development.code_intelligence.projection_stream import ProjectionEncoder
from aiagent_development.contracts import Cycle, ExecutionRequest, Phase, ProjectScope, RunFence, TaskPlan
from aiagent_development.factory import ToolGatewaySandboxAdapter
from aiagent_development.pg_store import PostgresCycleStore, PostgresRunAuthority, PostgresWorkspacePort
from aiagent_development.workflow import DevelopmentWorkflow
from aiagent_development.workspace_authority import register_verified_snapshot
from aiagent_domain import sha256_hex, uuid7
from aiagent_gateways.command_dispatch import AuthorizedCommandExecutor
from aiagent_gateways.command_receipt_reader import CommandReceiptReader
from aiagent_gateways.execution_capture_proof import HmacCaptureVerifier
from aiagent_gateways.execution_lifecycle import ExecutionLifecycle
from aiagent_gateways.tool_gateway import ToolGateway
from aiagent_sandbox.command_authority import command_template_digest
from psycopg.types.json import Jsonb
from stack import wait_port
from test_cell_e2e import _supervisor_answers

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        sys.platform != "linux" or os.environ.get("AIAGENT_COMMAND_E2E") != "1", reason="opt-in command Cell E2E"
    ),
]


@pytest.mark.parametrize(
    "kind,test_id,exit_code", [("build", None, 0), ("static-check", None, 0), ("test", "unit", 0), ("test", "unit", 7)]
)
def test_adapter_reads_real_signed_command_receipt(dbs, tenant, tmp_path: Path, kind, test_id, exit_code):
    git = shutil.which("git")
    assert git
    backend = Path(__file__).resolve().parents[1]
    image = subprocess.check_output(
        ["docker", "image", "inspect", "codex-dev13-scip-cell-fixture:latest", "--format", "{{.Id}}"], text=True
    ).strip()
    name = "aiagent-command-e2e-" + secrets.token_hex(5)
    started = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
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
    if started.returncode:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        pytest.fail(started.stderr)
    try:
        port = int(subprocess.check_output(["docker", "port", name, "7000/tcp"], text=True).strip().rsplit(":", 1)[1])
        wait_port(port, 60)
        assert _supervisor_answers(port, 30)
        binary = "/usr/local/bin/python3.13"
        toolchain = subprocess.check_output(
            [
                "docker",
                "exec",
                name,
                "python",
                "-c",
                "from aiagent_development.code_intelligence.indexer_recipe import measure_command_toolchain; "
                "print(measure_command_toolchain('/usr/local/bin/python3.13'))",
            ],
            text=True,
        ).strip()
        repo = tmp_path / "repo"
        repo.mkdir()

        def git_run(*args):
            return subprocess.check_output([git, "-C", str(repo), *args], stderr=subprocess.DEVNULL)

        git_run("init")
        git_run("config", "user.name", "Fixture")
        git_run("config", "user.email", "fixture@example.invalid")
        (repo / "command.py").write_text(f"raise SystemExit({exit_code})\n")
        git_run("add", ".")
        git_run("commit", "-m", "fixture")
        commit = git_run("rev-parse", "HEAD").decode().strip()
        scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
        ids = commands.submit_task(dbs["api"], scope, "fixture", profile="sandbox")
        claim = control.claim_run(dbs["runtime"], scope.tenant_id, ids["runId"], "command-worker", lease_s=120)
        binding_id = uuid7()
        dbs["owner"].one(
            "INSERT INTO agent.repository_bindings(tenant_id,id,owner_user_id,workspace_id,repository_ref,approved_commit,"
            "workspace_revision,approval_ref) VALUES (%s,%s,%s,%s,%s,%s,1,%s) RETURNING id",
            (claim.tenant_id, binding_id, claim.owner_user_id, scope.workspace_id, "fixture/repo", commit, "fixture"),
        )
        identity = Identity(claim.tenant_id, claim.owner_user_id, binding_id, "1", commit)
        source = GitSnapshot(AuthorizedSnapshot(identity, repo), Path(git))
        manifest = prepare_projection(source, identity, source.tree)
        proof = build_git_tree_proof(source)
        encoder = ProjectionEncoder(source, manifest)
        chunks = list(encoder.chunks())
        assert encoder.receipt is not None
        private = tmp_path / "private"
        private.mkdir(mode=0o700)
        snapshot = register_verified_snapshot(
            dbs["tool_gateway"],
            claim,
            binding_id,
            manifest,
            proof,
            encoder.receipt,
            io.BytesIO(b"".join(chunks)),
            private,
            deadline=time.monotonic() + 20,
        )
        project = ProjectScope(
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
            claim.task_id,
            claim.run_id,
            claim.goal_revision,
            claim.lease_epoch,
            claim.restore_incarnation,
            claim.worker_ref,
        )
        plan = TaskPlan("plan", "change", ("unit",), claim.goal_revision)
        cycle = Cycle("command-cycle", project, fence, Phase.PLANNED, "change", plan=plan)
        cycle_store = PostgresCycleStore(dbs["tool_gateway"])
        cycle_store.create(cycle)
        argv = [binary, "-B", "command.py"]
        args = {"argv": argv, "cwdRelative": "."}
        descriptor = {"kind": kind, "test_id": test_id, **args}
        request_sha, plan_sha = sha256_hex({"tool": "sandbox_exec", "args": args}), sha256_hex(asdict(plan))
        template, recipe_id = command_template_digest(descriptor), uuid7()
        recipe_sha = approved_recipe_digest(
            kind=kind,
            test_id=test_id,
            plan_sha256=plan_sha,
            request_sha256=request_sha,
            template_sha256=template,
            toolchain_sha256=toolchain,
            image_digest=image,
        )
        dbs["owner"].one(
            "INSERT INTO agent.execution_recipe_approvals(tenant_id,id,cycle_id,goal_revision,plan_sha256,plan_json,kind,"
            "test_id,request_sha256,template_sha256,recipe_sha256,toolchain_sha256,image_digest,approval_ref)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                claim.tenant_id,
                recipe_id,
                cycle.cycle_id,
                claim.goal_revision,
                plan_sha,
                Jsonb(asdict(plan)),
                kind,
                test_id,
                request_sha,
                template,
                recipe_sha,
                toolchain,
                image,
                "fixture",
            ),
        )
        lifecycle = ExecutionLifecycle(dbs["tool_gateway"], claim, HmacCaptureVerifier(dbs["tool_gateway"], b"c" * 32))
        executor = AuthorizedCommandExecutor(
            dbs["tool_gateway"],
            claim,
            snapshot.id,
            recipe_id,
            lifecycle,
            manifest,
            encoder.receipt,
            proof,
            chunks,
            f"tcp:127.0.0.1:{port}",
            b"r" * 32,
            b"t" * 32,
            b"g" * 32,
            b"c" * 32,
            private,
        )
        gateway = ToolGateway(dbs["tool_gateway"], {"sandbox_cell": executor})
        adapter = ToolGatewaySandboxAdapter(
            dbs["model_gateway"], gateway, claim, receipt_reader=CommandReceiptReader(lifecycle, b"c" * 32)
        )
        request = ExecutionRequest(
            project,
            fence,
            "plan",
            kind,
            tuple(argv),
            toolchain,
            "command-intent",
            test_id=test_id,
            target_commit_sha=commit,
        )
        if kind in ("build", "static-check"):

            class FixturePreflight:
                """Test-only DB preflight; the real Tool Gateway still performs final admission."""

                def require_operation(self, checked_scope, checked_run, operation, target, intent_ref):
                    assert checked_scope == project and checked_run == fence and intent_ref == request.intent_ref
                    assert operation == f"sandbox.{kind}" and target == commit
                    with dbs["tool_gateway"].tx(tenant=claim.tenant_id) as c:
                        control.check_current(c, claim)
                        approved = c.execute(
                            "SELECT 1 FROM agent.execution_recipe_approvals r "
                            "JOIN agent.workspace_snapshot_heads h ON h.tenant_id=r.tenant_id "
                            "WHERE r.tenant_id=%s AND r.id=%s AND r.active AND h.workspace_id=%s AND h.snapshot_id=%s",
                            (claim.tenant_id, recipe_id, scope.workspace_id, snapshot.id),
                        ).fetchone()
                        assert approved is not None

            workflow = DevelopmentWorkflow(
                runs=PostgresRunAuthority(dbs["api"]),
                reviewers=object(),
                workspaces=PostgresWorkspacePort(dbs["api"]),
                gateway=FixturePreflight(),
                sandbox=adapter,
                git=object(),
                store=cycle_store,
            )
            updated = workflow.execute(project, cycle.cycle_id, request)
            reloaded = cycle_store.load(project, cycle.cycle_id)
            assert updated == reloaded and len(reloaded.execution_receipts) == 1
            evidence = reloaded.execution_receipts[0]
        else:
            evidence = adapter.execute(request)
        assert evidence.executed and evidence.exit_code == exit_code and evidence.test_id == test_id
        assert evidence.source_commit_sha == commit and evidence.timed_out is False and evidence.truncated is False
        row = dbs["owner"].one(
            "SELECT state FROM agent.execution_instances WHERE tenant_id=%s AND run_id=%s",
            (claim.tenant_id, claim.run_id),
        )
        assert row is not None and row["state"] == "FINISHED"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
