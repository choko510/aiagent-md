"""Phase 7 end-to-end & security boundary integration tests (DEV-01..DEV-14 on PostgreSQL 16 + Sentinel + Chromium).

Covers:
1. Dev Projects, 3-candidate Design Picker, Demo Approval CAS & separation from external approval,
   PostgresReviewAttestationPort independence, and cross-owner isolation (DEV-01/02/03/07/09).
2. PreviewBroker short-lived loopback lease + observe/synthetic_interact/revoke + real Playwright
   AutonomousBrowserDebugger (console secret redaction, HTTP 500 capture, mobile overflow, zero CDP) (DEV-06).
3. Sentinel EgressProxy-enforced GitHub PR creation, post-send disconnect -> EFFECT_UNKNOWN ->
   idempotent refusal -> read-only Reconcile -> CONFIRMED, and PR Guardian finite repair ceiling (DEV-05/11).
4. Real Playwright Chromium UI E2E against built React Web UI `Development` tab served same-origin.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from aiagent_api.app import create_app
from aiagent_core import admin, commands, control
from aiagent_core.commands import Scope
from aiagent_core.db import Database
from aiagent_development.contracts import (
    CommitEvidence,
    Cycle,
    ExecutionEvidence,
    Phase,
    ProjectScope,
    ReviewFinding,
    ReviewVerdict,
    RunFence,
    ScopeDenied,
    TaskPlan,
)
from aiagent_development.pg_store import PostgresCycleStore
from aiagent_development.store import PostgresDevStore, PostgresReviewAttestationPort
from aiagent_domain import sha256_hex, uuid7
from aiagent_gateways.sentinel_port import LocalSentinel
from aiagent_sentinel.authority import EgressConfig, Sentinel
from aiagent_sentinel.egress import EgressProxy
from fastapi.testclient import TestClient
from psycopg.errors import InsufficientPrivilege

pytestmark = pytest.mark.pg


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
) -> tuple[str, str, dict[str, str], control.Claim]:
    """Seed a real CODE_READY cycle with 3 independent reviewer verdicts for API/Preview/PR tests."""
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    created = commands.submit_task(dbs["api"], scope, "phase7 build task", profile="sandbox")
    claim = control.claim_run(dbs["runtime"], scope.tenant_id, created["runId"], "dev-worker-p7")

    rev_tasks = [
        commands.submit_task(dbs["api"], scope, f"review-{role}", profile="sandbox")["taskId"]
        for role in ("functional", "security", "ui-e2e")
    ]
    base_sha = "1" * 40
    commit_sha = "2" * 40
    diff_sha = sha256_hex({"diff": "phase7"})
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
        commit=CommitEvidence(base_sha, commit_sha, diff_sha, "art-diff-1", "git:receipt-1"),
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
                source_commit_sha=commit_sha,
                test_id="test_main",
                receipt_ref="rcpt-t1",
                timed_out=False,
                truncated=False,
            ),
        ),
        reviews=(
            ReviewVerdict(rev_tasks[0], "functional", commit_sha, True, "ev-func"),
            ReviewVerdict(rev_tasks[1], "security", commit_sha, True, "ev-sec"),
            ReviewVerdict(rev_tasks[2], "ui-e2e", commit_sha, True, "ev-ui"),
        ),
    )
    PostgresCycleStore(dbs["api"]).create(cycle)
    return cid, commit_sha, created, claim


def test_phase7_projects_design_demo_reviews_and_owner_isolation(
    pg: Any, dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    """DEV-01/02/03/07/09: project prefs, deprecated demoChoice=AUTO rejection, CAS design/demo, review independence, RLS."""
    other = admin.provision_tenant(pg.owner)
    app = create_app(dbs["api"])
    h_a = {"Authorization": f"Bearer {tenant['token']}"}
    h_b = {"Authorization": f"Bearer {other['token']}"}

    # DB role isolation: sentinel/relay must NOT have access to new dev_* tables
    with pytest.raises(InsufficientPrivilege):
        dbs["sentinel"].one("SELECT id FROM agent.dev_projects LIMIT 1")
    with pytest.raises(InsufficientPrivilege):
        dbs["relay"].one("SELECT id FROM agent.dev_pr_records LIMIT 1")

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

        cycle_id, commit_sha, created, claim = _seed_code_ready_cycle(dbs, tenant, project_id=proj_id)

        # PostgresReviewAttestationPort rejects self-review and cross-owner reviewer task
        attest = PostgresReviewAttestationPort(dbs["api"])
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
                proj_scope, builder_fence, ReviewVerdict(claim.task_id, "functional", commit_sha, True, "ev-1")
            )
        other_task = commands.submit_task(
            dbs["api"], Scope(other["tenantId"], other["userId"], other["workspaceId"]), "other-review"
        )["taskId"]
        with pytest.raises(ScopeDenied):
            attest.assert_verified(
                proj_scope, builder_fence, ReviewVerdict(other_task, "functional", commit_sha, True, "ev-1")
            )
        indep_task = commands.submit_task(
            dbs["api"], Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"]), "indep-review"
        )["taskId"]
        attest.assert_verified(
            proj_scope,
            builder_fence,
            ReviewVerdict(indep_task, "security", commit_sha, True, "ev-sec", (ReviewFinding("f1", "P2", "RESOLVED"),)),
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
        digest = des["demoSnapshotDigest"]

        # Approving Demo before selecting a candidate fails 409
        r_premature = client.post(
            f"/v1/development/cycles/{cycle_id}/demo-decision",
            headers=h_a,
            json={"designRevision": 1, "decision": "APPROVED", "demoSnapshotDigest": digest},
        )
        assert r_premature.status_code == 409

        # Stale designRevision rejected 409
        r_stale = client.post(
            f"/v1/development/cycles/{cycle_id}/design-decision",
            headers=h_a,
            json={"designRevision": 99, "selectedCandidateId": "minimal"},
        )
        assert r_stale.status_code == 409

        # Valid design selection + Demo approval (explicitly grantsExternalApproval=False)
        r_sel = client.post(
            f"/v1/development/cycles/{cycle_id}/design-decision",
            headers=h_a,
            json={"designRevision": 1, "selectedCandidateId": "minimal", "feedback": "Good readability"},
        )
        assert r_sel.status_code == 200 and r_sel.json()["design"]["selectedCandidateId"] == "minimal"

        # Mismatched snapshot digest rejected 409
        assert (
            client.post(
                f"/v1/development/cycles/{cycle_id}/demo-decision",
                headers=h_a,
                json={"designRevision": 1, "decision": "APPROVED", "demoSnapshotDigest": "0" * 64},
            ).status_code
            == 409
        )

        r_demo = client.post(
            f"/v1/development/cycles/{cycle_id}/demo-decision",
            headers=h_a,
            json={"designRevision": 1, "decision": "APPROVED", "demoSnapshotDigest": digest},
        )
        assert r_demo.status_code == 200
        assert r_demo.json()["design"]["demoDecision"] == "APPROVED"
        assert r_demo.json()["grantsExternalApproval"] is False


def test_phase7_preview_lease_browser_debugger_and_cdp_redaction(
    dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    """DEV-06: Preview observe vs synthetic_interact, real Chromium debug probe redacting secrets & catching 500 + mobile overflow."""
    pytest.importorskip("playwright.async_api")

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
            html = (
                b"<!doctype html><html><body style='margin:0'>"
                b"<div style='width:900px;height:40px'>Wide mobile overflow</div>"
                b"<script>"
                b"console.error('Auth failed Bearer super_secret_token_999 ws://127.0.0.1:9222/devtools/page/abc');"
                b"fetch('/api/broken');"
                b"</script></body></html>"
            )
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
            cycle_id, _, _, _ = _seed_code_ready_cycle(dbs, tenant, project_id="proj-dbg-1", repository="org/dbg-app")

            r_lease = client.post(
                f"/v1/development/cycles/{cycle_id}/previews",
                headers=headers,
                json={"targetPort": srv_port, "ttlSeconds": 120},
            )
            assert r_lease.status_code == 201, r_lease.text
            lease_id = r_lease.json()["leaseId"]
            token = r_lease.json()["previewToken"]

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
                json={"action": "observe", "previewToken": token, "targetPort": srv_port},
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
                },
            )
            assert r_syn.status_code == 200 and r_syn.json()["syntheticFixtureOnly"] is True

            # Real Playwright debug probe captures redacted console error, HTTP 500, and mobile overflow, then auto-revokes lease
            r_dbg = client.post(
                f"/v1/development/cycles/{cycle_id}/debug",
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


def test_phase7_sentinel_github_pr_effect_unknown_reconcile_and_guardian_limit(
    dbs: dict[str, Database], tenant: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEV-05 / DEV-11: Sentinel EgressProxy PR creation, post-send drop -> EFFECT_UNKNOWN -> reconcile, and finite PR Guardian cap."""
    post_hits: list[dict[str, Any]] = []
    created_prs: list[dict[str, Any]] = []
    drop_next_post = {"enabled": True}

    async def _gh_receiver(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout=5.0)
            lines = head[:-4].decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            cl = 0
            for ln in lines[1:]:
                k, _, v = ln.partition(":")
                if k.lower().strip() == "content-length":
                    cl = int(v.strip())
            raw = await asyncio.wait_for(r.readexactly(cl), timeout=5.0) if cl else b""
            if method == "POST" and path == "/repos/org/gh-app/pulls":
                body_obj = json.loads(raw.decode("utf-8"))
                post_hits.append(body_obj)
                pr_obj = {
                    "number": 42,
                    "html_url": "https://github.test/org/gh-app/pull/42",
                    "state": "open",
                    "draft": True,
                    "mergeable": True,
                    "mergeable_state": "unstable",
                    "head": {"sha": body_obj["head_sha"], "ref": body_obj["head"]},
                }
                created_prs.append(pr_obj)
                if drop_next_post["enabled"]:
                    # Simulate ACK loss AFTER PR was created upstream -> must become EFFECT_UNKNOWN!
                    drop_next_post["enabled"] = False
                    w.close()
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
                resp = json.dumps(created_prs[0]).encode("utf-8")
                w.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                    % (len(resp), resp)
                )
                await w.drain()
                return
            if method == "GET" and path.endswith("/check-runs"):
                resp = json.dumps(
                    {"check_runs": [{"name": "integration-pytest", "status": "completed", "conclusion": "failure"}]}
                ).encode("utf-8")
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

    app = create_app(dbs["api"], sentinel=LocalSentinel(sentinel), egress_proxy=proxy_url)
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
            cycle_id, commit_sha, created, _ = _seed_code_ready_cycle(
                dbs, tenant, project_id="proj-gh-1", repository="org/gh-app"
            )

            # Seed canonical tool_intents + APPROVED approval_records for dev_github_pr
            intent_id, apr_id = uuid7(), uuid7()
            unapproved_intent_id = uuid7()
            with dbs["owner"].tx() as c:
                c.execute(
                    "INSERT INTO agent.tool_intents(tenant_id, id, task_id, run_id, tool_key, invocation_key,"
                    " request_sha256, safe_arguments_json, risk_class, goal_revision, status_code)"
                    " VALUES (%s,%s,%s,%s,'dev_github_pr','pr-inv-1',repeat('b',64),'{}','external_irreversible',1,'SENDING'),"
                    " (%s,%s,%s,%s,'dev_github_pr','pr-inv-unapp',repeat('c',64),'{}','external_irreversible',1,'SENDING')",
                    (
                        tenant["tenantId"],
                        intent_id,
                        created["taskId"],
                        created["runId"],
                        tenant["tenantId"],
                        unapproved_intent_id,
                        created["taskId"],
                        created["runId"],
                    ),
                )
                c.execute(
                    "INSERT INTO agent.approval_records(tenant_id, id, tool_intent_id, task_id, run_id, owner_user_id,"
                    " tool_key, request_sha256, goal_revision, safe_summary, decision_code, expires_at)"
                    " VALUES (%s,%s,%s,%s,%s,%s,'dev_github_pr',repeat('b',64),1,'create draft PR','APPROVED',now() + interval '1h')",
                    (tenant["tenantId"], apr_id, intent_id, created["taskId"], created["runId"], tenant["userId"]),
                )

            # 1. Unapproved tool intent is strictly denied 403 (zero outbound POSTs)
            r_unapp = client.post(
                "/v1/development/projects/proj-gh-1/integrations",
                headers={**headers, "Idempotency-Key": "idem-unapp-1"},
                json={"cycleId": cycle_id, "expectedHeadSha": commit_sha, "intentId": unapproved_intent_id},
            )
            assert r_unapp.status_code == 403 and len(post_hits) == 0

            # 2. Approved intent hits post-send disconnect -> 502 EFFECT_UNKNOWN
            r_drop = client.post(
                "/v1/development/projects/proj-gh-1/integrations",
                headers={**headers, "Idempotency-Key": "idem-pr-42"},
                json={
                    "cycleId": cycle_id,
                    "expectedHeadSha": commit_sha,
                    "intentId": intent_id,
                    "maxRepairAttempts": 2,
                },
            )
            assert r_drop.status_code == 502, r_drop.text
            assert r_drop.json()["error"] == "EFFECT_UNKNOWN"
            pr_rec_id = r_drop.json()["prRecord"]["id"]
            assert len(post_hits) == 1

            # 3. Re-submitting same Idempotency-Key refuses blind retry (409, zero extra POSTs)
            r_retry = client.post(
                "/v1/development/projects/proj-gh-1/integrations",
                headers={**headers, "Idempotency-Key": "idem-pr-42"},
                json={"cycleId": cycle_id, "expectedHeadSha": commit_sha, "intentId": intent_id},
            )
            assert r_retry.status_code == 409 and r_retry.json()["reconcileRequired"] is True
            assert len(post_hits) == 1

            # 4. Read-only Sentinel reconcile finds PR #42 and settles record to CONFIRMED
            r_rec = client.post(f"/v1/development/prs/{pr_rec_id}/reconcile", headers=headers)
            assert r_rec.status_code == 200, r_rec.text
            assert r_rec.json()["reconciled"] is True
            assert r_rec.json()["prRecord"]["effectStatus"] == "CONFIRMED"
            assert r_rec.json()["prRecord"]["prNumber"] == 42

            # 5. DEV-11 PR Guardian enforces finite repair ceiling (maxRepairAttempts=2)
            g1 = client.post(f"/v1/development/prs/{pr_rec_id}/guardian-check", headers=headers)
            assert g1.status_code == 200 and g1.json()["evaluation"]["ciState"] == "FAIL"
            assert g1.json()["prRecord"]["repairAttempts"] == 1

            g2 = client.post(f"/v1/development/prs/{pr_rec_id}/guardian-check", headers=headers)
            assert g2.status_code == 200 and g2.json()["evaluation"]["ciState"] == "FAIL"
            assert g2.json()["prRecord"]["repairAttempts"] == 2

            g3 = client.post(f"/v1/development/prs/{pr_rec_id}/guardian-check", headers=headers)
            assert g3.status_code == 200 and g3.json()["evaluation"]["ciState"] == "ESCALATED_LIMIT_REACHED"
            assert g3.json()["evaluation"]["escalated"] is True
            assert g3.json()["evaluation"]["shouldRepair"] is False
            assert g3.json()["prRecord"]["repairAttempts"] == 2
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
    cycle_id, _, _, _ = _seed_code_ready_cycle(dbs, tenant, project_id="proj-ui-1", repository="org/ui-app")
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
