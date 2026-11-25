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
    EffectUnknown,
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


def test_development_pg_rejects_unattested_mock_git_and_preview(
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
            test_id="unit-1",
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

    # This legacy fixture intentionally uses a fake Git provider. The real PG
    # store must refuse its unattested result rather than creating CODE_READY.
    with pytest.raises(EffectUnknown, match="could not be fenced"):
        runner.run_bounded_factory(
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
    final_cycle = store.load(proj_scope, "cycle-pg-1")
    assert final_cycle.phase == Phase.UNKNOWN_EXTERNAL_EFFECT
    assert final_cycle.commit is None and state_holder["repaired"] is False

    # Optimistic CAS rejects stale version save
    with pytest.raises(StateConflict, match="CAS mismatch|non-monotonic"):
        store.save(final_cycle, final_cycle)

    # Unattested Git cannot unlock the PreviewBroker.
    broker = PreviewBroker(dbs["api"])
    with pytest.raises(StateConflict, match="CODE_READY"):
        broker.issue_lease(final_cycle, target_port=4173, ttl_s=60)

    # No committed or preview event is emitted for the fake Git response.
    sse = api.get(
        f"/v1/tasks/{created['taskId']}/events/stream", headers={"Authorization": f"Bearer {tenant['token']}"}
    )
    assert sse.status_code == 200
    assert "text/event-stream" in sse.headers["content-type"]
    assert "dev.cycle_committed" not in sse.text and "dev.preview_leased" not in sse.text

    other = admin.provision_tenant(pg.owner)
    assert (
        api.get(
            f"/v1/tasks/{created['taskId']}/events/stream", headers={"Authorization": f"Bearer {other['token']}"}
        ).status_code
        == 404
    )


def test_preview_broker_lease_with_synthetic_code_ready_fixture(
    dbs: dict[str, Database], tenant: dict[str, str], api: TestClient
) -> None:
    """Exercise PreviewBroker in isolation; this fixture is not Git or Workflow evidence."""
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    created = submit_task(dbs["api"], scope, "preview broker fixture", profile="sandbox")
    claim = control.claim_run(dbs["runtime"], scope.tenant_id, created["runId"], "preview-fixture")
    project = ProjectScope(
        scope.tenant_id,
        scope.user_id,
        "preview-fixture",
        scope.workspace_id,
        "org/fixture",
        "feat/fixture",
        "a" * 40,
        "1",
    )
    run = RunFence(
        claim.task_id, claim.run_id, claim.goal_revision, claim.lease_epoch, claim.restore_incarnation, claim.worker_ref
    )
    fixture = Cycle(
        "preview-fixture",
        project,
        run,
        Phase.CODE_READY,
        "fixture",
        commit=CommitEvidence("a" * 40, "b" * 40, "c" * 64, "synthetic", "synthetic"),
    )
    from aiagent_development.store import PostgresDevStore

    PostgresDevStore(dbs["api"]).create_project(
        tenant_id=scope.tenant_id,
        owner_user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        project_id="preview-fixture",
        name="Preview Fixture",
        repository="org/fixture",
        default_branch="feat/fixture",
    )
    PostgresCycleStore(dbs["api"]).create(fixture)
    broker = PreviewBroker(dbs["api"])
    broker.register_instance(fixture, target_port=4173, process_attestation_sha256="d" * 64)
    lease, token = broker.issue_lease(fixture, target_port=4173, ttl_s=60)
    assert (
        broker.verify_lease(scope.tenant_id, scope.user_id, lease, token, 4173, expected_cycle_id="preview-fixture")
        == "b" * 40
    )
    assert broker.revoke_lease(scope.tenant_id, scope.user_id, lease) is True
    sse = api.get(
        f"/v1/tasks/{created['taskId']}/events/stream", headers={"Authorization": f"Bearer {tenant['token']}"}
    )
    assert sse.status_code == 200 and "dev.preview_leased" in sse.text


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


def test_campaign_goal_propagates_to_worker_deliverable(
    stack, pg: PgEnv, dbs: dict[str, Database], api: TestClient
) -> None:
    """P1-6 E2E: Campaign goal_text, work item, budget, and STEER instructions execute in child Worker."""
    from helpers import approve, state

    t = admin.provision_tenant(pg.owner)
    n0 = len(stack.sends())

    class _PassVerifier(VerificationPort):
        def passes(self, *, campaign_id: str, cycle_id: str, evidence_refs: list[str], quality_gate_ref: str) -> bool:
            return bool(evidence_refs and quality_gate_ref)

    store = PostgresCampaignStore(
        dbs["relay"], tenant_id=t["tenantId"], owner_user_id=t["userId"], workspace_id=t["workspaceId"]
    )
    q = PostgresQueueDispatch(dbs["relay"], owner_user_id=t["userId"], workspace_id=t["workspaceId"])
    ctrl = CampaignController(store, FakeClock(datetime(2026, 11, 16, 10, 0, tzinfo=UTC)), q, verifier=_PassVerifier())
    plan = CampaignPlan(
        campaign_id="camp-e2e-goal",
        tenant_id=t["tenantId"],
        goal_revision="1",
        policy=StopPolicy(
            max_cycles=4,
            deadline_utc="2026-11-16T18:00:00+00:00",
            max_cost_micro_usd=5000,
            max_tokens=50000,
            max_tool_calls=50,
            max_concurrent_cycles=1,
        ),
        cost_per_cycle=250,
        tokens_per_cycle=20000,
        tools_per_cycle=7,
        goal_text="Campaign default goal",
        work_item_goals=(("item-notify", "Find the widget price and notify alice."),),
    )
    ctrl.create(plan, "item-notify")
    assert ctrl.dispatch_pending("camp-e2e-goal") == 1
    assert len(q.created_runs) == 1
    child = q.created_runs[0]

    # DB task_goal_revisions & runs reflect real Campaign goal, work item, budget, and deadline
    with dbs["owner"].conn() as c:
        row = c.execute(
            "SELECT g.goal_text, r.token_budget, r.max_tool_calls"
            " FROM agent.task_goal_revisions g"
            " JOIN agent.runs r ON r.tenant_id = g.tenant_id AND r.task_id = g.task_id"
            " WHERE g.tenant_id=%s AND g.task_id=%s AND g.revision=1",
            (t["tenantId"], child["taskId"]),
        ).fetchone()
    assert row is not None
    assert "Find the widget price and notify alice." in row["goal_text"]
    assert "work_item=item-notify" in row["goal_text"]
    assert "tokens_budget=20000" in row["goal_text"]
    assert row["token_budget"] == 20000 and row["max_tool_calls"] == 7

    # Real Worker executes the spawned Campaign child Task end-to-end
    r1 = stack.worker()
    assert r1.returncode == 0, r1.stderr[-2000:]
    st1 = state(api, t, child["taskId"])
    assert st1["state"] == "WAITING_INPUT" and st1["run"]["state"] == "WAITING_APPROVAL", st1
    approve(api, t, child["taskId"])
    r2 = stack.worker()
    assert r2.returncode == 0, r2.stderr[-2000:]
    st2 = state(api, t, child["taskId"])
    assert st2["state"] == "SUCCEEDED" and st2["run"]["result"] == "done:CONFIRMED", st2
    sent = stack.sends()[n0:]
    assert len(sent) == 1 and sent[0]["body"] == {"to": "alice", "body": "price=42"}

    # STEER updates Campaign goal_text & revision; subsequent cycle carries new goal into child Task
    v2 = ctrl.steer(
        "camp-e2e-goal",
        new_goal_revision="2",
        expected_control_version=1,
        new_goal_text="Check DB connection pool status.",
    )
    ctrl.admit_cycle("camp-e2e-goal", "camp-e2e-goal:1", expected_control_version=v2)
    ctrl.advance("camp-e2e-goal", "camp-e2e-goal:1", CampPhase.PLAN, CampPhase.EXECUTE, expected_control_version=v2)
    ctrl.advance("camp-e2e-goal", "camp-e2e-goal:1", CampPhase.EXECUTE, CampPhase.VERIFY, expected_control_version=v2)
    ctrl.complete_cycle(
        "camp-e2e-goal",
        "camp-e2e-goal:1",
        claimed_success=True,
        evidence_refs=["ev:done"],
        quality_gate_ref="gate:pass",
        cost_micro_usd=250,
        tokens=3000,
        tools=2,
        next_work_item_id="item-pool-check",
        expected_control_version=v2,
    )
    assert ctrl.dispatch_pending("camp-e2e-goal") >= 1
    second_child = q.created_runs[-1]
    assert second_child["taskId"] != child["taskId"]
    with dbs["owner"].conn() as c:
        row2 = c.execute(
            "SELECT goal_text FROM agent.task_goal_revisions WHERE tenant_id=%s AND task_id=%s AND revision=1",
            (t["tenantId"], second_child["taskId"]),
        ).fetchone()
    assert row2 is not None
    assert "Check DB connection pool status." in row2["goal_text"]
    assert "work_item=item-pool-check" in row2["goal_text"]
    assert "goal_rev=2" in row2["goal_text"]


def test_campaign_tool_budget_enforced_atomically_by_tool_gateway(pg: PgEnv, dbs: dict[str, Database]) -> None:
    """P1-1: Dedicated runs.max_tool_calls is enforced inside ToolGateway before exceeding Campaign tools_per_cycle."""
    import asyncio

    import psycopg
    from aiagent_gateways.executors import MockSearchExecutor
    from aiagent_gateways.tool_gateway import ToolGateway

    t = admin.provision_tenant(pg.owner)
    store = PostgresCampaignStore(
        dbs["relay"], tenant_id=t["tenantId"], owner_user_id=t["userId"], workspace_id=t["workspaceId"]
    )
    q = PostgresQueueDispatch(dbs["relay"], owner_user_id=t["userId"], workspace_id=t["workspaceId"])

    class _PassVerifier(VerificationPort):
        def passes(self, *, campaign_id: str, cycle_id: str, evidence_refs: list[str], quality_gate_ref: str) -> bool:
            return bool(evidence_refs and quality_gate_ref)

    ctrl = CampaignController(store, FakeClock(datetime(2026, 11, 16, 10, 0, tzinfo=UTC)), q, verifier=_PassVerifier())
    plan = CampaignPlan(
        campaign_id="camp-tool-cap",
        tenant_id=t["tenantId"],
        goal_revision="1",
        policy=StopPolicy(
            max_cycles=2,
            deadline_utc="2026-11-16T18:00:00+00:00",
            max_cost_micro_usd=2000,
            max_tokens=20000,
            max_tool_calls=10,
        ),
        cost_per_cycle=200,
        tokens_per_cycle=5000,
        tools_per_cycle=2,
        goal_text="Cap tool calls per cycle to 2.",
    )
    ctrl.create(plan, "work-cap")
    assert ctrl.dispatch_pending("camp-tool-cap") == 1
    child = q.created_runs[0]

    # Least-privilege runtime cannot raise runs.max_tool_calls
    with dbs["runtime"].conn() as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute(
            "UPDATE agent.runs SET max_tool_calls = 999 WHERE tenant_id=%s AND id=%s", (t["tenantId"], child["runId"])
        )

    claim = control.claim_run(dbs["runtime"], t["tenantId"], child["runId"], "w-cap")
    gw = ToolGateway(dbs["tool_gateway"], {"mock_search": MockSearchExecutor()}, max_tool_calls=32)

    async def _submit_search(idx: int) -> dict:
        call_id = f"call-cap-{idx}"
        args = {"query": f"q{idx}"}
        with dbs["model_gateway"].tx() as mc:
            control.append_head(
                mc,
                claim,
                "assistant",
                {"kind": "assistant", "content": "", "tool_calls": [{"id": call_id, "name": "search", "args": args}]},
                writer="model_gateway",
            )
        return await gw.submit(claim, call_id, "search", args)

    out1 = asyncio.run(_submit_search(1))
    out2 = asyncio.run(_submit_search(2))
    out3 = asyncio.run(_submit_search(3))
    assert out1["status"] == "DONE"
    assert out2["status"] == "DONE"
    assert out3["status"] == "BUDGET_EXHAUSTED"
    assert out3["result"] == {"error": "tool_budget_exceeded"}

    # 3rd denied attempt must NOT have created a 3rd active tool_intent
    n_intents = dbs["owner"].one(
        "SELECT count(*) AS n FROM agent.tool_intents WHERE tenant_id=%s AND run_id=%s AND status_code <> 'BUDGET_EXHAUSTED'",
        (t["tenantId"], claim.run_id),
    )
    assert n_intents is not None and n_intents["n"] == 2
