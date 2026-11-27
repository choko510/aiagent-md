"""Phase 7 end-to-end & security boundary integration tests (DEV-01..DEV-14 on PostgreSQL 16 + Sentinel + ToolGateway + Sandbox Cell + Chromium).

Verifies ALL Round 1 (P0-01..P2-13) and Round 2 (R2-01..R2-13) security boundaries using production RPC wiring:
1. Repository Binding validation, Demo Approval artifact & live goal_revision binding, Review Artifact full-findings
   attestation & reconstruction, and DB role least-privilege isolation (R2-02, R2-07, R2-11).
2. Real PR #110 Sandbox Cell Supervisor (Landlock + CLONE_NEWUSER|CLONE_NEWPID|CLONE_NEWNET + Unix bridge) running
   Chromium Browser Debugger, HMAC Preview Launch Receipt + heartbeat + generation superseding, and Cycle binding
   (R2-01, R2-06, P2-11).
3. Zero-DI Production-Wired E2E: real Sentinel RPC + EgressProxy, real ToolGateway RPC + DevGitHubPrExecutor, and
   `create_app(dbs["api"])` with ZERO injected service dependencies — walking HEAD proposal -> ToolGateway.submit
   (unified TOOLS['dev_github_pr']) -> POST /v1/approvals -> concurrent PR dispatch -> EFFECT_UNKNOWN -> strict
   ToolGateway Reconcile -> CONFIRMED Sentinel RPC PR Guardian -> atomic repair ceiling -> policy requiredChecks
   (R2-02, R2-03, R2-04, R2-05, R2-08, R2-09, R2-10, R2-13).
4. Real Playwright Chromium UI E2E against built React Web UI `Development` tab served same-origin.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from aiagent_api.app import create_app
from aiagent_core import admin, commands, control
from aiagent_core.commands import Scope
from aiagent_core.db import Database
from aiagent_core.egress_http import ForwardProxyError, send_via_forward_proxy
from aiagent_core.rpc import RpcClient
from aiagent_development.contracts import (
    CommitEvidence,
    Cycle,
    ExecutionEvidence,
    InvalidEvidence,
    Phase,
    ProjectScope,
    ReviewFinding,
    ReviewVerdict,
    RunFence,
    ScopeDenied,
    StateConflict,
    TaskPlan,
)
from aiagent_development.github_egress import canonical_pr_request, pr_request_sha256
from aiagent_development.pg_store import PostgresCycleStore, PreviewBroker, sign_preview_launch_receipt
from aiagent_development.store import PostgresDevStore, PostgresReviewAttestationPort
from aiagent_domain import canonical_json, sha256_hex, uuid7
from aiagent_gateways.dev_github_executor import DevGitHubPrExecutor, canonical_github_pr_payload
from aiagent_gateways.sentinel_port import RpcSentinel
from aiagent_gateways.servers import build_tool_server
from aiagent_gateways.tool_gateway import ToolGateway
from aiagent_sentinel.authority import EgressConfig, Sentinel
from aiagent_sentinel.egress import EgressProxy
from aiagent_sentinel.main import build_server as build_sentinel_server
from fastapi.testclient import TestClient
from psycopg.errors import InsufficientPrivilege

pytestmark = pytest.mark.pg


@pytest.fixture(autouse=True)
def _drain_phase7_outbox_rows(dbs: dict[str, Database]) -> Any:
    """Ensure Phase 7 tests leave no undispatched outbox rows or pending queue jobs for downstream tests."""
    yield
    with dbs["owner"].tx() as c:
        c.execute("UPDATE agent.outbox_messages SET dispatched_at = now() WHERE dispatched_at IS NULL")
        c.execute("UPDATE queue.jobs SET status = 'CANCELLED' WHERE status IN ('READY', 'CLAIMED')")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _start_async_service(boot_coro_fn: Any) -> tuple[dict[str, Any], asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder: dict[str, Any] = {}
    err_box: list[BaseException] = []

    def runner() -> None:
        asyncio.set_event_loop(loop)
        try:
            res = loop.run_until_complete(boot_coro_fn(holder))
            if isinstance(res, dict):
                holder.update(res)
        except BaseException as exc:  # noqa: BLE001
            err_box.append(exc)
            ready.set()
            return
        ready.set()
        loop.run_forever()
        cleanup = holder.get("cleanup")
        if callable(cleanup):
            loop.run_until_complete(cleanup())
        loop.close()

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    assert ready.wait(timeout=15), "background async service failed to start"
    if err_box:
        raise err_box[0]
    return holder, loop, th


def _stop_async_service(loop: asyncio.AbstractEventLoop, th: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    th.join(timeout=8)


def _start_uvicorn_thread(app: Any) -> tuple[int, uvicorn.Server, threading.Thread, socket.socket]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = int(sock.getsockname()[1])
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    th = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    th.start()
    deadline = time.time() + 10.0
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn test server failed to start"
    return port, server, th, sock


def _seed_repository_binding(
    dbs: dict[str, Database],
    tenant: dict[str, str],
    *,
    repository: str,
    approved_commit: str = "2" * 40,
    approved_branch: str | None = "feat/p7-demo",
) -> str:
    binding_id = uuid7()
    with dbs["owner"].tx() as c:
        c.execute(
            "INSERT INTO agent.repository_bindings(tenant_id, id, owner_user_id, workspace_id, repository_ref,"
            " approved_commit, workspace_revision, approval_ref, approved_branch, active)"
            " VALUES (%s,%s,%s,%s,%s,%s,1,'op-apr-1',%s,true)",
            (
                tenant["tenantId"],
                binding_id,
                tenant["userId"],
                tenant["workspaceId"],
                repository,
                approved_commit,
                approved_branch,
            ),
        )
    return binding_id


def _seed_code_ready_cycle(
    dbs: dict[str, Database],
    tenant: dict[str, str],
    *,
    project_id: str,
    repository: str = "org/phase7-app",
    branch: str = "feat/p7-demo",
    cycle_id: str | None = None,
    commit_sha: str | None = None,
    profile: str = "development",
) -> tuple[str, str, dict[str, str], control.Claim, Cycle]:
    """Seed a real CODE_READY cycle with 3 independent reviewer verdicts for API/Preview/PR tests."""
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    created = commands.submit_task(dbs["api"], scope, "phase7 build task", profile=profile)
    claim = control.claim_run(dbs["runtime"], scope.tenant_id, created["runId"], "dev-worker-p7")

    rev_tasks = [
        commands.submit_task(dbs["api"], scope, f"review-{role}", profile="sandbox")["taskId"]
        for role in ("functional", "security", "ui-e2e")
    ]
    base_sha = "1" * 40
    c_sha = commit_sha or ("2" * 40)
    diff_sha = sha256_hex({"diff": "phase7", "sha": c_sha})
    tc_sha = sha256_hex({"tc": "py313"})
    cid = cycle_id or f"cyc-{uuid7()[:12]}"

    ps = ProjectScope(
        tenant_id=scope.tenant_id,
        owner_user_id=scope.user_id,
        project_id=project_id,
        workspace_id=scope.workspace_id,
        repository=repository,
        branch=branch,
        base_sha=base_sha,
        workspace_revision="1",
    )
    rf = RunFence(
        task_id=claim.task_id,
        run_id=claim.run_id,
        goal_revision=claim.goal_revision,
        lease_epoch=claim.lease_epoch,
        restore_incarnation=claim.restore_incarnation,
        worker_ref=claim.worker_ref,
    )
    cycle = Cycle(
        cycle_id=cid,
        scope=ps,
        run=rf,
        phase=Phase.CODE_READY,
        change_request_ref="cr-p7-1",
        version=5,
        plan=TaskPlan("plan-1", "cr-p7-1", ("test_main",), claim.goal_revision),
        commit=CommitEvidence(base_sha, c_sha, diff_sha, "art-diff-1", "git:receipt-1"),
        tests=(
            ExecutionEvidence(
                intent_ref="test-intent-1",
                run=rf,
                kind="test",
                exit_code=0,
                executed=True,
                artifact_sha256=sha256_hex({"out": "ok"}),
                toolchain_digest=tc_sha,
                stdout="1 passed",
                stderr="",
                source_commit_sha=c_sha,
                test_id="test_main",
                receipt_ref="rcpt-t1",
                timed_out=False,
                truncated=False,
            ),
        ),
        reviews=(
            ReviewVerdict(rev_tasks[0], "functional", c_sha, True, uuid7()),
            ReviewVerdict(rev_tasks[1], "security", c_sha, True, uuid7()),
            ReviewVerdict(rev_tasks[2], "ui-e2e", c_sha, True, uuid7()),
        ),
    )
    PostgresCycleStore(dbs["api"]).create(cycle)
    return cid, c_sha, created, claim, cycle


def _propose_dev_pr_through_gateway(
    dbs: dict[str, Database],
    gw: ToolGateway,
    claim: control.Claim,
    args: dict[str, Any],
) -> tuple[str, str]:
    """Propose `dev_github_pr` in Run HEAD and submit through REAL ToolGateway.submit (R2-03)."""
    tool_call_id = f"tc-pr-{uuid7()[:12]}"
    with dbs["model_gateway"].tx() as c:
        control.append_head(
            c,
            claim,
            "assistant",
            {
                "kind": "assistant",
                "content": "",
                "tool_calls": [{"id": tool_call_id, "name": "dev_github_pr", "args": args}],
            },
            writer="model_gateway",
        )
    out = _run(gw.submit(claim, tool_call_id, "dev_github_pr", args))
    assert out["status"] == "PENDING_APPROVAL", out
    approval_id = str(out["approvalId"])
    row = dbs["owner"].one(
        "SELECT id FROM agent.tool_intents WHERE tenant_id=%s AND run_id=%s AND invocation_key=%s",
        (claim.tenant_id, claim.run_id, tool_call_id),
    )
    assert row is not None
    return str(row["id"]), approval_id


def _seed_context_artifact(
    dbs: dict[str, Database],
    tenant: dict[str, str],
    *,
    task_id: str,
    run_id: str,
    payload: dict[str, Any] | None = None,
    raw_bytes: bytes | None = None,
    kind: str = "raw",
) -> tuple[str, bytes]:
    """Persist a real agent.context_artifacts row so R3-06/R4-05/R4-06 source attestation can verify it."""
    import hashlib as _hashlib

    if raw_bytes is None:
        assert payload is not None
        raw_bytes = canonical_json(payload).encode("utf-8")
    digest = _hashlib.sha256(raw_bytes).hexdigest()
    art_id = uuid7()
    with dbs["owner"].tx() as c:
        c.execute(
            "INSERT INTO agent.context_artifacts"
            " (tenant_id, id, task_id, run_id, owner_user_id, kind, content_sha256, byte_length,"
            "  nonce, ciphertext, key_id, expires_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'test-k1', now() + interval '1 hour')",
            (
                tenant["tenantId"],
                art_id,
                task_id,
                run_id,
                tenant["userId"],
                kind,
                digest,
                len(raw_bytes),
                b"\x01" * 12,
                raw_bytes + b"\x00" * 16,
            ),
        )
    return art_id, raw_bytes


def test_phase7_projects_design_demo_reviews_and_owner_isolation(
    pg: Any, dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    """DEV-01/02/03/07/09 + R3-06/R3-08: Repo Binding, Context-Artifact-grounded Review Verdicts, Real Demo Render Artifacts."""
    other = admin.provision_tenant(pg.owner)
    app = create_app(dbs["api"])
    h_a = {"Authorization": f"Bearer {tenant['token']}"}
    h_b = {"Authorization": f"Bearer {other['token']}"}

    # DB role isolation: sentinel/relay must NOT have access to new dev_* tables
    with pytest.raises(InsufficientPrivilege):
        dbs["sentinel"].one("SELECT id FROM agent.dev_projects LIMIT 1")
    with pytest.raises(InsufficientPrivilege):
        dbs["relay"].one("SELECT id FROM agent.dev_pr_records LIMIT 1")
    with pytest.raises(InsufficientPrivilege):
        dbs["sentinel"].one("SELECT id FROM agent.dev_preview_instances LIMIT 1")
    with pytest.raises(InsufficientPrivilege):
        dbs["relay"].one("SELECT id FROM agent.dev_review_artifacts LIMIT 1")
    with pytest.raises(InsufficientPrivilege):
        dbs["sentinel"].one("SELECT id FROM agent.dev_review_assignments LIMIT 1")
    with pytest.raises(InsufficientPrivilege):
        dbs["sentinel"].one("SELECT id FROM agent.dev_demo_artifacts LIMIT 1")

    other_binding_id = _seed_repository_binding(dbs, other, repository="org/phase7-app")
    own_binding_id = _seed_repository_binding(dbs, tenant, repository="org/phase7-app")

    with TestClient(app) as client:
        # Legacy ambiguous demoChoice=AUTO is rejected 422
        r_bad = client.post(
            "/v1/development/projects",
            headers=h_a,
            json={
                "projectId": "proj-bad",
                "name": "Bad Prefs",
                "repository": "org/phase7-app",
                "preferences": {"demoChoice": "AUTO"},
            },
        )
        assert r_bad.status_code == 422
        assert "DEPRECATED_DEMO_CHOICE_AUTO" in r_bad.text

        # R2-02: Binding another tenant's repositoryBindingId OR wrong repository is denied 403
        r_cross_bind = client.post(
            "/v1/development/projects",
            headers=h_a,
            json={
                "projectId": "proj-cross",
                "name": "Cross Bind",
                "repository": "org/phase7-app",
                "repositoryBindingId": other_binding_id,
            },
        )
        assert r_cross_bind.status_code == 403

        r_wrong_repo_bind = client.post(
            "/v1/development/projects",
            headers=h_a,
            json={
                "projectId": "proj-wrong-repo",
                "name": "Wrong Repo Bind",
                "repository": "org/different-app",
                "repositoryBindingId": own_binding_id,
            },
        )
        assert r_wrong_repo_bind.status_code == 403

        # Valid project creation with verified repositoryBindingId & explicit policy requiredChecks
        r_proj = client.post(
            "/v1/development/projects",
            headers=h_a,
            json={
                "projectId": "proj-p7-1",
                "name": "Phase7 Demo App",
                "repository": "org/phase7-app",
                "defaultBranch": "main",
                "framework": "web",
                "repositoryBindingId": own_binding_id,
                "preferences": {
                    "designPolicy": "AUTO_RECOMMEND",
                    "demoAcceptancePolicy": "REQUIRE_USER_APPROVAL",
                    "releasePolicy": "PR_AFTER_APPROVED_SCOPE",
                    "requiredChecks": ["pytest-gate"],
                },
            },
        )
        assert r_proj.status_code == 201, r_proj.text
        proj_id = r_proj.json()["project"]["id"]

        # Cross-tenant owner cannot read project (404)
        assert client.get(f"/v1/development/projects/{proj_id}", headers=h_b).status_code == 404

        cycle_id, commit_sha, created, claim, _ = _seed_code_ready_cycle(dbs, tenant, project_id=proj_id)

        # R2-07: PostgresReviewAttestationPort strictly verifies SUCCEEDED run, cycle, verdict digest & ALL findings
        attest = PostgresReviewAttestationPort(dbs["api"])
        dev_store = PostgresDevStore(dbs["api"])
        proj_scope = ProjectScope(
            tenant["tenantId"],
            tenant["userId"],
            proj_id,
            tenant["workspaceId"],
            "org/phase7-app",
            "feat/p7-demo",
            "1" * 40,
            "1",
        )
        builder_fence = RunFence(
            claim.task_id,
            claim.run_id,
            claim.goal_revision,
            claim.lease_epoch,
            claim.restore_incarnation,
            claim.worker_ref,
        )
        with pytest.raises(ScopeDenied, match="self-review"):
            attest.assert_verified(
                proj_scope, builder_fence, ReviewVerdict(claim.task_id, "functional", commit_sha, True, uuid7())
            )
        indep = commands.submit_task(
            dbs["api"], Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"]), "indep-review"
        )
        # R4-06: Self-review assignment MUST be rejected, and unassigned reviewer artifacts MUST be rejected!
        with pytest.raises(ScopeDenied, match="SELF_REVIEW_ASSIGNMENT_FORBIDDEN"):
            dev_store.assign_reviewer(
                tenant_id=tenant["tenantId"],
                owner_user_id=tenant["userId"],
                project_id=proj_id,
                cycle_id=cycle_id,
                reviewer_task_id=claim.task_id,
                reviewer_run_id=claim.run_id,
                reviewer_role="security",
                target_sha=commit_sha,
            )

        good_payload = {
            "purpose": "independent_code_review",
            "projectId": proj_id,
            "cycleId": cycle_id,
            "reviewerRole": "security",
            "targetSha": commit_sha,
            "passed": True,
            "findings": [{"findingId": "f1", "severity": "P2", "status": "RESOLVED"}],
            "summary": "resolved finding",
        }
        good_src_id, good_raw = _seed_context_artifact(
            dbs, tenant, task_id=indep["taskId"], run_id=indep["runId"], payload=good_payload
        )
        # Unassigned reviewer run MUST fail closed (REVIEWER_ASSIGNMENT_MISMATCH)!
        with pytest.raises(ScopeDenied, match="REVIEWER_ASSIGNMENT_MISMATCH"):
            dev_store.record_review_artifact(
                tenant_id=tenant["tenantId"],
                owner_user_id=tenant["userId"],
                project_id=proj_id,
                cycle_id=cycle_id,
                task_id=indep["taskId"],
                run_id=indep["runId"],
                source_artifact_id=good_src_id,
                evidence_payload=good_raw,
            )

        # Authoritatively assign indep task/run as `security` reviewer for this cycle + commit_sha (kind='raw')
        asg_id = dev_store.assign_reviewer(
            tenant_id=tenant["tenantId"],
            owner_user_id=tenant["userId"],
            project_id=proj_id,
            cycle_id=cycle_id,
            reviewer_task_id=indep["taskId"],
            reviewer_run_id=indep["runId"],
            reviewer_role="security",
            target_sha=commit_sha,
            expected_artifact_kind="raw",
        )

        # R4-06: Missing purpose, role mismatch, or wrong context_artifact kind ('summary' vs 'raw') MUST fail!
        no_purpose_payload = {k: v for k, v in good_payload.items() if k != "purpose"}
        no_purpose_id, no_purpose_raw = _seed_context_artifact(
            dbs, tenant, task_id=indep["taskId"], run_id=indep["runId"], payload=no_purpose_payload
        )
        with pytest.raises(InvalidEvidence, match="purpose='independent_code_review'"):
            dev_store.record_review_artifact(
                tenant_id=tenant["tenantId"],
                owner_user_id=tenant["userId"],
                project_id=proj_id,
                cycle_id=cycle_id,
                task_id=indep["taskId"],
                run_id=indep["runId"],
                source_artifact_id=no_purpose_id,
                evidence_payload=no_purpose_raw,
            )
        wrong_role_payload = {**good_payload, "reviewerRole": "functional"}
        wrong_role_id, wrong_role_raw = _seed_context_artifact(
            dbs, tenant, task_id=indep["taskId"], run_id=indep["runId"], payload=wrong_role_payload
        )
        with pytest.raises(ScopeDenied, match="REVIEWER_ASSIGNMENT_MISMATCH"):
            dev_store.record_review_artifact(
                tenant_id=tenant["tenantId"],
                owner_user_id=tenant["userId"],
                project_id=proj_id,
                cycle_id=cycle_id,
                task_id=indep["taskId"],
                run_id=indep["runId"],
                source_artifact_id=wrong_role_id,
                evidence_payload=wrong_role_raw,
            )
        summary_kind_id, summary_kind_raw = _seed_context_artifact(
            dbs, tenant, task_id=indep["taskId"], run_id=indep["runId"], payload=good_payload, kind="summary"
        )
        with pytest.raises(ScopeDenied, match="SOURCE_ARTIFACT_MISMATCH"):
            dev_store.record_review_artifact(
                tenant_id=tenant["tenantId"],
                owner_user_id=tenant["userId"],
                project_id=proj_id,
                cycle_id=cycle_id,
                task_id=indep["taskId"],
                run_id=indep["runId"],
                source_artifact_id=summary_kind_id,
                evidence_payload=summary_kind_raw,
            )

        # R3-06: Cannot record passed=True with an OPEN P0/P1 finding (payload itself drives verdict fields)
        bad_payload = {
            "purpose": "independent_code_review",
            "reviewerRole": "security",
            "targetSha": commit_sha,
            "passed": True,
            "findings": [{"findingId": "f0", "severity": "P0", "status": "OPEN"}],
            "summary": "critical issue",
        }
        bad_src_id, bad_raw = _seed_context_artifact(
            dbs, tenant, task_id=indep["taskId"], run_id=indep["runId"], payload=bad_payload
        )
        with pytest.raises(InvalidEvidence, match="open/unverified P0 or P1"):
            dev_store.record_review_artifact(
                tenant_id=tenant["tenantId"],
                owner_user_id=tenant["userId"],
                project_id=proj_id,
                cycle_id=cycle_id,
                task_id=indep["taskId"],
                run_id=indep["runId"],
                source_artifact_id=bad_src_id,
                evidence_payload=bad_raw,
            )

        # R3-06: Tampering payload bytes vs persisted context_artifacts MUST be rejected (SOURCE_ARTIFACT_MISMATCH)!
        with pytest.raises(ScopeDenied, match="SOURCE_ARTIFACT_MISMATCH"):
            dev_store.record_review_artifact(
                tenant_id=tenant["tenantId"],
                owner_user_id=tenant["userId"],
                project_id=proj_id,
                cycle_id=cycle_id,
                task_id=indep["taskId"],
                run_id=indep["runId"],
                source_artifact_id=good_src_id,
                evidence_payload={**good_payload, "findings": []},
            )
        # Non-existent context_artifact ID MUST also be rejected!
        with pytest.raises(ScopeDenied, match="SOURCE_ARTIFACT_MISMATCH"):
            dev_store.record_review_artifact(
                tenant_id=tenant["tenantId"],
                owner_user_id=tenant["userId"],
                project_id=proj_id,
                cycle_id=cycle_id,
                task_id=indep["taskId"],
                run_id=indep["runId"],
                source_artifact_id=uuid7(),
                evidence_payload=good_raw,
            )
        art_id = dev_store.record_review_artifact(
            tenant_id=tenant["tenantId"],
            owner_user_id=tenant["userId"],
            project_id=proj_id,
            cycle_id=cycle_id,
            task_id=indep["taskId"],
            run_id=indep["runId"],
            source_artifact_id=good_src_id,
            evidence_payload=good_raw,
        )
        # QUEUED reviewer run -> rejected
        with pytest.raises(ScopeDenied, match="did not SUCCEED"):
            attest.assert_verified(
                proj_scope,
                builder_fence,
                ReviewVerdict(
                    indep["taskId"], "security", commit_sha, True, art_id, (ReviewFinding("f1", "P2", "RESOLVED"),)
                ),
                cycle_id=cycle_id,
            )
        # Transition indep run to SUCCEEDED
        with dbs["owner"].tx() as c:
            c.execute(
                "UPDATE agent.runs SET status_code='SUCCEEDED' WHERE tenant_id=%s AND id=%s",
                (tenant["tenantId"], indep["runId"]),
            )
        # R2-07: Dropping finding f1 from submitted ReviewVerdict MUST be rejected!
        with pytest.raises(ScopeDenied, match="findings do not match"):
            attest.assert_verified(
                proj_scope,
                builder_fence,
                ReviewVerdict(indep["taskId"], "security", commit_sha, True, art_id, ()),
                cycle_id=cycle_id,
            )
        # Reconstructing authoritative ReviewVerdict directly from persisted artifact returns ALL findings
        reconstructed = attest.load_verified_verdict(proj_scope, builder_fence, cycle_id=cycle_id, artifact_id=art_id)
        assert reconstructed.findings == (ReviewFinding("f1", "P2", "RESOLVED"),)
        attest.assert_verified(proj_scope, builder_fence, reconstructed, cycle_id=cycle_id)

        # R4-06: Revoking the reviewer assignment MUST immediately fail attestation!
        with dbs["owner"].tx() as c:
            c.execute(
                "UPDATE agent.dev_review_assignments SET status='REVOKED' WHERE tenant_id=%s AND id=%s",
                (tenant["tenantId"], asg_id),
            )
        with pytest.raises(ScopeDenied, match="assignment/purpose mismatch"):
            attest.load_verified_verdict(proj_scope, builder_fence, cycle_id=cycle_id, artifact_id=art_id)
        with dbs["owner"].tx() as c:
            c.execute(
                "UPDATE agent.dev_review_assignments SET status='COMPLETED' WHERE tenant_id=%s AND id=%s",
                (tenant["tenantId"], asg_id),
            )

        # Design Picker + Demo approval with REAL rendered artifact (R3-08) + live goal_revision check (P1-06 + R2-11)
        r_des = client.post(
            f"/v1/development/cycles/{cycle_id}/design",
            headers=h_a,
            json={
                "candidates": [
                    {
                        "candidateId": "win11",
                        "label": "Windows 11",
                        "fixtureRef": "fixture://dash-1",
                        "summary": "Fluent",
                    },
                    {
                        "candidateId": "minimal",
                        "label": "Minimal",
                        "fixtureRef": "fixture://dash-1",
                        "summary": "Clean",
                    },
                    {
                        "candidateId": "dense",
                        "label": "Dense Pro",
                        "fixtureRef": "fixture://dash-1",
                        "summary": "Compact",
                    },
                ]
            },
        )
        assert r_des.status_code == 201

        # R3-08: Passing an unverified/self-declared demoArtifactSha256 without a persisted dev_demo_artifacts row fails 422!
        r_fake_sha = client.post(
            f"/v1/development/cycles/{cycle_id}/design-decision",
            headers=h_a,
            json={
                "designRevision": 1,
                "selectedCandidateId": "minimal",
                "demoArtifactRef": "artifact://unverified",
                "demoArtifactSha256": "a" * 64,
            },
        )
        assert r_fake_sha.status_code == 422 and "RENDERED_DEMO_ARTIFACT_REQUIRED" in r_fake_sha.text

        # R3-08: Selecting without a rendered artifact succeeds as PENDING, but Demo APPROVED fails 422!
        r_sel_unrendered = client.post(
            f"/v1/development/cycles/{cycle_id}/design-decision",
            headers=h_a,
            json={"designRevision": 1, "selectedCandidateId": "minimal"},
        )
        assert r_sel_unrendered.status_code == 200
        assert r_sel_unrendered.json()["design"]["demoArtifactSha256"] is None
        r_demo_unrendered = client.post(
            f"/v1/development/cycles/{cycle_id}/demo-decision",
            headers=h_a,
            json={
                "designRevision": 1,
                "decision": "APPROVED",
                "demoSnapshotDigest": r_sel_unrendered.json()["design"]["demoSnapshotDigest"],
            },
        )
        assert r_demo_unrendered.status_code == 422 and "RENDERED_DEMO_ARTIFACT_REQUIRED" in r_demo_unrendered.text

        # R4-05: Persist real raw Demo render bytes in canonical `agent.context_artifacts` and bind via source_artifact_id!
        min_bytes = b"<html><body>Minimal Rendered Demo v1</body></html>"
        dense_bytes = b"<html><body>Dense Rendered Demo v1</body></html>"
        min_ctx_art_id, _ = _seed_context_artifact(
            dbs, tenant, task_id=claim.task_id, run_id=claim.run_id, raw_bytes=min_bytes, kind="raw"
        )
        dense_ctx_art_id, _ = _seed_context_artifact(
            dbs, tenant, task_id=claim.task_id, run_id=claim.run_id, raw_bytes=dense_bytes, kind="raw"
        )
        # Non-existent or mismatched context_artifacts backing MUST fail closed!
        with pytest.raises(ScopeDenied, match="DEMO_SOURCE_ARTIFACT_MISMATCH"):
            dev_store.record_demo_render_artifact(
                tenant_id=tenant["tenantId"],
                owner_user_id=tenant["userId"],
                cycle_id=cycle_id,
                candidate_id="minimal",
                viewport="1280x800",
                fixture_ref="fixture://dash-1",
                source_artifact_id=min_ctx_art_id,
                render_bytes=b"<html><body>TAMPERED BYTES</body></html>",
            )
        with pytest.raises(ScopeDenied, match="DEMO_SOURCE_ARTIFACT_MISMATCH"):
            dev_store.record_demo_render_artifact(
                tenant_id=tenant["tenantId"],
                owner_user_id=tenant["userId"],
                cycle_id=cycle_id,
                candidate_id="minimal",
                viewport="1280x800",
                fixture_ref="fixture://dash-1",
                source_artifact_id=uuid7(),
                render_bytes=min_bytes,
            )

        render_min = dev_store.record_demo_render_artifact(
            tenant_id=tenant["tenantId"],
            owner_user_id=tenant["userId"],
            cycle_id=cycle_id,
            candidate_id="minimal",
            viewport="1280x800",
            fixture_ref="fixture://dash-1",
            source_artifact_id=min_ctx_art_id,
            render_bytes=min_bytes,
        )
        assert render_min["sourceArtifactId"] == min_ctx_art_id
        render_dense = dev_store.record_demo_render_artifact(
            tenant_id=tenant["tenantId"],
            owner_user_id=tenant["userId"],
            cycle_id=cycle_id,
            candidate_id="dense",
            viewport="1280x800",
            fixture_ref="fixture://dash-1",
            source_artifact_id=dense_ctx_art_id,
            render_bytes=dense_bytes,
        )
        r_sel = client.post(
            f"/v1/development/cycles/{cycle_id}/design-decision",
            headers=h_a,
            json={
                "designRevision": 1,
                "selectedCandidateId": "minimal",
                "demoRenderArtifactId": render_min["id"],
            },
        )
        assert r_sel.status_code == 200, r_sel.text
        digest_minimal = r_sel.json()["design"]["demoSnapshotDigest"]
        assert r_sel.json()["design"]["demoRenderArtifactId"] == render_min["id"]
        assert r_sel.json()["design"]["demoArtifactSha256"] == render_min["contentSha256"]
        assert r_sel.json()["design"]["demoArtifactRef"] == f"context-artifact://{min_ctx_art_id}"

        # R4-05: Revoking the backing context_artifacts entry blocks Demo APPROVED fail-closed!
        with dbs["owner"].tx() as c:
            c.execute(
                "UPDATE agent.context_artifacts SET revoked_at=now(), ciphertext=NULL WHERE tenant_id=%s AND id=%s",
                (tenant["tenantId"], min_ctx_art_id),
            )
        r_demo_revoked_art = client.post(
            f"/v1/development/cycles/{cycle_id}/demo-decision",
            headers=h_a,
            json={"designRevision": 1, "decision": "APPROVED", "demoSnapshotDigest": digest_minimal},
        )
        assert r_demo_revoked_art.status_code == 409 and "RENDERED_DEMO_ARTIFACT_MISMATCH" in r_demo_revoked_art.text
        with dbs["owner"].tx() as c:
            c.execute(
                "UPDATE agent.context_artifacts SET revoked_at=NULL, ciphertext=%s WHERE tenant_id=%s AND id=%s",
                (min_bytes + b"\x00" * 16, tenant["tenantId"], min_ctx_art_id),
            )

        r_demo = client.post(
            f"/v1/development/cycles/{cycle_id}/demo-decision",
            headers=h_a,
            json={"designRevision": 1, "decision": "APPROVED", "demoSnapshotDigest": digest_minimal},
        )
        assert r_demo.status_code == 200 and r_demo.json()["design"]["demoDecision"] == "APPROVED"

        # Switching candidate resets Demo APPROVED -> PENDING
        r_switch = client.post(
            f"/v1/development/cycles/{cycle_id}/design-decision",
            headers=h_a,
            json={
                "designRevision": 1,
                "selectedCandidateId": "dense",
                "demoRenderArtifactId": render_dense["id"],
            },
        )
        assert r_switch.status_code == 200 and r_switch.json()["design"]["demoDecision"] == "PENDING"
        digest_dense = r_switch.json()["design"]["demoSnapshotDigest"]
        assert digest_dense != digest_minimal

        # R2-11: Steering task (bumping tasks.goal_revision) invalidates Demo approval (409 STALE_GOAL_REVISION)
        r_steer = client.post(
            f"/v1/tasks/{created['taskId']}/steer",
            headers=h_a,
            json={"schemaVersion": "1.0", "goal": "new goal", "expectedGoalRevision": "1"},
        )
        assert r_steer.status_code == 200, r_steer.text
        r_stale_goal = client.post(
            f"/v1/development/cycles/{cycle_id}/demo-decision",
            headers=h_a,
            json={"designRevision": 1, "decision": "APPROVED", "demoSnapshotDigest": digest_dense},
        )
        assert r_stale_goal.status_code == 409
        assert "STALE_GOAL_REVISION" in r_stale_goal.text


@pytest.mark.skipif(
    sys.platform != "linux", reason="PR #110 Sandbox Cell Supervisor requires Linux kernel namespaces/Landlock"
)
def test_phase7_preview_lease_browser_debugger_and_cdp_redaction(
    pg: Any, dbs: dict[str, Database], tenant: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEV-06 + R3-02..R3-05 + R4-02..R4-04: Instance-bound Unix socket (ino+SO_PEERCRED), cgroup fork barrier, DB-authoritative task/run/scope/watcher."""
    pytest.importorskip("playwright.async_api")
    from aiagent_development.browser_debugger import run_sandbox_cell_preview_debug_async
    from aiagent_development.pg_store import format_preview_unix_endpoint_ref
    from aiagent_sandbox.grant import GrantError, PreviewCapability, sign_preview_capability, verify_preview_capability
    from aiagent_sandbox.supervisor import Supervisor, _BrowserCgroup
    from aiagent_sandbox.supervisor import serve as serve_sandbox

    escape_hits: list[str] = []

    class _EscapeReceiver(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_GET(self) -> None:
            escape_hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"escaped")

    escape_srv = ThreadingHTTPServer(("127.0.0.1", 0), _EscapeReceiver)
    threading.Thread(target=escape_srv.serve_forever, daemon=True).start()
    escape_port = escape_srv.server_address[1]

    # R4-03: Preview Runner serves HTTP strictly over an instance-owned AF_UNIX socket (NEVER raw TCP port!)
    srv_port = _free_tcp_port()
    preview_sock_path = Path("/tmp") / f"aiag-test-prv-{secrets.token_hex(6)}.sock"  # noqa: S108
    if preview_sock_path.exists():
        preview_sock_path.unlink()

    async def _preview_unix_http(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout=5.0)
            first = head.decode("latin-1").split("\r\n", 1)[0]
            parts = first.split(" ")
            req_path = parts[1] if len(parts) >= 2 else "/"
            if req_path == "/api/broken":
                body = b'{"error":"boom"}'
                w.write(
                    b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
                    % (len(body), body)
                )
            elif req_path == "/redirect-escape":
                w.write(
                    (
                        f"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{escape_port}/escaped\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    ).encode("latin-1")
                )
            elif req_path == "/slow":
                await asyncio.sleep(2.5)
                body = b"<!doctype html><html><body>slow</body></html>"
                w.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
                    % (len(body), body)
                )
            else:
                html = (
                    "<!doctype html><html><body style='margin:0'>"
                    "<div style='width:900px;height:40px'>Wide mobile overflow</div>"
                    "<iframe src='/redirect-escape'></iframe>"
                    "<script>"
                    "console.error('Auth failed Bearer super_secret_token_999 ws://127.0.0.1:9222/devtools/page/abc');"
                    "fetch('/api/broken');"
                    "fetch('http://evil.example/steal').catch(() => {});"
                    f"fetch('http://127.0.0.1:{escape_port}/direct-port-escape').catch(() => {{}});"
                    "</script></body></html>"
                ).encode()
                w.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
                    % (len(html), html)
                )
            await w.drain()
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            w.close()

    async def _boot_preview_unix(holder: dict[str, Any]) -> None:
        unix_srv = await asyncio.start_unix_server(_preview_unix_http, path=str(preview_sock_path))
        os.chmod(preview_sock_path, 0o600)
        holder["ino"] = os.lstat(preview_sock_path).st_ino

        async def _cleanup() -> None:
            unix_srv.close()
            await unix_srv.wait_closed()
            preview_sock_path.unlink(missing_ok=True)

        holder["cleanup"] = _cleanup

    prv_h, prv_loop, prv_th = _start_async_service(_boot_preview_unix)
    preview_ino = int(prv_h["ino"])
    preview_pid = os.getpid()
    preview_ep_ref = format_preview_unix_endpoint_ref(str(preview_sock_path), inode=preview_ino, pid=preview_pid)

    # R3-02: Separate preview_capability_key from sandbox_grant_key; reusing the same key MUST fail closed!
    sb_port = _free_tcp_port()
    sb_rpc_key = secrets.token_bytes(32)
    sb_grant_key = secrets.token_bytes(32)
    preview_cap_key = secrets.token_bytes(32)
    attest_key = secrets.token_bytes(32)
    sb_endpoint = f"tcp:127.0.0.1:{sb_port}"

    with pytest.raises(RuntimeError, match="distinct from grant_key"):
        Supervisor(tmp_path / "bad_sup", sb_grant_key, preview_capability_key=sb_grant_key)

    # R3-02 + R4-03: PreviewCapability binds instance Unix socket (ino+pid); raw TCP port endpoint_ref MUST be rejected!
    raw_tcp_cap = PreviewCapability(
        capability_id=f"prvcap-{secrets.token_hex(12)}",
        tenant_id=tenant["tenantId"],
        owner_user_id=tenant["userId"],
        project_id="proj-dbg-1",
        cycle_id="cyc-1",
        workspace_id=tenant["workspaceId"],
        task_id=uuid7(),
        run_id=uuid7(),
        lease_id=uuid7(),
        preview_instance_id=uuid7(),
        instance_generation=2,
        target_port=srv_port,
        target_url=f"http://127.0.0.1:{srv_port}/",
        endpoint_ref=f"sandbox-cell://127.0.0.1:{srv_port}",
        commit_sha="2" * 40,
        wall_ms=15_000,
        memory_bytes=2 * 1024 * 1024 * 1024,
        pids_max=256,
        cpu_quota_us=100_000,
        expires=int(time.time()) + 30,
        attempt_id="dbg-bad-tcp",
    )
    with pytest.raises(GrantError, match="PREVIEW_CAP_ENDPOINT_MISMATCH"):
        verify_preview_capability(preview_cap_key, sign_preview_capability(preview_cap_key, raw_tcp_cap))

    sample_cap = PreviewCapability(
        capability_id=f"prvcap-{secrets.token_hex(12)}",
        tenant_id=tenant["tenantId"],
        owner_user_id=tenant["userId"],
        project_id="proj-dbg-1",
        cycle_id="cyc-1",
        workspace_id=tenant["workspaceId"],
        task_id=uuid7(),
        run_id=uuid7(),
        lease_id=uuid7(),
        preview_instance_id=uuid7(),
        instance_generation=2,
        target_port=srv_port,
        target_url=f"http://127.0.0.1:{srv_port}/",
        endpoint_ref=preview_ep_ref,
        commit_sha="2" * 40,
        wall_ms=15_000,
        memory_bytes=2 * 1024 * 1024 * 1024,
        pids_max=256,
        cpu_quota_us=100_000,
        expires=int(time.time()) + 30,
        attempt_id="dbg-sample",
    )
    signed_sample = sign_preview_capability(preview_cap_key, sample_cap)
    with pytest.raises(GrantError, match="PREVIEW_CAP_BAD_SIGNATURE"):
        verify_preview_capability(sb_grant_key, signed_sample)
    tampered_port = {
        "capability": {**signed_sample["capability"], "target_port": escape_port},
        "mac": signed_sample["mac"],
    }
    with pytest.raises(GrantError, match="PREVIEW_CAP_BAD_SIGNATURE"):
        verify_preview_capability(preview_cap_key, tampered_port)

    # R4-03: Wrong inode in endpoint_ref MUST fail closed BEFORE launching any browser cell!
    wrong_ino_sup = Supervisor(
        tmp_path / "wrong_ino_cells",
        sb_grant_key,
        preview_capability_key=preview_cap_key,
        require_browser_cgroup=False,
    )
    wrong_ino_cap = PreviewCapability(
        **{
            **sample_cap.payload(),
            "capability_id": f"prvcap-{secrets.token_hex(12)}",
            "endpoint_ref": format_preview_unix_endpoint_ref(
                str(preview_sock_path), inode=preview_ino + 999999, pid=preview_pid
            ),
        }
    )
    with pytest.raises(GrantError, match="PREVIEW_ENDPOINT_UNVERIFIED"):
        _run(wrong_ino_sup.debug_preview(sign_preview_capability(preview_cap_key, wrong_ino_cap)))

    # R4-03: Wrong PID (SO_PEERCRED mismatch) MUST close bridge without serving page!
    wrong_pid_cap = PreviewCapability(
        **{
            **sample_cap.payload(),
            "capability_id": f"prvcap-{secrets.token_hex(12)}",
            "endpoint_ref": format_preview_unix_endpoint_ref(
                str(preview_sock_path), inode=preview_ino, pid=preview_pid + 12345
            ),
        }
    )
    wrong_pid_res = _run(wrong_ino_sup.debug_preview(sign_preview_capability(preview_cap_key, wrong_pid_cap)))
    assert wrong_pid_res["status"] == "EXITED" and wrong_pid_res["exitCode"] != 0

    # R3-04 + R4-02: Verify _BrowserCgroup writes memory.max/pids.max/cpu.max, AND cgroup attach failure aborts BEFORE fork!
    fake_cg_root = tmp_path / "fake_cgroup"
    fake_cg_root.mkdir()
    cg = _BrowserCgroup.create(
        name="aiagent-prv-unit",
        memory_bytes=1024 * 1024 * 1024,
        pids_max=192,
        cpu_quota_us=80_000,
        cgroup_root=fake_cg_root,
    )
    assert cg is not None
    assert (cg.path / "memory.max").read_text().strip() == str(1024 * 1024 * 1024)
    assert (cg.path / "pids.max").read_text().strip() == "192"
    assert (cg.path / "cpu.max").read_text().strip() == "80000 100000"
    assert cg.attach(os.getpid()) is True
    assert (cg.path / "cgroup.procs").read_text().strip() == str(os.getpid())
    for ctrl in ("memory.max", "pids.max", "cpu.max", "cgroup.procs"):
        (cg.path / ctrl).unlink(missing_ok=True)
    cg.cleanup()
    assert not cg.path.exists()

    # R4-02: When cgroup.attach fails under require_browser_cgroup=True, start barrier is NEVER released (child never forks)!
    attach_fail_root = tmp_path / "attach_fail_cg"
    attach_fail_root.mkdir()
    attach_fail_sup = Supervisor(
        tmp_path / "attach_fail_cells",
        sb_grant_key,
        preview_capability_key=preview_cap_key,
        require_browser_cgroup=True,
        cgroup_root=attach_fail_root,
    )
    orig_attach = _BrowserCgroup.attach
    attach_calls: list[int] = []

    def _fail_attach(self_cg: _BrowserCgroup, pid: int) -> bool:
        del self_cg
        attach_calls.append(pid)
        return False

    monkeypatch.setattr(_BrowserCgroup, "attach", _fail_attach)
    try:
        attach_fail_cap = PreviewCapability(
            **{**sample_cap.payload(), "capability_id": f"prvcap-{secrets.token_hex(12)}"}
        )
        with pytest.raises(GrantError, match="BROWSER_CGROUP_ATTACH_FAILED"):
            _run(attach_fail_sup.debug_preview(sign_preview_capability(preview_cap_key, attach_fail_cap)))
        assert len(attach_calls) == 1
    finally:
        monkeypatch.setattr(_BrowserCgroup, "attach", orig_attach)

    strict_sup = Supervisor(
        tmp_path / "strict_cg_cells",
        sb_grant_key,
        preview_capability_key=preview_cap_key,
        require_browser_cgroup=True,
        cgroup_root=tmp_path / "nonexistent_ro_parent" / "nested",
    )
    (tmp_path / "nonexistent_ro_parent").mkdir()
    os.chmod(tmp_path / "nonexistent_ro_parent", 0o500)
    try:
        with pytest.raises(GrantError, match="BROWSER_CGROUP_REQUIRED"):
            _run(strict_sup.debug_preview(signed_sample))
    finally:
        os.chmod(tmp_path / "nonexistent_ro_parent", 0o700)

    # Boot REAL Sandbox Supervisor accepting ONLY caller `tool_gateway` (matches production supervisor.py main()!)
    async def _boot_sandbox(holder: dict[str, Any]) -> None:
        sup = Supervisor(
            tmp_path / "sandbox_cells",
            sb_grant_key,
            require_netns=True,
            cell_uid=65534 if os.geteuid() == 0 else None,
            cell_gid=65534 if os.geteuid() == 0 else None,
            preview_capability_key=preview_cap_key,
            require_browser_cgroup=False,
        )
        task = asyncio.create_task(serve_sandbox(sup, {"tool_gateway": sb_rpc_key}, sb_endpoint))
        await asyncio.sleep(0.2)

        async def _cleanup() -> None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        holder["cleanup"] = _cleanup

    _, sb_loop, sb_th = _start_async_service(_boot_sandbox)

    # Boot REAL ToolGateway RPC server (the ONLY component holding Sandbox RPC & PreviewCapability keys!)
    tg_rpc_port = _free_tcp_port()
    tg_endpoint = f"tcp:127.0.0.1:{tg_rpc_port}"
    api_tg_key = secrets.token_bytes(32)
    gw = ToolGateway(dbs["tool_gateway"], {})

    async def _boot_tg(holder: dict[str, Any]) -> None:
        tg_srv = build_tool_server({"api": api_tg_key}, tg_endpoint, gw)
        await tg_srv.start()

        async def _cleanup() -> None:
            await tg_srv.close()

        holder["cleanup"] = _cleanup

    _, tg_loop, tg_th = _start_async_service(_boot_tg)

    # API gets ONLY Preview Attestation key & ToolGateway RPC (ZERO sandbox_grant_key / sandbox_rpc_key / preview_cap_key!)
    monkeypatch.setenv("AIAGENT_PREVIEW_ATTESTATION_KEY", attest_key.hex())
    monkeypatch.delenv("AIAGENT_TOOL_GATEWAY_ENDPOINT", raising=False)
    monkeypatch.delenv("AIAGENT_API_TOOL_RPC_KEY", raising=False)
    monkeypatch.delenv("AIAGENT_SANDBOX_ENDPOINT", raising=False)
    monkeypatch.delenv("AIAGENT_SANDBOX_RPC_KEY", raising=False)
    monkeypatch.delenv("AIAGENT_SANDBOX_GRANT_KEY", raising=False)
    monkeypatch.delenv("AIAGENT_PREVIEW_CAPABILITY_KEY", raising=False)

    app = create_app(dbs["api"])
    headers = {"Authorization": f"Bearer {tenant['token']}"}

    try:
        with TestClient(app) as client:
            r_p = client.post(
                "/v1/development/projects",
                headers=headers,
                json={"projectId": "proj-dbg-1", "name": "Debug App", "repository": "org/dbg-app"},
            )
            assert r_p.status_code == 201
            cycle_a, sha_a, created_a, _, cycle_a_obj = _seed_code_ready_cycle(
                dbs, tenant, project_id="proj-dbg-1", repository="org/dbg-app"
            )
            cycle_b, _, _, _, _ = _seed_code_ready_cycle(
                dbs, tenant, project_id="proj-dbg-1", repository="org/dbg-app", commit_sha="3" * 40
            )

            broker = PreviewBroker(dbs["api"])
            proc_sha = sha256_hex({"cycle": cycle_a, "endpoint": preview_ep_ref})
            now_s = int(time.time())

            # R3-05 + R4-03: Raw TCP endpoint_ref, forged HMAC, & expired launch receipts MUST be rejected!
            with pytest.raises(ScopeDenied, match="INVALID_PREVIEW_ENDPOINT_REF"):
                broker.register_instance(
                    cycle_a_obj,
                    target_port=srv_port,
                    endpoint_ref=f"sandbox-cell://127.0.0.1:{srv_port}",
                    process_attestation_sha256=proc_sha,
                    launch_receipt_hmac="f" * 64,
                    launch_nonce=f"nonce-badtcp-{secrets.token_hex(8)}",
                    issued_at_epoch=now_s,
                    expires_at_epoch=now_s + 30,
                )
            with pytest.raises(ScopeDenied, match="INVALID_PREVIEW_LAUNCH_RECEIPT"):
                broker.register_instance(
                    cycle_a_obj,
                    target_port=srv_port,
                    endpoint_ref=preview_ep_ref,
                    process_attestation_sha256=proc_sha,
                    launch_receipt_hmac="f" * 64,
                    launch_nonce=f"nonce-forged-{secrets.token_hex(8)}",
                    issued_at_epoch=now_s,
                    expires_at_epoch=now_s + 30,
                )
            expired_nonce = f"nonce-expired-{secrets.token_hex(8)}"
            rcpt_exp = sign_preview_launch_receipt(
                attest_key,
                tenant_id=tenant["tenantId"],
                owner_user_id=tenant["userId"],
                project_id="proj-dbg-1",
                cycle_id=cycle_a,
                workspace_id=tenant["workspaceId"],
                commit_sha=sha_a,
                target_port=srv_port,
                endpoint_ref=preview_ep_ref,
                runner_kind="sandbox_cell",
                process_attestation_sha256=proc_sha,
                generation=1,
                launch_nonce=expired_nonce,
                issued_at_epoch=now_s - 50,
                expires_at_epoch=now_s - 5,
            )
            with pytest.raises(ScopeDenied, match="EXPIRED_PREVIEW_LAUNCH_RECEIPT"):
                broker.register_instance(
                    cycle_a_obj,
                    target_port=srv_port,
                    endpoint_ref=preview_ep_ref,
                    process_attestation_sha256=proc_sha,
                    launch_receipt_hmac=rcpt_exp,
                    launch_nonce=expired_nonce,
                    issued_at_epoch=now_s - 50,
                    expires_at_epoch=now_s - 5,
                    generation=1,
                )

            # Register generation 1 with valid short-lived single-use HMAC launch receipt bound to Unix socket (ino+pid)
            nonce_g1 = f"nonce-g1-{secrets.token_hex(10)}"
            rcpt_g1 = sign_preview_launch_receipt(
                attest_key,
                tenant_id=tenant["tenantId"],
                owner_user_id=tenant["userId"],
                project_id="proj-dbg-1",
                cycle_id=cycle_a,
                workspace_id=tenant["workspaceId"],
                commit_sha=sha_a,
                target_port=srv_port,
                endpoint_ref=preview_ep_ref,
                runner_kind="sandbox_cell",
                process_attestation_sha256=proc_sha,
                generation=1,
                launch_nonce=nonce_g1,
                issued_at_epoch=now_s,
                expires_at_epoch=now_s + 45,
            )
            inst_g1 = broker.register_instance(
                cycle_a_obj,
                target_port=srv_port,
                endpoint_ref=preview_ep_ref,
                process_attestation_sha256=proc_sha,
                launch_receipt_hmac=rcpt_g1,
                launch_nonce=nonce_g1,
                issued_at_epoch=now_s,
                expires_at_epoch=now_s + 45,
                generation=1,
            )

            # R3-05: Replaying the SAME launch_nonce OR non-monotonic generation (<= 1) MUST be rejected!
            with pytest.raises(StateConflict, match="NON_MONOTONIC_PREVIEW_GENERATION"):
                broker.register_instance(
                    cycle_a_obj,
                    target_port=srv_port,
                    endpoint_ref=preview_ep_ref,
                    process_attestation_sha256=proc_sha,
                    launch_receipt_hmac=rcpt_g1,
                    launch_nonce=nonce_g1,
                    issued_at_epoch=now_s,
                    expires_at_epoch=now_s + 45,
                    generation=1,
                )
            rcpt_replay_nonce = sign_preview_launch_receipt(
                attest_key,
                tenant_id=tenant["tenantId"],
                owner_user_id=tenant["userId"],
                project_id="proj-dbg-1",
                cycle_id=cycle_a,
                workspace_id=tenant["workspaceId"],
                commit_sha=sha_a,
                target_port=srv_port,
                endpoint_ref=preview_ep_ref,
                runner_kind="sandbox_cell",
                process_attestation_sha256=proc_sha,
                generation=2,
                launch_nonce=nonce_g1,
                issued_at_epoch=now_s,
                expires_at_epoch=now_s + 45,
            )
            with pytest.raises(ScopeDenied, match="PREVIEW_LAUNCH_RECEIPT_REPLAYED"):
                broker.register_instance(
                    cycle_a_obj,
                    target_port=srv_port,
                    endpoint_ref=preview_ep_ref,
                    process_attestation_sha256=proc_sha,
                    launch_receipt_hmac=rcpt_replay_nonce,
                    launch_nonce=nonce_g1,
                    issued_at_epoch=now_s,
                    expires_at_epoch=now_s + 45,
                    generation=2,
                )

            r_l1 = client.post(
                f"/v1/development/cycles/{cycle_a}/previews",
                headers=headers,
                json={"targetPort": srv_port, "ttlSeconds": 120, "previewInstanceId": inst_g1},
            )
            assert r_l1.status_code == 201
            lease_g1, tok_g1 = r_l1.json()["leaseId"], r_l1.json()["previewToken"]

            # R3-05: Another tenant attempting to register the SAME running port MUST be rejected (PREVIEW_PORT_OCCUPIED)!
            other = admin.provision_tenant(pg.owner)
            PostgresDevStore(dbs["api"]).create_project(
                tenant_id=other["tenantId"],
                owner_user_id=other["userId"],
                workspace_id=other["workspaceId"],
                project_id="proj-other-port",
                name="Other Port",
                repository="org/other-port",
            )
            cyc_o, sha_o, _, _, cyc_o_obj = _seed_code_ready_cycle(
                dbs, other, project_id="proj-other-port", repository="org/other-port"
            )
            nonce_other = f"nonce-other-{secrets.token_hex(10)}"
            other_ep_ref = format_preview_unix_endpoint_ref("/tmp/other-tenant-prv.sock", inode=55555, pid=preview_pid)
            rcpt_other = sign_preview_launch_receipt(
                attest_key,
                tenant_id=other["tenantId"],
                owner_user_id=other["userId"],
                project_id="proj-other-port",
                cycle_id=cyc_o,
                workspace_id=other["workspaceId"],
                commit_sha=sha_o,
                target_port=srv_port,
                endpoint_ref=other_ep_ref,
                runner_kind="sandbox_cell",
                process_attestation_sha256=proc_sha,
                generation=1,
                launch_nonce=nonce_other,
                issued_at_epoch=now_s,
                expires_at_epoch=now_s + 45,
            )
            with pytest.raises(ScopeDenied, match="PREVIEW_PORT_OCCUPIED"):
                broker.register_instance(
                    cyc_o_obj,
                    target_port=srv_port,
                    endpoint_ref=other_ep_ref,
                    process_attestation_sha256=proc_sha,
                    launch_receipt_hmac=rcpt_other,
                    launch_nonce=nonce_other,
                    issued_at_epoch=now_s,
                    expires_at_epoch=now_s + 45,
                    generation=1,
                )

            # Registering monotonic generation 2 on the same port supersedes generation 1 & revokes lease_g1!
            nonce_g2 = f"nonce-g2-{secrets.token_hex(10)}"
            rcpt_g2 = sign_preview_launch_receipt(
                attest_key,
                tenant_id=tenant["tenantId"],
                owner_user_id=tenant["userId"],
                project_id="proj-dbg-1",
                cycle_id=cycle_a,
                workspace_id=tenant["workspaceId"],
                commit_sha=sha_a,
                target_port=srv_port,
                endpoint_ref=preview_ep_ref,
                runner_kind="sandbox_cell",
                process_attestation_sha256=proc_sha,
                generation=2,
                launch_nonce=nonce_g2,
                issued_at_epoch=now_s,
                expires_at_epoch=now_s + 45,
            )
            inst_g2 = broker.register_instance(
                cycle_a_obj,
                target_port=srv_port,
                endpoint_ref=preview_ep_ref,
                process_attestation_sha256=proc_sha,
                launch_receipt_hmac=rcpt_g2,
                launch_nonce=nonce_g2,
                issued_at_epoch=now_s,
                expires_at_epoch=now_s + 45,
                generation=2,
            )
            assert (
                client.post(
                    f"/v1/development/previews/{lease_g1}/commands",
                    headers=headers,
                    json={"action": "observe", "previewToken": tok_g1, "targetPort": srv_port},
                ).status_code
                == 403
            )

            # Issue fresh lease for generation 2
            r_lease = client.post(
                f"/v1/development/cycles/{cycle_a}/previews",
                headers=headers,
                json={"targetPort": srv_port, "ttlSeconds": 120, "previewInstanceId": inst_g2},
            )
            assert r_lease.status_code == 201
            lease_id, token = r_lease.json()["leaseId"], r_lease.json()["previewToken"]

            # P2-11: Passing Cycle A's lease to Cycle B's debug endpoint fails 403
            assert (
                client.post(
                    f"/v1/development/cycles/{cycle_b}/debug",
                    headers=headers,
                    json={"leaseId": lease_id, "previewToken": token, "targetPort": srv_port, "revokeAfter": False},
                ).status_code
                == 403
            )

            # R2-01/R3-02: Without ToolGateway RPC configured, Browser Debugger FAILS CLOSED (403 SANDBOX_CELL_REQUIRED)
            r_no_sb = client.post(
                f"/v1/development/cycles/{cycle_a}/debug",
                headers=headers,
                json={"leaseId": lease_id, "previewToken": token, "targetPort": srv_port, "revokeAfter": False},
            )
            assert r_no_sb.status_code == 403 and "SANDBOX_CELL_REQUIRED" in r_no_sb.text

            # Wire API -> ToolGateway (`api` caller) and ToolGateway -> Sandbox Cell (`tool_gateway` caller + PreviewCapability key)
            monkeypatch.setenv("AIAGENT_TOOL_GATEWAY_ENDPOINT", tg_endpoint)
            monkeypatch.setenv("AIAGENT_API_TOOL_RPC_KEY", api_tg_key.hex())
            monkeypatch.setenv("AIAGENT_SANDBOX_ENDPOINT", sb_endpoint)
            monkeypatch.setenv("AIAGENT_SANDBOX_RPC_KEY", sb_rpc_key.hex())
            monkeypatch.setenv("AIAGENT_PREVIEW_CAPABILITY_KEY", preview_cap_key.hex())

            # R4-04: Forged caller task_id / run_id MUST be rejected by ToolGateway.dispatch_preview_debug!
            from aiagent_domain.rpcwire import RpcError as _RpcError

            with pytest.raises(_RpcError, match="PREVIEW_TASK_OR_RUN_MISMATCH"):
                _run(
                    gw.dispatch_preview_debug(
                        tenant_id=tenant["tenantId"],
                        owner_user_id=tenant["userId"],
                        cycle_id=cycle_a,
                        lease_id=lease_id,
                        preview_token=token,
                        target_port=srv_port,
                        task_id=uuid7(),
                        run_id=created_a["runId"],
                        revoke_after=False,
                    )
                )

            # R4-04: Watcher exception (e.g. DB error) during active execution MUST fail closed & cancel cell!
            lease_det = broker.verify_lease_details(
                tenant["tenantId"], tenant["userId"], lease_id, token, srv_port, expected_cycle_id=cycle_a
            )
            assert lease_det.task_id == created_a["taskId"] and lease_det.run_id == created_a["runId"]

            def _exploding_watcher() -> bool:
                raise RuntimeError("simulated DB outage during live preview watch")

            with pytest.raises(ScopeDenied, match="PREVIEW_LEASE_REVOKED_OR_WATCHER_FAILED"):
                _run(
                    run_sandbox_cell_preview_debug_async(
                        tenant_id=tenant["tenantId"],
                        owner_user_id=tenant["userId"],
                        task_id=lease_det.task_id,
                        run_id=lease_det.run_id,
                        lease=lease_det,
                        target_url=f"http://127.0.0.1:{srv_port}/slow",
                        sandbox_endpoint=sb_endpoint,
                        sandbox_rpc_key=sb_rpc_key,
                        preview_capability_key=preview_cap_key,
                        lease_alive_check=_exploding_watcher,
                    )
                )

            # R4-04: Run cancellation invalidates lease immediately!
            with dbs["owner"].tx() as c:
                c.execute(
                    "UPDATE agent.runs SET cancel_requested=true WHERE tenant_id=%s AND id=%s",
                    (tenant["tenantId"], created_a["runId"]),
                )
            with pytest.raises(ScopeDenied, match="PREVIEW_RUN_OR_CYCLE_INACTIVE"):
                broker.verify_lease_details(
                    tenant["tenantId"], tenant["userId"], lease_id, token, srv_port, expected_cycle_id=cycle_a
                )
            with dbs["owner"].tx() as c:
                c.execute(
                    "UPDATE agent.runs SET cancel_requested=false WHERE tenant_id=%s AND id=%s",
                    (tenant["tenantId"], created_a["runId"]),
                )

            r_dbg = client.post(
                f"/v1/development/cycles/{cycle_a}/debug",
                headers=headers,
                json={
                    "leaseId": lease_id,
                    "previewToken": token,
                    "targetPort": srv_port,
                    "path": "/",
                    "revokeAfter": False,
                },
            )
            assert r_dbg.status_code == 201, r_dbg.text
            finding = r_dbg.json()["finding"]
            assert finding["status"] == "OPEN"
            assert any("[REDACTED]" in msg for msg in finding["consoleErrors"])
            assert all(
                "super_secret_token_999" not in msg and "devtools" not in msg for msg in finding["consoleErrors"]
            )
            assert any("HTTP 500 /api/broken" in msg for msg in finding["networkErrors"])
            assert any("BLOCKED_EGRESS" in msg for msg in finding["networkErrors"])
            assert any("BLOCKED_REDIRECT" in msg for msg in finding["networkErrors"])
            assert escape_hits == [], f"Sandbox Cell browser worker escaped isolated netns: {escape_hits}"
            assert any("[mobile] horizontal overflow" in msg for msg in finding["layoutIssues"])

            # R4-04: Steering the task (real API goal_revision bump) invalidates active preview lease immediately!
            r_steer_prv = client.post(
                f"/v1/tasks/{created_a['taskId']}/steer",
                headers=headers,
                json={"schemaVersion": "1.0", "goal": "updated preview goal", "expectedGoalRevision": "1"},
            )
            assert r_steer_prv.status_code == 200, r_steer_prv.text
            with pytest.raises(ScopeDenied, match="STALE_GOAL_REVISION"):
                broker.verify_lease_details(
                    tenant["tenantId"], tenant["userId"], lease_id, token, srv_port, expected_cycle_id=cycle_a
                )
            # Revoke lease explicitly -> observe fails 403
            assert (
                client.post(
                    f"/v1/development/previews/{lease_id}/commands",
                    headers=headers,
                    json={"action": "revoke"},
                ).status_code
                == 200
            )
            assert (
                client.post(
                    f"/v1/development/previews/{lease_id}/commands",
                    headers=headers,
                    json={"action": "observe", "previewToken": token, "targetPort": srv_port},
                ).status_code
                == 403
            )
    finally:
        _stop_async_service(tg_loop, tg_th)
        _stop_async_service(sb_loop, sb_th)
        _stop_async_service(prv_loop, prv_th)
        escape_srv.shutdown()
        escape_srv.server_close()


def test_phase7_production_wired_tool_gateway_sentinel_pr_e2e(
    dbs: dict[str, Database], tenant: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero-DI Production-Wired E2E (R2-02..R2-05, R2-08..R2-10, R2-13): REAL Sentinel RPC + EgressProxy + ToolGateway RPC."""
    post_hits: list[dict[str, Any]] = []
    seen_auth_headers: list[str] = []
    # Pre-seed an unrelated pre-existing PR #41 on the same branch/head/title WITHOUT intent correlation marker (R3-07)
    created_prs: list[dict[str, Any]] = [
        {
            "number": 41,
            "html_url": "https://github.test/org/gh-app/pull/41",
            "state": "open",
            "title": "Approved Phase 7 PR",
            "body": "Pre-existing PR without correlation marker",
            "draft": True,
            "mergeable": True,
            "mergeable_state": "clean",
            "head": {"sha": "2" * 40, "ref": "feat/p7-demo"},
            "base": {"ref": "main"},
        }
    ]
    gh_state: dict[str, Any] = {
        "remote_ref_sha": "2" * 40,
        "pr_response_head_sha": "2" * 40,
        "live_pr_head_sha": "2" * 40,
        "post_mode": "drop",
        "reconcile_mismatch": False,
        "reconcile_body_tamper": False,
        "branch_protected": False,
        "status_http_code": 200,
        "check_runs": [{"name": "integration-pytest", "status": "completed", "conclusion": "failure"}],
        "commit_statuses": [{"context": "ci/policy", "state": "success"}],
    }

    async def _gh_receiver(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout=5.0)
            lines = head[:-4].decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            cl = 0
            for ln in lines[1:]:
                k, _, v = ln.partition(":")
                kl = k.lower().strip()
                if kl == "content-length":
                    cl = int(v.strip())
                elif kl == "authorization":
                    seen_auth_headers.append(v.strip())
            raw = await asyncio.wait_for(r.readexactly(cl), timeout=5.0) if cl else b""

            if method == "GET" and path == "/repos/org/gh-app/git/ref/heads/feat/p7-demo":
                resp = json.dumps(
                    {"ref": "refs/heads/feat/p7-demo", "object": {"sha": gh_state["remote_ref_sha"]}}
                ).encode()
                w.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                    % (len(resp), resp)
                )
                await w.drain()
                return

            if method == "POST" and path == "/repos/org/gh-app/pulls":
                body_obj = json.loads(raw.decode("utf-8"))
                assert "head_sha" not in body_obj
                assert "<!-- aiagent-pr-intent:" in str(body_obj.get("body") or "")
                post_hits.append(body_obj)
                pr_num = 41 + len(created_prs)
                pr_obj = {
                    "number": pr_num,
                    "html_url": f"https://github.test/org/gh-app/pull/{pr_num}",
                    "state": "open",
                    "title": body_obj["title"],
                    "body": body_obj["body"],
                    "draft": bool(body_obj["draft"]),
                    "mergeable": True,
                    "mergeable_state": "unstable",
                    "head": {"sha": gh_state["pr_response_head_sha"], "ref": body_obj["head"]},
                    "base": {"ref": body_obj["base"]},
                }
                created_prs.append(pr_obj)
                if gh_state["post_mode"] == "drop":
                    gh_state["post_mode"] = "normal"
                    w.close()
                    return
                resp = json.dumps(pr_obj).encode()
                w.write(
                    b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                    % (len(resp), resp)
                )
                await w.drain()
                return

            if method == "GET" and path.startswith("/repos/org/gh-app/pulls?"):
                items = []
                for p in created_prs:
                    item = dict(p)
                    if gh_state["reconcile_mismatch"]:
                        item["base"] = {"ref": "wrong-base"}
                    if gh_state["reconcile_body_tamper"] and item["number"] >= 42:
                        item["body"] = "Tampered body without marker"
                    items.append(item)
                resp = json.dumps(items).encode()
                w.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                    % (len(resp), resp)
                )
                await w.drain()
                return

            if method == "GET" and path == "/repos/org/gh-app/pulls/42":
                pr_42 = next(p for p in created_prs if p["number"] == 42)
                live_pr = {**pr_42, "head": {"sha": gh_state["live_pr_head_sha"], "ref": "feat/p7-demo"}}
                resp = json.dumps(live_pr).encode()
                w.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                    % (len(resp), resp)
                )
                await w.drain()
                return

            if method == "GET" and path == "/repos/org/gh-app/branches/main":
                resp = json.dumps({"name": "main", "protected": gh_state["branch_protected"]}).encode()
                w.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                    % (len(resp), resp)
                )
                await w.drain()
                return

            if method == "GET" and path == "/repos/org/gh-app/rules/branches/main":
                resp = json.dumps([]).encode()
                w.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                    % (len(resp), resp)
                )
                await w.drain()
                return

            if method == "GET" and path.endswith("/protection/required_status_checks"):
                w.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                await w.drain()
                return

            if method == "GET" and "/check-runs" in path:
                runs = gh_state["check_runs"]
                resp = json.dumps({"total_count": len(runs), "check_runs": runs}).encode()
                w.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                    % (len(resp), resp)
                )
                await w.drain()
                return

            if method == "GET" and path.endswith("/status"):
                if gh_state["status_http_code"] != 200:
                    w.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n")
                    await w.drain()
                    return
                resp = json.dumps({"statuses": gh_state["commit_statuses"]}).encode()
                w.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                    % (len(resp), resp)
                )
                await w.drain()
                return

            w.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            await w.drain()
        finally:
            w.close()

    # 1. Start mock GitHub receiver
    async def _boot_gh(holder: dict[str, Any]) -> None:
        srv = await asyncio.start_server(_gh_receiver, "127.0.0.1", 0)
        holder["port"] = srv.sockets[0].getsockname()[1]

        async def _cleanup() -> None:
            srv.close()
            await srv.wait_closed()

        holder["cleanup"] = _cleanup

    gh_h, gh_loop, gh_th = _start_async_service(_boot_gh)
    gh_port = int(gh_h["port"])
    gh_origin = f"http://api.github.test:{gh_port}"

    # 2. Start REAL Sentinel RPC server + EgressProxy
    sen_rpc_port = _free_tcp_port()
    sen_endpoint = f"tcp:127.0.0.1:{sen_rpc_port}"
    tool_sen_key = secrets.token_bytes(32)
    api_sen_key = secrets.token_bytes(32)

    async def _resolve(host: str, port_num: int) -> list[str]:
        del port_num
        return ["127.0.0.1"] if host == "api.github.test" else []

    ecfg = EgressConfig.from_json(
        json.dumps(
            {
                "destinations": {
                    "github-api": {
                        "scheme": "http",
                        "host": "api.github.test",
                        "port": gh_port,
                        "methods": ["GET", "POST"],
                        "allow_private": ["127.0.0.1"],
                    }
                }
            }
        ),
        production=False,
    )
    sentinel_obj = Sentinel(dbs["sentinel"], ecfg, secrets.token_bytes(32))

    async def _boot_sentinel(holder: dict[str, Any]) -> None:
        rpc_srv = build_sentinel_server(
            {"tool_gateway": tool_sen_key, "api": api_sen_key},
            sen_endpoint,
            sentinel_obj,
        )
        await rpc_srv.start()
        proxy = EgressProxy(sentinel_obj, ecfg, resolver=_resolve)
        p_port = await proxy.start("127.0.0.1", 0)
        holder["proxy_port"] = p_port

        async def _cleanup() -> None:
            await proxy.close()
            await rpc_srv.close()

        holder["cleanup"] = _cleanup

    sen_h, sen_loop, sen_th = _start_async_service(_boot_sentinel)
    proxy_url = f"http://127.0.0.1:{int(sen_h['proxy_port'])}"

    # 3. Start REAL ToolGateway RPC server wired to REAL RpcSentinel + DevGitHubPrExecutor
    tg_rpc_port = _free_tcp_port()
    tg_endpoint = f"tcp:127.0.0.1:{tg_rpc_port}"
    api_tg_key = secrets.token_bytes(32)
    rt_tg_key = secrets.token_bytes(32)

    rpc_sentinel = RpcSentinel(RpcClient(sen_endpoint, "tool_gateway", tool_sen_key))
    gw = ToolGateway(
        dbs["tool_gateway"],
        {"dev_github_pr": DevGitHubPrExecutor(dbs["tool_gateway"], proxy=proxy_url)},
        sentinel=rpc_sentinel,
    )

    async def _boot_tg(holder: dict[str, Any]) -> None:
        tg_srv = build_tool_server({"api": api_tg_key, "runtime": rt_tg_key}, tg_endpoint, gw)
        await tg_srv.start()

        async def _cleanup() -> None:
            await tg_srv.close()

        holder["cleanup"] = _cleanup

    _, tg_loop, tg_th = _start_async_service(_boot_tg)

    # 4. Provision operator repository_binding & binding-scoped GitHub credential (R2-02)
    binding_id = _seed_repository_binding(dbs, tenant, repository="org/gh-app", approved_branch="feat/p7-demo")
    monkeypatch.setenv("AIAGENT_EGRESS_PROXY", proxy_url)
    monkeypatch.setenv("AIAGENT_SENTINEL_ENDPOINT", sen_endpoint)
    monkeypatch.setenv("AIAGENT_API_SENTINEL_RPC_KEY", api_sen_key.hex())
    monkeypatch.setenv("AIAGENT_TOOL_GATEWAY_ENDPOINT", tg_endpoint)
    monkeypatch.setenv("AIAGENT_API_TOOL_RPC_KEY", api_tg_key.hex())
    # Ensure a decoy unscoped AIAGENT_GITHUB_TOKEN is NEVER used; only binding-scoped credential works!
    monkeypatch.setenv("AIAGENT_GITHUB_TOKEN", "decoy_global_token_must_never_be_used")
    monkeypatch.setenv(
        "AIAGENT_GITHUB_BINDING_TOKENS_JSON",
        json.dumps(
            {
                binding_id: {
                    "tenantId": tenant["tenantId"],
                    "workspaceId": tenant["workspaceId"],
                    "repository": "org/gh-app",
                    "apiOrigin": gh_origin,
                    "token": "gh_bound_tok_secret_987",
                }
            }
        ),
    )

    # 5. Create API with ZERO injected service dependencies (R2-04: real production startup path!)
    app = create_app(dbs["api"])
    headers = {"Authorization": f"Bearer {tenant['token']}"}

    try:
        with TestClient(app) as client:
            r_p = client.post(
                "/v1/development/projects",
                headers=headers,
                json={
                    "projectId": "proj-gh-1",
                    "name": "GH App",
                    "repository": "org/gh-app",
                    "defaultBranch": "main",
                    "repositoryBindingId": binding_id,
                    "githubApiBase": gh_origin,
                    "preferences": {"requiredChecks": ["integration-pytest"]},
                },
            )
            assert r_p.status_code == 201, r_p.text

            cycle_id, commit_sha, _, claim, _ = _seed_code_ready_cycle(
                dbs, tenant, project_id="proj-gh-1", repository="org/gh-app", profile="development"
            )

            # R3-01: Direct runtime ToolGateway.submit with wrong headSha or non-CODE_READY cycle MUST be DENIED inside ToolGateway!
            wrong_sha_args = canonical_pr_request(
                project_id="proj-gh-1",
                cycle_id=cycle_id,
                repository="org/gh-app",
                branch="feat/p7-demo",
                head_sha="9" * 40,
                base_branch="main",
                title="Bypass Attempt PR",
                body="Should be denied by ToolGateway",
                draft=True,
            )
            bad_tc_id = f"call-{uuid7()[:10]}"
            with dbs["model_gateway"].tx() as c:
                control.append_head(
                    c,
                    claim,
                    "assistant",
                    {"content": "", "tool_calls": [{"id": bad_tc_id, "name": "dev_github_pr", "args": wrong_sha_args}]},
                    writer="model_gateway",
                    stream="main",
                )
            bad_submit = _run(gw.submit(claim, bad_tc_id, "dev_github_pr", wrong_sha_args))
            assert bad_submit["status"] == "DENIED", bad_submit
            assert bad_submit["result"]["error"] == "CYCLE_COMMIT_OR_GOAL_MISMATCH", bad_submit

            # Propose valid dev_github_pr through REAL ToolGateway.submit (validates TOOLS['dev_github_pr'] + Cycle CODE_READY!)
            pr_args = canonical_pr_request(
                project_id="proj-gh-1",
                cycle_id=cycle_id,
                repository="org/gh-app",
                branch="feat/p7-demo",
                head_sha=commit_sha,
                base_branch="main",
                title="Approved Phase 7 PR",
                body="Exact approved body",
                draft=True,
            )
            intent_id, approval_id = _propose_dev_pr_through_gateway(dbs, gw, claim, pr_args)

            # R2-13: Unapproved request fails 403 WITHOUT consuming/polluting dev_pr_records!
            r_unapp = client.post(
                "/v1/development/projects/proj-gh-1/integrations",
                headers={**headers, "Idempotency-Key": "idem-unapp-before-approve"},
                json={
                    "cycleId": cycle_id,
                    "expectedHeadSha": commit_sha,
                    "intentId": intent_id,
                    "title": "Approved Phase 7 PR",
                    "body": "Exact approved body",
                    "draft": True,
                },
            )
            assert r_unapp.status_code == 403 and len(post_hits) == 0

            # Approve via REAL public API endpoint POST /v1/approvals/{approval_id}/decision!
            r_apr = client.post(
                f"/v1/approvals/{approval_id}/decision",
                headers=headers,
                json={
                    "schemaVersion": "1.0",
                    "decision": "APPROVED",
                    "requestSha256": pr_request_sha256(pr_args),
                    "expectedRecordVersion": "1",
                },
            )
            assert r_apr.status_code == 200, r_apr.text

            # R2-13: Tampered title fails 403 WITHOUT consuming/polluting dev_pr_records (legitimate intent still usable!)
            r_tamper = client.post(
                "/v1/development/projects/proj-gh-1/integrations",
                headers={**headers, "Idempotency-Key": "idem-tamper-must-not-reserve"},
                json={
                    "cycleId": cycle_id,
                    "expectedHeadSha": commit_sha,
                    "intentId": intent_id,
                    "title": "TAMPERED Title",
                    "body": "Exact approved body",
                    "draft": True,
                },
            )
            assert r_tamper.status_code == 403 and len(post_hits) == 0

            # R2-13 + R2-04 + R2-09 + P1-03: Concurrent PR dispatch on the SAME approved intent -> ONLY 1 upstream POST!
            # (Notice request omits requiredChecks -> Project policy ["integration-pytest"] is STILL enforced per R2-08!)
            barrier_pr = threading.Barrier(2)

            def _fire_pr(idx: int) -> tuple[int, dict[str, Any]]:
                barrier_pr.wait(timeout=10)
                with TestClient(app) as c_thr:
                    resp = c_thr.post(
                        "/v1/development/projects/proj-gh-1/integrations",
                        headers={**headers, "Idempotency-Key": f"idem-race-{idx}"},
                        json={
                            "cycleId": cycle_id,
                            "expectedHeadSha": commit_sha,
                            "intentId": intent_id,
                            "title": "Approved Phase 7 PR",
                            "body": "Exact approved body",
                            "draft": True,
                            "maxRepairAttempts": 2,
                        },
                    )
                    return resp.status_code, resp.json()

            with ThreadPoolExecutor(max_workers=2) as pool:
                race_results = list(pool.map(_fire_pr, range(2)))
            codes = sorted(r[0] for r in race_results)
            assert codes == [409, 502], f"Expected one 502 EFFECT_UNKNOWN and one 409 lock refusal, got {race_results}"
            assert len(post_hits) == 1
            pr_rec_id = next(r[1]["prRecord"]["id"] for r in race_results if r[0] == 502)

            # Verify ToolGateway (NOT API) settled canonical tool_intents & side_effect_records to UNKNOWN
            st_row = dbs["owner"].one(
                "SELECT i.status_code, se.outcome_code, se.attempt_fence FROM agent.tool_intents i"
                " JOIN agent.side_effect_records se ON se.tenant_id=i.tenant_id AND se.tool_intent_id=i.id"
                " WHERE i.tenant_id=%s AND i.id=%s",
                (tenant["tenantId"], intent_id),
            )
            assert st_row is not None and st_row["status_code"] == "UNKNOWN" and st_row["outcome_code"] == "UNKNOWN"

            # Re-submitting under a brand new Idempotency-Key refuses blind retry (409, 0 extra POSTs)
            r_retry_diff = client.post(
                "/v1/development/projects/proj-gh-1/integrations",
                headers={**headers, "Idempotency-Key": "idem-brand-new-key"},
                json={
                    "cycleId": cycle_id,
                    "expectedHeadSha": commit_sha,
                    "intentId": intent_id,
                    "title": "Approved Phase 7 PR",
                    "body": "Exact approved body",
                    "draft": True,
                },
            )
            assert r_retry_diff.status_code == 409 and r_retry_diff.json()["reconcileRequired"] is True
            assert len(post_hits) == 1

            # R3-07: Reconcile refuses base.ref mismatch, body/marker tamper, AND NEVER confuses pre-existing PR #41!
            gh_state["reconcile_mismatch"] = True
            r_rec_miss = client.post(f"/v1/development/prs/{pr_rec_id}/reconcile", headers=headers)
            assert r_rec_miss.status_code == 200 and r_rec_miss.json()["reconciled"] is False
            gh_state["reconcile_mismatch"] = False

            gh_state["reconcile_body_tamper"] = True
            r_rec_body = client.post(f"/v1/development/prs/{pr_rec_id}/reconcile", headers=headers)
            assert r_rec_body.status_code == 200 and r_rec_body.json()["reconciled"] is False
            gh_state["reconcile_body_tamper"] = False

            # R2-04 + R3-07: Reconcile via ToolGateway RPC confirms ONLY exact intent-correlated PR #42 (NOT pre-existing #41)!
            r_rec = client.post(f"/v1/development/prs/{pr_rec_id}/reconcile", headers=headers)
            assert r_rec.status_code == 200, r_rec.text
            assert r_rec.json()["reconciled"] is True
            assert r_rec.json()["prRecord"]["effectStatus"] == "CONFIRMED"
            assert r_rec.json()["prRecord"]["prNumber"] == 42

            confirmed_row = dbs["owner"].one(
                "SELECT i.status_code, se.outcome_code, se.external_receipt_ref FROM agent.tool_intents i"
                " JOIN agent.side_effect_records se ON se.tenant_id=i.tenant_id AND se.tool_intent_id=i.id"
                " WHERE i.tenant_id=%s AND i.id=%s",
                (tenant["tenantId"], intent_id),
            )
            assert confirmed_row is not None
            assert confirmed_row["status_code"] == "CONFIRMED"
            assert confirmed_row["outcome_code"] == "RECONCILED_SENT"
            assert confirmed_row["external_receipt_ref"] == f"github-pr:org/gh-app#42:{commit_sha[:12]}:{intent_id[:8]}"

            # R2-04 + P1-05 + P2-10: Guardian check via Sentinel RPC in CONFIRMED state -> 200 OK & 0 repair consumption
            g_ro = client.post(f"/v1/development/prs/{pr_rec_id}/guardian-check", headers=headers, json={})
            assert g_ro.status_code == 200, g_ro.text
            assert g_ro.json()["evaluation"]["ciState"] == "FAIL"
            assert g_ro.json()["prRecord"]["repairAttempts"] == 0

            # P2-10: Concurrent claimRepair=True strictly caps repairAttempts at 2
            barrier_g = threading.Barrier(4)

            def _claim_slot(_: int) -> dict[str, Any]:
                barrier_g.wait(timeout=10)
                with TestClient(app) as c_thr:
                    resp = c_thr.post(
                        f"/v1/development/prs/{pr_rec_id}/guardian-check",
                        headers=headers,
                        json={"claimRepair": True},
                    )
                    assert resp.status_code == 200, resp.text
                    return dict(resp.json())

            with ThreadPoolExecutor(max_workers=4) as pool:
                g_results = list(pool.map(_claim_slot, range(4)))
            assert sum(1 for r in g_results if r["repairClaimed"] is True) == 2
            assert max(r["prRecord"]["repairAttempts"] for r in g_results) == 2

            # R2-08: Policy requiredChecks=["integration-pytest"] is enforced even though PR request omitted it,
            # AND Commit Status API failure (HTTP 500) forces UNKNOWN (never PASS)!
            gh_state["check_runs"] = [{"name": "other-check", "status": "completed", "conclusion": "success"}]
            g_missing = client.post(f"/v1/development/prs/{pr_rec_id}/guardian-check", headers=headers, json={})
            assert g_missing.status_code == 200 and g_missing.json()["evaluation"]["ciState"] == "UNKNOWN"
            assert "integration-pytest" in g_missing.json()["evaluation"]["missingRequiredChecks"]

            gh_state["check_runs"] = [{"name": "integration-pytest", "status": "completed", "conclusion": "success"}]
            gh_state["status_http_code"] = 500
            g_stat_err = client.post(f"/v1/development/prs/{pr_rec_id}/guardian-check", headers=headers, json={})
            assert g_stat_err.status_code == 200 and g_stat_err.json()["evaluation"]["ciState"] == "UNKNOWN"

            gh_state["status_http_code"] = 200
            gh_state["live_pr_head_sha"] = "7" * 40
            g_stale = client.post(f"/v1/development/prs/{pr_rec_id}/guardian-check", headers=headers, json={})
            assert g_stale.status_code == 200 and g_stale.json()["evaluation"]["ciState"] == "STALE_HEAD"

            # R2-10: Sentinel consumption rejects expired approval EVEN AFTER permit was minted!
            exp_args = canonical_pr_request(
                project_id="proj-gh-1",
                cycle_id=cycle_id,
                repository="org/gh-app",
                branch="feat/p7-demo",
                head_sha=commit_sha,
                base_branch="main",
                title="Expiry Test PR",
                body="Body",
                draft=True,
            )
            exp_intent_id, exp_apr_id = _propose_dev_pr_through_gateway(dbs, gw, claim, exp_args)
            r_exp_apr = client.post(
                f"/v1/approvals/{exp_apr_id}/decision",
                headers=headers,
                json={
                    "schemaVersion": "1.0",
                    "decision": "APPROVED",
                    "requestSha256": pr_request_sha256(exp_args),
                    "expectedRecordVersion": "1",
                },
            )
            assert r_exp_apr.status_code == 200, r_exp_apr.text

            # R2-05: Reconcile MUST refuse a FAILED dev_pr_records entry (409) instead of confirming an unattempted PR
            failed_rec_id = uuid7()
            with dbs["owner"].tx() as c:
                c.execute(
                    "INSERT INTO agent.dev_pr_records"
                    " (id, tenant_id, owner_user_id, project_id, cycle_id, repository, branch, head_sha,"
                    "  idempotency_key, intent_id, effect_status)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'FAILED')",
                    (
                        failed_rec_id,
                        tenant["tenantId"],
                        tenant["userId"],
                        "proj-gh-1",
                        cycle_id,
                        "org/gh-app",
                        "feat/p7-demo",
                        commit_sha,
                        "idem-failed-pr-no-reconcile",
                        exp_intent_id,
                    ),
                )
            r_rec_failed = client.post(f"/v1/development/prs/{failed_rec_id}/reconcile", headers=headers)
            assert r_rec_failed.status_code == 409, r_rec_failed.text
            with dbs["owner"].tx() as c:
                c.execute(
                    "UPDATE agent.tool_intents SET status_code='SENDING' WHERE tenant_id=%s AND id=%s",
                    (tenant["tenantId"], exp_intent_id),
                )
            exp_permit = sentinel_obj.authorize(claim, exp_intent_id, "http", "api.github.test", gh_port, "POST")
            # Expire approval BEFORE EgressProxy consumes the permit -> MUST be denied!
            with dbs["owner"].tx() as c:
                c.execute(
                    "UPDATE agent.approval_records SET expires_at = now() - interval '5s' WHERE tenant_id=%s AND id=%s",
                    (tenant["tenantId"], exp_apr_id),
                )
            exp_body = canonical_github_pr_payload(
                exp_args, intent_id=exp_intent_id, request_sha256=pr_request_sha256(exp_args)
            )
            with pytest.raises(ForwardProxyError) as exc_exp:
                _run(
                    send_via_forward_proxy(
                        proxy_url,
                        identity="tool_gateway",
                        permit=exp_permit,
                        method="POST",
                        target_url=f"{gh_origin}/repos/org/gh-app/pulls",
                        host_header=f"api.github.test:{gh_port}",
                        headers={"Content-Type": "application/json"},
                        body=exp_body,
                    )
                )
            assert exc_exp.value.post_send is False and len(post_hits) == 1

            # R2-02: Verify ONLY binding-scoped token was sent (never decoy global token) and never leaked to DB/events
            assert "Bearer gh_bound_tok_secret_987" in seen_auth_headers
            assert all("decoy_global_token" not in h for h in seen_auth_headers)
            leaked_ev = dbs["owner"].one(
                "SELECT count(*) AS n FROM agent.domain_events WHERE tenant_id=%s AND payload_json::text LIKE %s",
                (tenant["tenantId"], "%gh_bound_tok_secret_987%"),
            )
            assert leaked_ev is not None and int(leaked_ev["n"]) == 0
    finally:
        _stop_async_service(tg_loop, tg_th)
        _stop_async_service(sen_loop, sen_th)
        _stop_async_service(gh_loop, gh_th)


def test_phase7_web_ui_development_tab_playwright_e2e(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    """DEV-09 UI E2E: Development tab project creation, 3-candidate Design Picker, and Demo approval dialog in Chromium."""
    playwright_async = pytest.importorskip("playwright.async_api")
    async_playwright = playwright_async.async_playwright
    dist_dir = Path(__file__).resolve().parents[2] / "web" / "dist"
    assert (dist_dir / "index.html").is_file(), "Run `npm run build` in web/ before Playwright E2E"

    store = PostgresDevStore(dbs["api"])
    store.create_project(
        tenant_id=tenant["tenantId"],
        owner_user_id=tenant["userId"],
        workspace_id=tenant["workspaceId"],
        project_id="proj-ui-1",
        name="Seeded UI App",
        repository="org/ui-app",
    )
    cycle_id, _, created_ui, _, _ = _seed_code_ready_cycle(dbs, tenant, project_id="proj-ui-1", repository="org/ui-app")
    store.create_design_proposal(
        tenant_id=tenant["tenantId"],
        owner_user_id=tenant["userId"],
        cycle_id=cycle_id,
        candidates=[
            {
                "candidateId": "cand-a",
                "label": "Modern Clean",
                "fixtureRef": "fixture://v1",
                "summary": "Light card UI",
            },
            {
                "candidateId": "cand-b",
                "label": "Studio Dark",
                "fixtureRef": "fixture://v1",
                "summary": "Dark studio UI",
            },
            {"candidateId": "cand-c", "label": "Classic Pro", "fixtureRef": "fixture://v1", "summary": "Dense data UI"},
        ],
    )
    # R3-08 / R4-05: Seed a real raw Demo render artifact in agent.context_artifacts for cand-b
    ui_render_bytes = b"<html><body>Studio Dark Rendered Preview</body></html>"
    ui_src_art_id, _ = _seed_context_artifact(
        dbs, tenant, task_id=created_ui["taskId"], run_id=created_ui["runId"], raw_bytes=ui_render_bytes, kind="raw"
    )
    store.record_demo_render_artifact(
        tenant_id=tenant["tenantId"],
        owner_user_id=tenant["userId"],
        cycle_id=cycle_id,
        candidate_id="cand-b",
        viewport="1280x800",
        fixture_ref="fixture://v1",
        source_artifact_id=ui_src_art_id,
        render_bytes=ui_render_bytes,
    )

    app = create_app(dbs["api"], web_dist_dir=dist_dir)
    api_port, uv_srv, uv_th, uv_sock = _start_uvicorn_thread(app)
    base_url = f"http://127.0.0.1:{api_port}"

    async def _drive() -> None:
        page_errors: list[str] = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await context.new_page()
            page.on("pageerror", lambda err: page_errors.append(str(err)))
            try:
                await page.goto(base_url, wait_until="networkidle")
                await page.get_by_label("APIトークン").fill(tenant["token"])
                await page.get_by_role("button", name="セッション開始").click()
                await page.get_by_text("AUTHENTICATED").wait_for(timeout=10000)

                await page.get_by_test_id("tab-development").click()
                await page.get_by_test_id("development-panel").wait_for(timeout=10000)

                await page.get_by_label("プロジェクト名").fill("New E2E Project")
                await page.get_by_label("リポジトリ").fill("org/new-e2e-app")
                await page.get_by_role("button", name="プロジェクト登録").click()
                await page.get_by_text("New E2E Project").wait_for(timeout=10000)

                await page.get_by_test_id("dev-project-proj-ui-1").click()
                await page.get_by_test_id(f"dev-cycle-{cycle_id}").click()
                await page.get_by_test_id("design-candidate-cand-b").wait_for(timeout=10000)
                await (
                    page.get_by_test_id("design-candidate-cand-b").get_by_role("button", name="この候補を選択").click()
                )
                await page.get_by_test_id("design-candidate-cand-b").get_by_text("[選択中]").wait_for(timeout=10000)

                await page.get_by_role("button", name="Demo を承認 (APPROVED)").click()
                dlg = page.get_by_test_id("confirm-dialog")
                await dlg.wait_for(timeout=10000)
                await dlg.get_by_text("Tool 承認にはなりません").wait_for(timeout=10000)
                await page.get_by_test_id("confirm-dialog-confirm").click()
                await page.get_by_test_id("dev-demo-status").get_by_text("APPROVED").wait_for(timeout=10000)
                assert page_errors == []
            finally:
                await context.close()
                await browser.close()

    try:
        _run(_drive())
    finally:
        uv_srv.should_exit = True
        uv_th.join(timeout=5)
        uv_sock.close()
