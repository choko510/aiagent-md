"""PostgreSQL + FastAPI SSE + Preview Broker + Multi-Reviewer & Automation integration tests (Slice 3)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from aiagent_automation.campaign import (
    CampaignController,
    CampaignPlan,
    CampaignState,
    FakeClock,
    Rejected,
    ResultState,
    StopPolicy,
    VerificationPort,
)
from aiagent_automation.campaign import (
    Phase as CampPhase,
)
from aiagent_automation.pg_store import PostgresCampaignStore, PostgresQueueDispatch, PostgresWatchStore
from aiagent_automation.watch import DailyWatch
from aiagent_core import admin, control
from aiagent_core.commands import Scope, submit_task
from aiagent_core.db import Database
from aiagent_development import (
    CommitEvidence,
    Cycle,
    DevelopmentWorkflow,
    ExecutionRequest,
    PatchArtifact,
    Phase,
    ProjectScope,
    ReviewFinding,
    ReviewVerdict,
    RunFence,
    StateConflict,
    TaskPlan,
)
from aiagent_development.factory import MultiReviewerRunner, ReviewerCheckSpec
from aiagent_development.mock import MockGitProvider, MockSandbox
from aiagent_development.pg_store import (
    PostgresCycleStore,
    PostgresRunAuthority,
    PostgresWorkspacePort,
    PreviewBroker,
)
from conftest import PgEnv
from fastapi.testclient import TestClient

pytestmark = pytest.mark.pg


class _AllowGateway:
    def require_operation(
        self, scope: ProjectScope, run: RunFence, operation: str, target_sha: str, intent_ref: str
    ) -> None:
        assert operation in ("sandbox.build", "sandbox.test", "sandbox.static-check", "git.commit", "git.push")
        assert target_sha and intent_ref


class _FixedReviewerAuth:
    def assert_verified(self, scope: ProjectScope, builder_run: RunFence, verdict: ReviewVerdict) -> None:
        if verdict.reviewer_task_id == builder_run.task_id:
            raise AssertionError("self-review forbidden")


class _StubGit(MockGitProvider):
    def __init__(self, scope: ProjectScope) -> None:
        self.heads = {(scope.repository, scope.branch, scope.workspace_id): scope.base_sha}
        import threading

        self._lock = threading.RLock()
        self.pushes = []
        self.prs = []

    def commit_patch(
        self, scope: ProjectScope, patch: PatchArtifact, expected_head_sha: str, intent_ref: str, message: str
    ) -> CommitEvidence:
        with self._lock:
            sha = hashlib.sha256((expected_head_sha + patch.diff_sha256 + intent_ref).encode()).hexdigest()[:40]
            self.heads[(scope.repository, scope.branch, scope.workspace_id)] = sha
            return CommitEvidence(
                expected_head_sha, sha, patch.diff_sha256, patch.artifact_ref, "pg-commit:" + intent_ref
            )


def test_development_pg_cycle_repair_review_and_preview_broker(
    pg: PgEnv, dbs: dict[str, Database], tenant: dict[str, str], api: TestClient, tmp_path
) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    created = submit_task(dbs["api"], scope, "autonomous feature factory", profile="sandbox")
    claim = control.claim_run(dbs["runtime"], scope.tenant_id, created["runId"], "dev-worker-1")

    base_sha = "a" * 40
    proj_scope = ProjectScope(
        tenant_id=scope.tenant_id,
        owner_user_id=scope.user_id,
        project_id="proj-1",
        workspace_id=scope.workspace_id,
        repository="org/demo-app",
        branch="feat/factory",
        base_sha=base_sha,
        workspace_revision="rev-1",
    )
    fence = RunFence(
        task_id=claim.task_id,
        run_id=claim.run_id,
        goal_revision=claim.goal_revision,
        lease_epoch=claim.lease_epoch,
        restore_incarnation=claim.restore_incarnation,
    )
    store = PostgresCycleStore(dbs["api"])
    run_auth = PostgresRunAuthority(dbs["api"])
    ws_port = PostgresWorkspacePort(dbs["api"])
    git = _StubGit(proj_scope)
    sandbox = MockSandbox()
    wf = DevelopmentWorkflow(
        runs=run_auth,
        reviewers=_FixedReviewerAuth(),
        workspaces=ws_port,
        gateway=_AllowGateway(),  # type: ignore[arg-type]
        sandbox=sandbox,
        git=git,
        store=store,
    )

    state_holder = {"repaired": False}
    digest_1 = hashlib.sha256(b"patch-v1").hexdigest()
    digest_2 = hashlib.sha256(b"patch-v2").hexdigest()
    toolchain = hashlib.sha256(b"py3.13-cell").hexdigest()

    def build_patch(c: Cycle) -> PatchArtifact:
        return PatchArtifact("art:p1", digest_1, ("app.py",), git.head(proj_scope), "rev-1", c.run)

    def repair_patch(c: Cycle) -> PatchArtifact:
        state_holder["repaired"] = True
        return PatchArtifact("art:p2", digest_2, ("app.py",), git.head(proj_scope), "rev-1", c.run)

    def build_reqs(c: Cycle) -> tuple[ExecutionRequest, ...]:
        req = ExecutionRequest(
            proj_scope,
            c.run,
            c.plan.plan_ref if c.plan else "plan-1",
            "build",
            ("python", "-m", "compileall", "."),
            toolchain,
            f"intent:{c.round_number}:build-1",
        )
        sandbox.results[req.intent_ref] = (True, 0, None)
        return (req,)

    def test_reqs(c: Cycle) -> tuple[ExecutionRequest, ...]:
        assert c.commit is not None
        sandbox.source_sha = c.commit.commit_sha
        req = ExecutionRequest(
            proj_scope,
            c.run,
            c.plan.plan_ref if c.plan else "plan-1",
            "test",
            ("pytest", "-q"),
            toolchain,
            f"intent:{c.round_number}:post:unit-1",
        )
        sandbox.results[req.intent_ref] = (True, 0, "unit-1")
        return (req,)

    def _eval(role: str, c: Cycle) -> ReviewVerdict:
        assert c.commit is not None
        if role == "security" and c.round_number == 1:
            return ReviewVerdict(
                f"rev-{role}",
                role,
                c.commit.commit_sha,
                False,
                "ev:sec:r1",
                (ReviewFinding("F-1", "P1", "OPEN"),),
            )
        return ReviewVerdict(f"rev-{role}", role, c.commit.commit_sha, True, f"ev:{role}:r{c.round_number}")

    runner = MultiReviewerRunner(
        wf,
        tuple(
            ReviewerCheckSpec(role, f"rev-{role}", lambda c, r=role: _eval(r, c))
            for role in ("functional", "security", "ui-e2e")
        ),
    )

    final_cycle = runner.run_bounded_factory(
        cycle_id="cycle-pg-1",
        scope=proj_scope,
        run=fence,
        change_request_ref="cr-1",
        plan=TaskPlan("plan-1", "cr-1", ("unit-1",), claim.goal_revision),
        repo_root=tmp_path,
        build_requests=build_reqs,
        build_patch=build_patch,
        test_requests=test_reqs,
        repair_patch=repair_patch,
        max_repairs=1,
    )
    assert final_cycle.phase == Phase.CODE_READY
    assert final_cycle.round_number == 2 and state_holder["repaired"] is True

    # Optimistic CAS rejects stale version save
    with pytest.raises(StateConflict, match="CAS mismatch|non-monotonic"):
        store.save(final_cycle, final_cycle)

    # PreviewBroker issues SHA-bound ephemeral lease ONLY for CODE_READY cycle
    broker = PreviewBroker(dbs["api"])
    lease_id, raw_tok = broker.issue_lease(final_cycle, target_port=4173, ttl_s=60)
    assert final_cycle.commit is not None
    assert broker.verify_lease(scope.tenant_id, scope.user_id, lease_id, raw_tok, 4173) == final_cycle.commit.commit_sha
    assert broker.revoke_lease(scope.tenant_id, scope.user_id, lease_id) is True

    # Domain events include dev.cycle_committed + dev.preview_leased and stream over SSE with owner isolation
    sse = api.get(
        f"/v1/tasks/{created['taskId']}/events/stream", headers={"Authorization": f"Bearer {tenant['token']}"}
    )
    assert sse.status_code == 200
    assert "text/event-stream" in sse.headers["content-type"]
    assert "dev.cycle_committed" in sse.text and "dev.preview_leased" in sse.text

    other = admin.provision_tenant(pg.owner)
    assert (
        api.get(
            f"/v1/tasks/{created['taskId']}/events/stream", headers={"Authorization": f"Bearer {other['token']}"}
        ).status_code
        == 404
    )


def test_automation_pg_quota_wait_multicycle_steer_watch_and_sse(
    pg: PgEnv, dbs: dict[str, Database], tenant: dict[str, str], api: TestClient
) -> None:
    class _PassVerifier(VerificationPort):
        def passes(self, *, campaign_id: str, cycle_id: str, evidence_refs: list[str], quality_gate_ref: str) -> bool:
            return bool(evidence_refs and quality_gate_ref)

    clock = FakeClock(datetime(2026, 11, 16, 10, 0, tzinfo=UTC))
    store = PostgresCampaignStore(
        dbs["api"], tenant_id=tenant["tenantId"], owner_user_id=tenant["userId"], workspace_id=tenant["workspaceId"]
    )
    q = PostgresQueueDispatch(dbs["api"], owner_user_id=tenant["userId"], workspace_id=tenant["workspaceId"])
    ctrl = CampaignController(store, clock, q, verifier=_PassVerifier())

    plan = CampaignPlan(
        campaign_id="camp-pg-1",
        tenant_id=tenant["tenantId"],
        goal_revision="1",
        policy=StopPolicy(
            max_cycles=5,
            deadline_utc="2026-11-16T18:00:00+00:00",
            max_cost_micro_usd=5000,
            max_tokens=50000,
            max_tool_calls=50,
            max_concurrent_cycles=2,
        ),
        cost_per_cycle=500,
        tokens_per_cycle=5000,
        tools_per_cycle=5,
    )
    ctrl.create(plan, "work-1")
    assert ctrl.dispatch_pending("camp-pg-1") == 1
    assert len(q.created_runs) == 1

    # Multi-cycle concurrent admission (max_concurrent_cycles=2 allows 2 active cycles, rejects 3rd)
    c2_id = ctrl.reserve_parallel_cycle("camp-pg-1", "work-2", expected_control_version=1)
    assert c2_id == "camp-pg-1:2"
    assert ctrl.dispatch_pending("camp-pg-1") == 1
    assert ctrl.admit_cycle("camp-pg-1", "camp-pg-1:1", expected_control_version=1) is True
    assert ctrl.admit_cycle("camp-pg-1", "camp-pg-1:2", expected_control_version=1) is True
    with pytest.raises(Rejected, match="CONCURRENCY_LIMIT_REACHED"):
        ctrl.reserve_parallel_cycle("camp-pg-1", "work-3", expected_control_version=1)

    # QUOTA_WAIT + cooldown gate + recovery
    retry_at = (clock.now() + timedelta(minutes=2)).isoformat()
    v2 = ctrl.pause_quota("camp-pg-1", retry_at, expected_control_version=1)
    assert store.read("camp-pg-1").state == CampaignState.QUOTA_WAIT
    with pytest.raises(Rejected, match="QUOTA_COOLDOWN_ACTIVE"):
        ctrl.resume_quota("camp-pg-1", expected_control_version=v2)
    clock.advance(minutes=3)
    v3 = ctrl.resume_quota("camp-pg-1", expected_control_version=v2)
    assert store.read("camp-pg-1").state == CampaignState.ACTIVE

    # STEER increments control_version and goal_revision while preserving running cycles
    v4 = ctrl.steer("camp-pg-1", new_goal_revision="2", expected_control_version=v3)
    assert store.read("camp-pg-1").plan.goal_revision == "2"

    # Complete cycle 1 cleanly
    ctrl.advance("camp-pg-1", "camp-pg-1:1", CampPhase.PLAN, CampPhase.EXECUTE, expected_control_version=v4)
    ctrl.advance("camp-pg-1", "camp-pg-1:1", CampPhase.EXECUTE, CampPhase.VERIFY, expected_control_version=v4)
    res = ctrl.complete_cycle(
        "camp-pg-1",
        "camp-pg-1:1",
        claimed_success=True,
        evidence_refs=["ev:1"],
        quality_gate_ref="gate:1",
        cost_micro_usd=100,
        tokens=1000,
        tools=1,
        next_work_item_id="work-4",
        expected_control_version=v4,
    )
    assert res == ResultState.VERIFIED

    # Watch atomic bound check in Postgres
    wstore = PostgresWatchStore(dbs["api"], owner_user_id=tenant["userId"], workspace_id=tenant["workspaceId"])
    watch = DailyWatch(
        watch_id="watch-pg-1",
        tenant_id=tenant["tenantId"],
        timezone_id="Asia/Tokyo",
        local_hour=19,
        local_minute=0,
        start_utc="2026-11-16T00:00:00+00:00",
        max_occurrences=1,
        revision=1,
    )
    wstore.register(watch)
    slot_t = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
    assert wstore.claim_bounded(watch, slot_t) is True
    assert wstore.claim_bounded(watch, slot_t) is False

    # Campaign state & SSE endpoint reachable for owner, 404 for another tenant
    camp_sse = api.get("/v1/campaigns/camp-pg-1/events/stream", headers={"Authorization": f"Bearer {tenant['token']}"})
    assert camp_sse.status_code == 200
    assert "quota_wait" in camp_sse.text and "steer" in camp_sse.text
    other = admin.provision_tenant(pg.owner)
    assert (
        api.get(
            "/v1/campaigns/camp-pg-1/events/stream", headers={"Authorization": f"Bearer {other['token']}"}
        ).status_code
        == 404
    )


def test_authority_lease_expiry_and_service_role_rls_enforcement(
    pg: PgEnv, dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    """Verify P1-2 (RunFence expiry/worker_ref/reclaim), P1-3 (service-role RLS), and P2 (Watch owner/revision)."""
    import psycopg
    from aiagent_core.commands import cancel
    from aiagent_development import StaleReference

    scope_core = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    created = submit_task(dbs["api"], scope_core, "dev authority test", profile="sandbox")
    claim1 = control.claim_run(dbs["runtime"], scope_core.tenant_id, created["runId"], "dev-w1")
    dev_scope = ProjectScope(
        tenant_id=scope_core.tenant_id,
        owner_user_id=scope_core.user_id,
        project_id="proj-rls",
        workspace_id=scope_core.workspace_id,
        repository="org/rls-repo",
        branch="feat/rls",
        base_sha="a" * 40,
        workspace_revision="rev-1",
    )
    run_auth = PostgresRunAuthority(dbs["runtime"])
    fence1 = RunFence(
        task_id=claim1.task_id,
        run_id=claim1.run_id,
        goal_revision=claim1.goal_revision,
        lease_epoch=claim1.lease_epoch,
        restore_incarnation=claim1.restore_incarnation,
        worker_ref="dev-w1",
    )
    run_auth.assert_current(dev_scope, fence1)

    # Wrong worker_ref denied
    with pytest.raises(StaleReference, match="stale or inactive"):
        run_auth.assert_current(
            dev_scope,
            RunFence(
                task_id=claim1.task_id,
                run_id=claim1.run_id,
                goal_revision=claim1.goal_revision,
                lease_epoch=claim1.lease_epoch,
                restore_incarnation=claim1.restore_incarnation,
                worker_ref="dev-rogue",
            ),
        )

    # Expired lease BEFORE re-claim must fail closed
    assert control.release(dbs["runtime"], claim1)
    with pytest.raises(StaleReference, match="stale or inactive"):
        run_auth.assert_current(dev_scope, fence1)

    # Re-claim advances lease_epoch; fence2 passes, fence1 remains rejected
    claim2 = control.claim_run(dbs["runtime"], scope_core.tenant_id, created["runId"], "dev-w2")
    fence2 = RunFence(
        task_id=claim2.task_id,
        run_id=claim2.run_id,
        goal_revision=claim2.goal_revision,
        lease_epoch=claim2.lease_epoch,
        restore_incarnation=claim2.restore_incarnation,
        worker_ref="dev-w2",
    )
    with pytest.raises(StaleReference, match="stale or inactive"):
        run_auth.assert_current(dev_scope, fence1)
    run_auth.assert_current(dev_scope, fence2)

    # Service-role DSN RLS enforcement on PR #97 tables: runtime + tool_gateway + relay succeed in own tenant
    runtime_store = PostgresCycleStore(dbs["runtime"])
    cycle = Cycle(
        cycle_id="cycle-rls-1",
        scope=dev_scope,
        run=fence2,
        phase=Phase.CODE_READY,
        change_request_ref="cr-rls",
        commit=CommitEvidence("a" * 40, "b" * 40, "c" * 64, "art:rls", "rec:rls"),
    )
    runtime_store.create(cycle)
    gw_store = PostgresCycleStore(dbs["tool_gateway"])
    loaded = gw_store.load(dev_scope, "cycle-rls-1")
    assert loaded.cycle_id == "cycle-rls-1"

    other = admin.provision_tenant(pg.owner)
    other_scope = ProjectScope(
        tenant_id=other["tenantId"],
        owner_user_id=other["userId"],
        project_id="proj-rls",
        workspace_id=other["workspaceId"],
        repository="org/rls-repo",
        branch="feat/rls",
        base_sha="a" * 40,
        workspace_revision="rev-1",
    )
    # Cross-tenant read via tool_gateway blocked by RLS + scope check
    with pytest.raises(Exception):
        gw_store.load(other_scope, "cycle-rls-1")
    # Cross-tenant write with mismatched GUC blocked by RLS WITH CHECK
    with dbs["tool_gateway"].tx(tenant=other["tenantId"]) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute(
            "INSERT INTO agent.dev_diff_claims(tenant_id, project_id, diff_sha256, cycle_id) VALUES (%s,%s,%s,%s)",
            (tenant["tenantId"], "proj-rls", "d" * 64, "cycle-rls-1"),
        )

    # Relay service role can persist and query automation tables in own tenant, blocked cross-tenant
    relay_camp = PostgresCampaignStore(
        dbs["relay"], tenant_id=tenant["tenantId"], owner_user_id=tenant["userId"], workspace_id=tenant["workspaceId"]
    )
    relay_plan = CampaignPlan(
        campaign_id="camp-rls-1",
        tenant_id=tenant["tenantId"],
        goal_revision="1",
        policy=StopPolicy(2, "2026-11-16T18:00:00+00:00", 1000, 10000, 10),
        cost_per_cycle=100,
        tokens_per_cycle=1000,
        tools_per_cycle=2,
    )

    class _PassVerifier(VerificationPort):
        def passes(self, *, campaign_id: str, cycle_id: str, evidence_refs: list[str], quality_gate_ref: str) -> bool:
            return bool(evidence_refs and quality_gate_ref)

    relay_ctrl = CampaignController(
        relay_camp,
        FakeClock(datetime(2026, 11, 16, 0, 0, tzinfo=UTC)),
        PostgresQueueDispatch(dbs["relay"], owner_user_id=tenant["userId"], workspace_id=tenant["workspaceId"]),
        verifier=_PassVerifier(),
    )
    relay_ctrl.create(relay_plan, "w1")
    assert relay_camp.read("camp-rls-1").plan.campaign_id == "camp-rls-1"
    with dbs["relay"].tx(tenant=other["tenantId"]) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute(
            "INSERT INTO agent.automation_watch_slots(tenant_id, watch_id, slot_iso, dedupe_key, status)"
            " VALUES (%s,%s,%s,%s,'PENDING')",
            (tenant["tenantId"], "w-other", "2026-11-16T00:00:00Z", "dk-x"),
        )

    # Watch owner collision and stale revision checks
    w_owner1 = PostgresWatchStore(dbs["relay"], owner_user_id=tenant["userId"], workspace_id=tenant["workspaceId"])
    w_owner2 = PostgresWatchStore(dbs["relay"], owner_user_id=other["userId"], workspace_id=tenant["workspaceId"])
    watch1 = DailyWatch(
        watch_id="watch-rls-1",
        tenant_id=tenant["tenantId"],
        timezone_id="Asia/Tokyo",
        local_hour=9,
        local_minute=0,
        start_utc="2026-11-16T00:00:00+00:00",
        max_occurrences=2,
        revision=2,
    )
    w_owner1.register(watch1)
    with pytest.raises(Rejected, match="WATCH_OWNER_OR_REVISION_CONFLICT"):
        w_owner2.register(watch1)
    # Downgrade revision rejected
    stale_watch = DailyWatch(
        watch_id="watch-rls-1",
        tenant_id=tenant["tenantId"],
        timezone_id="Asia/Tokyo",
        local_hour=9,
        local_minute=0,
        start_utc="2026-11-16T00:00:00+00:00",
        max_occurrences=2,
        revision=1,
    )
    with pytest.raises(Rejected, match="WATCH_OWNER_OR_REVISION_CONFLICT"):
        w_owner1.register(stale_watch)
    # Claim with mismatched revision returns False
    assert (
        w_owner1.claim_bounded(
            stale_watch,
            datetime(2026, 11, 16, 0, 0, tzinfo=UTC),
        )
        is False
    )

    # STOP/cancel invalidates RunFence immediately
    cancel(dbs["api"], scope_core, claim2.task_id)
    with pytest.raises(StaleReference, match="stale or inactive"):
        run_auth.assert_current(dev_scope, fence2)

    # Restore incarnation bump also invalidates RunFence on isolated DB (do not mutate session pg)
    from conftest import drop_database, make_database

    iso_env = make_database("aiagent_dev_restore")
    try:
        iso_t = admin.provision_tenant(iso_env.owner)
        iso_scope = Scope(iso_t["tenantId"], iso_t["userId"], iso_t["workspaceId"])
        iso_api = iso_env.db("api", max_size=2)
        iso_rt = iso_env.db("runtime", max_size=2)
        try:
            created3 = submit_task(iso_api, iso_scope, "dev restore test", profile="sandbox")
            claim3 = control.claim_run(iso_rt, iso_scope.tenant_id, created3["runId"], "dev-w3")
            iso_dev_scope = ProjectScope(
                tenant_id=iso_scope.tenant_id,
                owner_user_id=iso_scope.user_id,
                project_id="proj-rls",
                workspace_id=iso_scope.workspace_id,
                repository="org/rls-repo",
                branch="feat/rls",
                base_sha="a" * 40,
                workspace_revision="rev-1",
            )
            fence3 = RunFence(
                task_id=claim3.task_id,
                run_id=claim3.run_id,
                goal_revision=claim3.goal_revision,
                lease_epoch=claim3.lease_epoch,
                restore_incarnation=claim3.restore_incarnation,
                worker_ref="dev-w3",
            )
            iso_auth = PostgresRunAuthority(iso_rt)
            iso_auth.assert_current(iso_dev_scope, fence3)
            admin.restore_bump(iso_env.owner)
            with pytest.raises(StaleReference, match="restore incarnation mismatch"):
                iso_auth.assert_current(iso_dev_scope, fence3)
        finally:
            iso_api.close()
            iso_rt.close()
    finally:
        drop_database(iso_env.name)
