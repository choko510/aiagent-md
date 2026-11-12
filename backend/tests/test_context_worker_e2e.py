"""Normal Runtime Worker + Model Gateway + owner-granted Context path, on real PostgreSQL.

Every test here drives the formal stack: API -> Task/Run -> Outbox -> QueuePort ->
Runtime Worker process -> Model Gateway process -> provider (Fake by default, or the
local Provider-compatible test server). The provider-compatible server is NOT the
real Anthropic service: no credential and no paid traffic is used.

Context is enabled per task by the authenticated owner (policy + Memory consent);
an environment variable can never enable it.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from aiagent_context.models import Scope, SourceInput
from aiagent_core import admin, control, memory_writer
from aiagent_core.db import Database
from conftest import PgEnv
from fastapi.testclient import TestClient
from helpers import auth, state, submit
from stack import Stack

pytestmark = pytest.mark.pg


@contextmanager
def _database(dsn: str) -> Iterator[Database]:
    db = Database(dsn)
    try:
        yield db
    finally:
        db.close()


def _claim_run(pg: PgEnv, tenant_id: str, run_id: str, worker: str) -> control.Claim:
    with _database(pg.dsn["runtime"]) as db:
        return control.claim_run(db, tenant_id, run_id, worker)


def _release_claim(pg: PgEnv, claim: control.Claim) -> None:
    db = Database(pg.dsn["runtime"])
    try:
        control.release(db, claim)
    finally:
        db.close()


def _context_ready(api: TestClient, t: dict[str, str], task_id: str, *, recipient: str, model: str) -> tuple[str, str]:
    """Owner writes an encrypted source + projection and enables the Context path."""
    r = api.post(
        f"/v1/tasks/{task_id}/memory/source",
        headers=auth(t),
        content=b"the owner asked to remember the widget price is 42",
    )
    assert r.status_code == 201, r.text
    source_ref = r.json()["artifactId"]
    r = api.post(f"/v1/tasks/{task_id}/memory/source", headers=auth(t), content=b"remembered data")
    assert r.status_code == 201, r.text
    projection_ref = r.json()["artifactId"]
    api.post(f"/v1/memory/consents/{recipient}", headers=auth(t))
    r = api.put(
        f"/v1/tasks/{task_id}/context-policy",
        headers=auth(t),
        json={"schemaVersion": "1.0", "recipient": recipient, "model": model, "enabled": True},
    )
    assert r.status_code == 200, r.text
    return source_ref, projection_ref


def _create_memory(
    api: TestClient,
    t: dict[str, str],
    task_id: str,
    source_ref: str,
    projection_ref: str,
    *,
    recipient: str = "fake",
) -> str:
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    later = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 86400))
    r = api.post(
        f"/v1/tasks/{task_id}/memory",
        headers=auth(t),
        json={
            "schemaVersion": "1.0",
            "sourceArtifactRef": source_ref,
            "projectionArtifactRef": projection_ref,
            "recipient": recipient,
            "consentBasis": "user_explicit",
            "observedAt": now,
            "expiresAt": later,
        },
    )
    assert r.status_code == 201, r.text
    return str(r.json()["memoryId"])


def _publish_context(
    pg: PgEnv,
    t: dict[str, str],
    task_id: str,
    run_id: str,
    claim: control.Claim,
    goal: str,
    *,
    recipient: str = "fake",
    model: str = "fake-gateway",
) -> str:
    """Publish a Context HEAD the way the trusted internal service does (same claim-bound path)."""
    from aiagent_context import Anchor, AnchorKind, ContextRequest, ContextService, ModelBudget
    from aiagent_gateways.context_artifacts import PostgresArtifactStore
    from aiagent_gateways.context_head import PostgresHeadPort
    from aiagent_gateways.context_memory import PostgresMemoryRepository

    dbs = {
        "model_gateway": Database(pg.dsn["model_gateway"], max_size=4),
        "owner": Database(pg.owner, max_size=4),
    }
    try:
        store = PostgresArtifactStore(
            dbs["model_gateway"], claim, b"k" * 32, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        repo = PostgresMemoryRepository(dbs["model_gateway"], claim, store)
        scope = Scope(claim.tenant_id, claim.owner_user_id, claim.task_id, claim.run_id)
        memory = repo.retrieval(scope, query="remembered", recipient=recipient)
        head = PostgresHeadPort(dbs["model_gateway"], claim, store, repo)
        revision = dbs["owner"].one("SELECT head_revision AS h FROM agent.runs WHERE id=%s", (run_id,))["h"]
        req = ContextRequest(
            scope,
            str(revision),
            str(claim.lease_epoch),
            str(claim.goal_revision),
            head.policy_version(scope),
            memory.source_epoch,
            "cursor:1",
            (Anchor(AnchorKind.GOAL_REF, f"goal:{claim.goal_revision}", goal),),
            ModelBudget(model, "r1", "byte-upper-bound", 10000, 1000, 100),
            memory.projections,
            recipient,
        )
        plan = ContextService(store, head).compact(
            req, (SourceInput("evidence", "2026-01-01T00:00:00Z", b"document evidence", locator_ref="bytes:0"),)
        )
        return plan.committed_head
    finally:
        for db in dbs.values():
            db.close()


def test_normal_worker_uses_owner_granted_context(stack: Stack, api: TestClient, pg: PgEnv) -> None:
    t = admin.provision_tenant(pg.owner)
    goal = "remember my preferences"
    task = submit(api, t, goal)
    st = state(api, t, task)
    run_id = st["run"]["id"]
    claim = _claim_run(pg, t["tenantId"], run_id, "context-setup-worker")
    source_ref, projection_ref = _context_ready(api, t, task, recipient="fake", model="fake-gateway")
    memory_id = _create_memory(api, t, task, source_ref, projection_ref)
    head = _publish_context(pg, t, task, run_id, claim, goal)
    # Hand the run back to the normal worker (the setup claim is not a second execution path).
    _release_claim(pg, claim)

    n0 = len(stack.provider_requests())
    runs = stack.drive()
    st = state(api, t, task)
    assert st["state"] == "SUCCEEDED", (st, [x.stderr[-800:] for x in runs])
    assert st["run"]["headRevision"] == str(int(head) + 1)
    with _database(pg.owner) as db, db.conn() as c:
        dispatch = c.execute("SELECT status_code FROM agent.context_dispatches WHERE run_id=%s", (run_id,)).fetchall()
        assert [d["status_code"] for d in dispatch] == ["COMPLETED"]
        attempt = c.execute(
            "SELECT provider_key,status_code FROM agent.model_attempts WHERE run_id=%s", (run_id,)
        ).fetchall()
        assert attempt[-1]["provider_key"] == "fake" and attempt[-1]["status_code"] == "COMPLETED"
        manifest = c.execute(
            "SELECT checkpoint_json FROM agent.run_checkpoints WHERE run_id=%s AND entry_kind='summary'"
            " ORDER BY checkpoint_revision DESC LIMIT 1",
            (run_id,),
        ).fetchone()["checkpoint_json"]
        assert manifest["memory"] == [{"id": memory_id, "revision": 1}]
        assert "remembered data" not in str(manifest)
        # the answer was produced from the Context payload, not the ordinary goal prompt
        ctx = c.execute(
            "SELECT count(*) AS n FROM agent.model_attempts WHERE run_id=%s AND admitted", (run_id,)
        ).fetchone()["n"]
        assert ctx >= 1
    assert len(stack.provider_requests()) == n0  # Fake provider is in-process, never HTTP


def test_context_policy_off_keeps_step3_path(stack: Stack, api: TestClient, pg: PgEnv) -> None:
    t = admin.provision_tenant(pg.owner)
    task = submit(api, t, "plain STEP 3 run")
    r = stack.worker()
    assert r.returncode == 0, r.stderr[-3000:]
    st = state(api, t, task)
    assert st["state"] == "SUCCEEDED"
    with _database(pg.owner) as db, db.conn() as c:
        assert (
            c.execute(
                "SELECT count(*) AS n FROM agent.context_dispatches d JOIN agent.runs r"
                " ON r.tenant_id=d.tenant_id AND r.id=d.run_id WHERE r.task_id=%s",
                (task,),
            ).fetchone()["n"]
            == 0
        )


def test_memory_consent_withdrawal_disables_context_without_sending(stack: Stack, api: TestClient, pg: PgEnv) -> None:
    t = admin.provision_tenant(pg.owner)
    goal = "consent withdrawal"
    task = submit(api, t, goal)
    st = state(api, t, task)
    run_id = st["run"]["id"]
    runtime_db = Database(pg.dsn["runtime"])
    try:
        claim = control.claim_run(runtime_db, t["tenantId"], run_id, "consent-worker")
    finally:
        runtime_db.close()
    source_ref, projection_ref = _context_ready(api, t, task, recipient="fake", model="fake-gateway")
    _create_memory(api, t, task, source_ref, projection_ref)
    head = _publish_context(pg, t, task, run_id, claim, goal)
    _release_claim(pg, claim)
    r = api.delete("/v1/memory/consents/fake", headers=auth(t))
    assert r.status_code == 200, r.text
    n0 = len(stack.provider_requests())
    r = stack.worker()
    assert r.returncode == 0, r.stderr[-3000:]
    with _database(pg.owner) as db, db.conn() as c:
        assert (
            c.execute("SELECT count(*) AS n FROM agent.context_dispatches WHERE run_id=%s", (run_id,)).fetchone()["n"]
            == 0
        )
        assert (
            c.execute(
                "SELECT count(*) AS n FROM agent.model_attempts WHERE run_id=%s AND provider_key IS NOT NULL",
                (run_id,),
            ).fetchone()["n"]
            >= 1
        )
        # The Context HEAD remains append-only evidence; the ordinary path answered instead.
        assert (
            c.execute(
                "SELECT count(*) AS n FROM agent.run_checkpoints WHERE run_id=%s AND checkpoint_revision=%s",
                (run_id, int(head)),
            ).fetchone()["n"]
            == 1
        )
        assert state(api, t, task)["state"] == "SUCCEEDED"
    assert len(stack.provider_requests()) == n0


@pytest.mark.parametrize("change", ["memory_revoked", "head_moved", "lease_lost", "goal_changed"])
def test_stale_context_before_send_never_reaches_provider(
    change: str, stack: Stack, api: TestClient, pg: PgEnv
) -> None:
    t = admin.provision_tenant(pg.owner)
    goal = f"stale context {change}"
    task = submit(api, t, goal)
    st = state(api, t, task)
    run_id = st["run"]["id"]
    runtime_db = Database(pg.dsn["runtime"])
    try:
        claim = control.claim_run(runtime_db, t["tenantId"], run_id, "stale-worker")
    finally:
        runtime_db.close()
    source_ref, projection_ref = _context_ready(api, t, task, recipient="fake", model="fake-gateway")
    memory_id = _create_memory(api, t, task, source_ref, projection_ref)
    _publish_context(pg, t, task, run_id, claim, goal)
    if change == "memory_revoked":
        api.delete(f"/v1/tasks/{task}/memory/{memory_id}", headers=auth(t))
    elif change == "head_moved":
        with _database(pg.dsn["model_gateway"]) as mgw, mgw.tx() as c:
            control.append_head(c, claim, "summary", {"kind": "marker"}, writer="test")
    elif change == "lease_lost":
        owner_db = Database(pg.owner)
        try:
            owner_db.one(
                "UPDATE agent.runs SET lease_expires_at=now()-interval '1 second' WHERE id=%s RETURNING id",
                (run_id,),
            )
        finally:
            owner_db.close()
    else:
        r = api.post(
            f"/v1/tasks/{task}/steer",
            headers=auth(t),
            json={"schemaVersion": "1.0", "goal": "changed goal", "expectedGoalRevision": "1"},
        )
        assert r.status_code == 200, r.text
    n0 = len(stack.provider_requests())
    stack.worker()
    with _database(pg.owner) as db, db.conn() as c:
        dispatches = c.execute(
            "SELECT status_code FROM agent.context_dispatches WHERE run_id=%s ORDER BY created_at", (run_id,)
        ).fetchall()
        if change == "lease_lost":
            # The next normal worker has a fresh, current claim. It may resume the
            # same committed Context HEAD; the previous lease is not reused.
            assert [d["status_code"] for d in dispatches] == ["COMPLETED"]
        else:
            assert dispatches == []
    assert len(stack.provider_requests()) == n0


def test_cross_owner_cannot_touch_memory(stack: Stack, api: TestClient, pg: PgEnv) -> None:
    t = admin.provision_tenant(pg.owner)
    other = admin.add_user(pg.owner, t["tenantId"])
    task = submit(api, t, "owner isolation")
    # No Worker claim is needed for owner-isolation API checks.
    source_ref, projection_ref = _context_ready(api, t, task, recipient="fake", model="fake-gateway")
    memory_id = _create_memory(api, t, task, source_ref, projection_ref)
    for method, path in (
        ("get", f"/v1/tasks/{task}/memory"),
        ("get", f"/v1/tasks/{task}/memory/{memory_id}/revisions"),
        ("delete", f"/v1/tasks/{task}/memory/{memory_id}"),
        ("get", f"/v1/tasks/{task}/context-policy"),
    ):
        r = getattr(api, method)(path, headers=auth(other))
        assert r.status_code == 404, (method, path, r.status_code, r.text)
    r = api.patch(
        f"/v1/tasks/{task}/memory/{memory_id}",
        headers=auth(other),
        json={"schemaVersion": "1.0", "expectedRevision": "1", "recipient": "x"},
    )
    assert r.status_code == 404
    r = api.post("/v1/memory/consents/fake-placeholder", headers=auth(other))
    assert r.status_code == 201  # consent is per-user, never per-task of another owner
    # The original owner's consent is unaffected.
    assert api.get(f"/v1/tasks/{task}/memory", headers=auth(t)).status_code == 200


def test_memory_revision_history_and_update(stack: Stack, api: TestClient, pg: PgEnv) -> None:
    t = admin.provision_tenant(pg.owner)
    task = submit(api, t, "history")
    source_ref, projection_ref = _context_ready(api, t, task, recipient="fake", model="fake-gateway")
    memory_id = _create_memory(api, t, task, source_ref, projection_ref)
    r = api.patch(
        f"/v1/tasks/{task}/memory/{memory_id}",
        headers=auth(t),
        json={"schemaVersion": "1.0", "expectedRevision": "1", "recipient": "other-recipient"},
    )
    assert r.status_code == 200 and r.json()["revision"] == "2", r.text
    r = api.patch(
        f"/v1/tasks/{task}/memory/{memory_id}",
        headers=auth(t),
        json={"schemaVersion": "1.0", "expectedRevision": "1", "recipient": "stale"},
    )
    assert r.status_code == 409 and r.json()["error"] == "MEMORY_REVISION_CONFLICT"
    api.delete(f"/v1/tasks/{task}/memory/{memory_id}", headers=auth(t))
    rows = api.get(f"/v1/tasks/{task}/memory/{memory_id}/revisions", headers=auth(t)).json()["items"]
    assert [x["changeKind"] for x in rows] == ["created", "updated", "revoked"]
    assert [x["revision"] for x in rows] == [1, 2, 3]
    assert all(x["actorUserId"] == t["userId"] for x in rows)
    api.delete("/v1/memory/consents/fake", headers=auth(t))
    consent_rows = api.get("/v1/memory/consents/fake/revisions", headers=auth(t)).json()["items"]
    assert [x["changeKind"] for x in consent_rows] == ["granted", "revoked"]
    assert [x["revision"] for x in consent_rows] == [1, 2]
    assert all(x["actorUserId"] == t["userId"] for x in consent_rows)
    with _database(pg.owner) as db, db.conn() as c:
        audit = c.execute(
            "SELECT action_code FROM agent.audit_records WHERE tenant_id=%s AND target_ref=%s ORDER BY recorded_at",
            (t["tenantId"], memory_id),
        ).fetchall()
        assert [a["action_code"] for a in audit] == ["memory.created", "memory.updated", "memory.revoked"]


def test_context_unknown_barrier_survives_gateway_restart(stack: Stack, api: TestClient, pg: PgEnv) -> None:
    t = admin.provision_tenant(pg.owner)
    goal = "unknown barrier"
    task = submit(api, t, goal)
    st = state(api, t, task)
    run_id = st["run"]["id"]
    runtime_db = Database(pg.dsn["runtime"])
    try:
        claim = control.claim_run(runtime_db, t["tenantId"], run_id, "barrier-worker")
    finally:
        runtime_db.close()
    source_ref, projection_ref = _context_ready(api, t, task, recipient="fake", model="fake-gateway")
    _create_memory(api, t, task, source_ref, projection_ref)
    head = _publish_context(pg, t, task, run_id, claim, goal)
    _release_claim(pg, claim)
    stack.restart("model", AIAGENT_FAULT="context_after_admit")
    try:
        stack.worker()
    finally:
        stack.restart("model")
    with _database(pg.owner) as db, db.conn() as c:
        dispatch = c.execute("SELECT status_code FROM agent.context_dispatches WHERE run_id=%s", (run_id,)).fetchall()
        assert [d["status_code"] for d in dispatch] == ["ADMITTED"]
    n0 = len(stack.provider_requests())
    stack.worker()
    with _database(pg.owner) as db, db.conn() as c:
        assert (
            c.execute("SELECT count(*) AS n FROM agent.context_dispatches WHERE run_id=%s", (run_id,)).fetchone()["n"]
            == 1
        )
        assert (
            c.execute(
                "SELECT count(*) AS n FROM agent.model_attempts WHERE run_id=%s AND admitted", (run_id,)
            ).fetchone()["n"]
            == 1
        )
    assert len(stack.provider_requests()) == n0
    assert head


def test_restore_requires_reconciliation_before_memory(stack: Stack, api: TestClient, pg: PgEnv, tmp_path) -> None:
    from aiagent_core import memory_tombstones

    t = admin.provision_tenant(pg.owner)
    task = submit(api, t, "restore reconciliation")
    run_id = state(api, t, task)["run"]["id"]
    source_ref, projection_ref = _context_ready(api, t, task, recipient="fake", model="fake-gateway")
    memory_id = _create_memory(api, t, task, source_ref, projection_ref)
    log = memory_tombstones.TombstoneLog(tmp_path / "tombstones.jsonl", integrity_key=b"t" * 32)
    log.initialize()
    with _database(pg.dsn["api"]) as api_db:
        memory_writer.revoke_memory(api_db, _scope_of(t), task_id=task, memory_id=memory_id, tombstone_log=log)
    admin.restore_bump(pg.owner)
    admin.adopt_runs_after_restore(pg.owner)
    # Emulate PITR: the restored snapshot predates the revocation (the row is active
    # again). The guard trigger is disabled only for this owner-side simulation,
    # in one transaction, exactly like restoring older bytes would do.
    with _database(pg.owner) as db, db.tx() as c:
        c.execute("ALTER TABLE agent.memory_records DISABLE TRIGGER memory_record_guard")
        c.execute("UPDATE agent.memory_records SET revoked_at=NULL, revision=1 WHERE id=%s", (memory_id,))
        c.execute("ALTER TABLE agent.memory_records ENABLE TRIGGER memory_record_guard")
    # Even metadata listing/history stays closed because it would report a
    # potentially resurrected active record; direct revocation remains available.
    listing = api.get(f"/v1/tasks/{task}/memory", headers=auth(t))
    assert listing.status_code == 409 and listing.json()["error"] == "MEMORY_RESTORE_RECONCILIATION_REQUIRED"
    from aiagent_gateways.context_artifacts import PostgresArtifactStore
    from aiagent_gateways.context_memory import PostgresMemoryRepository

    claim = _claim_run(pg, t["tenantId"], run_id, "restore-worker")
    with _database(pg.dsn["model_gateway"]) as mgw:
        store = PostgresArtifactStore(
            mgw,
            claim,
            b"k" * 32,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        repo = PostgresMemoryRepository(mgw, claim, store)
        with pytest.raises(Exception, match="MEMORY_RESTORE_RECONCILIATION_REQUIRED|context claim denied"):
            repo.retrieval(
                Scope(claim.tenant_id, claim.owner_user_id, claim.task_id, claim.run_id),
                query="remembered",
                recipient="fake",
            )
    from aiagent_core.memory_restore import reconcile_owner

    result = reconcile_owner(pg.owner, log, reconciled_by="operator-test")
    assert result.replayed >= 1, result
    # After reconciliation, the earlier revocation is still enforced (it is not resurrected).
    rows = api.get(f"/v1/tasks/{task}/memory", headers=auth(t)).json()["items"]
    assert [r["id"] for r in rows] == [memory_id] and rows[0]["revoked"] is True


def _scope_of(t: dict[str, str]):
    from aiagent_core.commands import Scope

    return Scope(t["tenantId"], t["userId"], t["workspaceId"])


def _prepare_anthropic_context(
    api: TestClient,
    pg: PgEnv,
    goal: str,
    *,
    budget: int | None = None,
) -> tuple[dict[str, str], str, str, str]:
    t = admin.provision_tenant(pg.owner)
    task = submit(api, t, goal, profile="anthropic")
    run_id = state(api, t, task)["run"]["id"]
    claim = _claim_run(pg, t["tenantId"], run_id, "provider-context-setup")
    source_ref, projection_ref = _context_ready(api, t, task, recipient="anthropic", model="claude-sonnet-5-5")
    _create_memory(api, t, task, source_ref, projection_ref, recipient="anthropic")
    head = _publish_context(
        pg,
        t,
        task,
        run_id,
        claim,
        goal,
        recipient="anthropic",
        model="claude-sonnet-5-5",
    )
    _release_claim(pg, claim)
    if budget is not None:
        with _database(pg.owner) as owner:
            owner.one("UPDATE agent.runs SET token_budget=%s WHERE id=%s RETURNING id", (budget, run_id))
    return t, task, run_id, head


def test_normal_worker_context_through_anthropic_compatible_server(stack: Stack, api: TestClient, pg: PgEnv) -> None:
    """The normal worker uses the real AnthropicProvider adapter against a local protocol stub.

    This proves HTTP proxy + Messages SSE parsing + usage/HEAD commit, not real
    Anthropic connectivity or production isolation. It performs no paid traffic.
    """
    goal = "provider compatible Context run"
    t, task, run_id, head = _prepare_anthropic_context(api, pg, goal)
    # The active Memory revision is referenced in the published manifest.
    memories = api.get(f"/v1/tasks/{task}/memory", headers=auth(t)).json()["items"]
    memory_id = memories[0]["id"]
    stack.enable_anthropic_route(t["tenantId"])

    before = len(stack.provider_requests())
    worker_runs = stack.drive(AIAGENT_MODEL="claude-sonnet-5-5")
    result = state(api, t, task)
    assert result["state"] == "SUCCEEDED", (result, [r.stderr[-1200:] for r in worker_runs])
    requests = stack.provider_requests()[before:]
    assert len(requests) == 1, requests
    request = requests[0]
    assert request["model"] == "claude-sonnet-5-5" and request["stream"] is True
    assert request["has_system"] and request["has_api_key"]
    assert not request["proxy_authorization_forwarded"]
    assert request["chars"] > 0

    with _database(pg.owner) as owner, owner.conn() as c:
        dispatch = c.execute("SELECT status_code FROM agent.context_dispatches WHERE run_id=%s", (run_id,)).fetchall()
        assert [r["status_code"] for r in dispatch] == ["COMPLETED"]
        attempt = c.execute(
            "SELECT provider_key,status_code,input_tokens,output_tokens,usage_quality,provider_attempts"
            " FROM agent.model_attempts WHERE run_id=%s ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        assert attempt["provider_key"] == "anthropic" and attempt["status_code"] == "COMPLETED"
        assert attempt["input_tokens"] == 11 and attempt["output_tokens"] > 0
        assert attempt["usage_quality"] == "MEASURED" and attempt["provider_attempts"] == 1
        usage = c.execute(
            "SELECT provider_key,input_units,output_units FROM agent.cost_usage_records"
            " WHERE run_id=%s ORDER BY measured_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        assert usage["provider_key"] == "anthropic"
        assert usage["input_units"] == 11 and usage["output_units"] == attempt["output_tokens"]
        latest = c.execute(
            "SELECT checkpoint_json FROM agent.run_checkpoints WHERE run_id=%s AND entry_kind='summary'"
            " ORDER BY checkpoint_revision DESC LIMIT 1",
            (run_id,),
        ).fetchone()["checkpoint_json"]
        assert latest["memory"] == [{"id": memory_id, "revision": 1}]
        assert "remembered data" not in str(latest)
        assert control.verify_head_chain(c, t["tenantId"], run_id)

    # Protocol stub logs metadata only; neither original Memory text nor provider key
    # is retained in its log or the gateway's public HEAD.
    raw_log = stack.provider_log_path.read_text(encoding="utf-8") if stack.provider_log_path else ""
    assert "remembered data" not in raw_log
    assert stack.provider_secret not in raw_log
    sentinel_log = (stack.tmp / "sentinel.log").read_text(encoding="utf-8", errors="replace")
    assert "egress forwarded" in sentinel_log and "model_gateway" in sentinel_log
    assert stack.provider_secret not in sentinel_log
    assert "remembered data" not in sentinel_log


@pytest.mark.parametrize("failure", ["egress_denied", "partial_disconnect"])
def test_provider_failure_is_unknown_and_never_blindly_retried(
    failure: str, stack: Stack, api: TestClient, pg: PgEnv
) -> None:
    t, task, run_id, _ = _prepare_anthropic_context(api, pg, f"provider failure {failure}")
    stack.enable_anthropic_route(t["tenantId"])
    before = len(stack.provider_requests())
    if failure == "partial_disconnect":
        stack.provider_mode = "hang"
        stack.restart("provider")
    else:
        # Corrupt the caller's static proxy credential; the provider-compatible
        # server must receive no request when Sentinel Egress denies the route.
        stack.restart("model", AIAGENT_EGRESS_MODEL_TOKEN="bad-egress-token")
    try:
        stack.drive(AIAGENT_MODEL="claude-sonnet-5-5")
    finally:
        stack.restart("model")
        if failure == "partial_disconnect":
            stack.provider_mode = "ok"
            stack.restart("provider")
    requests = stack.provider_requests()[before:]
    if failure == "egress_denied":
        assert requests == []
    else:
        # The server accepted exactly one request then disconnected before
        # message_stop. Incomplete SSE must not be treated as a valid answer.
        assert len(requests) == 1
    with _database(pg.owner) as owner, owner.conn() as c:
        dispatch = c.execute("SELECT status_code FROM agent.context_dispatches WHERE run_id=%s", (run_id,)).fetchall()
        assert [d["status_code"] for d in dispatch] in (["UNKNOWN"], ["ADMITTED"])
        attempts = c.execute(
            "SELECT status_code FROM agent.model_attempts WHERE run_id=%s AND admitted ORDER BY created_at",
            (run_id,),
        ).fetchall()
        assert len(attempts) == 1
        assert attempts[0]["status_code"] in ("FAILED", "RESERVED")
    # A follow-up worker after Gateway restart observes the unresolved barrier and
    # cannot resend, even though an earlier network result was unknown.
    count_before_retry = len(stack.provider_requests())
    stack.drive(AIAGENT_MODEL="claude-sonnet-5-5")
    assert len(stack.provider_requests()) == count_before_retry


def test_provider_model_budget_denies_before_external_send(stack: Stack, api: TestClient, pg: PgEnv) -> None:
    t, task, run_id, _ = _prepare_anthropic_context(api, pg, "provider budget denial", budget=0)
    stack.enable_anthropic_route(t["tenantId"])
    before = len(stack.provider_requests())
    worker_runs = stack.drive(AIAGENT_MODEL="claude-sonnet-5-5")
    assert state(api, t, task)["state"] == "BUDGET_EXHAUSTED", [r.stderr[-1200:] for r in worker_runs]
    assert len(stack.provider_requests()) == before
    with _database(pg.owner) as owner, owner.conn() as c:
        assert (
            c.execute("SELECT count(*) AS n FROM agent.context_dispatches WHERE run_id=%s", (run_id,)).fetchone()["n"]
            == 0
        )
        attempts = c.execute(
            "SELECT reason_code,admitted FROM agent.model_attempts WHERE run_id=%s", (run_id,)
        ).fetchall()
        assert attempts and all(not a["admitted"] and a["reason_code"] == "BUDGET_EXHAUSTED" for a in attempts)


def test_normal_model_complete_unknown_attempt_is_not_resent(stack: Stack, api: TestClient, pg: PgEnv) -> None:
    """The ordinary model.complete worker path also fences UNKNOWN provider attempts."""
    t = admin.provision_tenant(pg.owner)
    task = submit(api, t, "ordinary provider request", profile="anthropic")
    stack.enable_anthropic_route(t["tenantId"])
    stack.provider_mode = "hang"
    stack.restart("provider")
    before = len(stack.provider_requests())
    try:
        worker_runs = stack.drive(AIAGENT_MODEL="claude-sonnet-5-5")
        result = state(api, t, task)
        assert result["state"] == "WAITING_RECONCILIATION", (
            result,
            [r.stderr[-1200:] for r in worker_runs],
        )
        assert len(stack.provider_requests()) - before == 1
        run_id = result["run"]["id"]
        with _database(pg.owner) as owner, owner.conn() as c:
            attempts = c.execute(
                "SELECT status_code,usage_quality,reason_code FROM agent.model_attempts"
                " WHERE run_id=%s ORDER BY created_at",
                (run_id,),
            ).fetchall()
            assert len(attempts) >= 1
            assert attempts[0]["status_code"] == "FAILED" and attempts[0]["usage_quality"] == "UNKNOWN"
            if len(attempts) > 1:
                assert all(a["reason_code"] == "MODEL_OUTCOME_UNKNOWN" for a in attempts[1:])
            unknown_usage = c.execute(
                "SELECT usage_kind,input_units,output_units FROM agent.cost_usage_records WHERE run_id=%s",
                (run_id,),
            ).fetchall()
            assert [u["usage_kind"] for u in unknown_usage] == ["UNKNOWN"]
            assert unknown_usage[0]["input_units"] is None and unknown_usage[0]["output_units"] is None
        # Explicit redelivery/restart cannot send the same unresolved request again.
        count_before_retry = len(stack.provider_requests())
        stack.drive(AIAGENT_MODEL="claude-sonnet-5-5")
        assert len(stack.provider_requests()) == count_before_retry
    finally:
        stack.provider_mode = "ok"
        stack.restart("provider")


def test_revoked_recipient_consent_survives_database_restore(api: TestClient, pg: PgEnv, tmp_path) -> None:
    from aiagent_core import memory_tombstones
    from aiagent_core.memory_restore import reconcile_owner

    t = admin.provision_tenant(pg.owner)
    scope = _scope_of(t)
    with _database(pg.dsn["api"]) as api_db:
        assert memory_writer.grant_memory_consent(api_db, scope, recipient="anthropic") == 1
    log = memory_tombstones.TombstoneLog(tmp_path / "consent-tombstones.jsonl", integrity_key=b"c" * 32)
    log.initialize()
    with _database(pg.dsn["api"]) as api_db:
        assert memory_writer.revoke_memory_consent(api_db, scope, recipient="anthropic", tombstone_log=log) == 2
    # PITR lands before the revocation: simulate restoring the former active row.
    with _database(pg.owner) as owner, owner.tx() as c:
        c.execute(
            "UPDATE agent.memory_consents SET revoked_at=NULL,record_version=1"
            " WHERE tenant_id=%s AND owner_user_id=%s AND recipient='anthropic'",
            (t["tenantId"], t["userId"]),
        )
    admin.restore_bump(pg.owner)
    admin.adopt_runs_after_restore(pg.owner)
    assert api.get("/v1/memory/consents", headers=auth(t)).status_code == 409
    refused_grant = api.post("/v1/memory/consents/anthropic", headers=auth(t))
    assert refused_grant.status_code == 409
    report = reconcile_owner(pg.owner, log, reconciled_by="restore-consent-test")
    assert report.replayed == 1
    consent = api.get("/v1/memory/consents", headers=auth(t)).json()["items"]
    assert len(consent) == 1
    assert consent[0]["recipient"] == "anthropic" and consent[0]["granted"] is False
    assert consent[0]["recordVersion"] == 3
    revisions = api.get("/v1/memory/consents/anthropic/revisions", headers=auth(t)).json()["items"]
    assert revisions[-1]["changeKind"] == "revoked" and revisions[-1]["revision"] == 3


def test_restored_pre_update_memory_is_revoked_conservatively(
    stack: Stack, api: TestClient, pg: PgEnv, tmp_path
) -> None:
    """A recipient/retention update after PITR is replayed as revoke, never stale consent."""
    from aiagent_core import memory_tombstones
    from aiagent_core.memory_restore import reconcile_owner

    t = admin.provision_tenant(pg.owner)
    task = submit(api, t, "memory update restore")
    source_ref, projection_ref = _context_ready(api, t, task, recipient="fake", model="fake-gateway")
    memory_id = _create_memory(api, t, task, source_ref, projection_ref)
    log = memory_tombstones.TombstoneLog(tmp_path / "update-tombstones.jsonl", integrity_key=b"u" * 32)
    log.initialize()
    with _database(pg.dsn["api"]) as api_db:
        revision = memory_writer.update_memory(
            api_db,
            _scope_of(t),
            task_id=task,
            memory_id=memory_id,
            recipient="anthropic",
            expected_revision=1,
            tombstone_log=log,
        )
    assert revision == 2
    # Simulate a PITR snapshot before the update, including its revision/epoch.
    with _database(pg.owner) as owner, owner.tx() as c:
        c.execute("ALTER TABLE agent.memory_records DISABLE TRIGGER memory_record_guard")
        c.execute("ALTER TABLE agent.memory_scope_epochs DISABLE TRIGGER memory_epoch_guard")
        c.execute("ALTER TABLE agent.memory_record_revisions DISABLE TRIGGER memory_revision_append_only")
        c.execute(
            "UPDATE agent.memory_records SET recipient='fake',revision=1,revoked_at=NULL WHERE tenant_id=%s AND id=%s",
            (t["tenantId"], memory_id),
        )
        c.execute(
            "UPDATE agent.memory_scope_epochs SET source_epoch=1 WHERE tenant_id=%s AND task_id=%s",
            (t["tenantId"], task),
        )
        c.execute(
            "DELETE FROM agent.memory_record_revisions WHERE tenant_id=%s AND memory_id=%s AND revision=2",
            (t["tenantId"], memory_id),
        )
        c.execute("ALTER TABLE agent.memory_record_revisions ENABLE TRIGGER memory_revision_append_only")
        c.execute("ALTER TABLE agent.memory_scope_epochs ENABLE TRIGGER memory_epoch_guard")
        c.execute("ALTER TABLE agent.memory_records ENABLE TRIGGER memory_record_guard")
    admin.restore_bump(pg.owner)
    admin.adopt_runs_after_restore(pg.owner)
    report = reconcile_owner(pg.owner, log, reconciled_by="restore-update-test")
    assert report.replayed == 1
    item = api.get(f"/v1/tasks/{task}/memory", headers=auth(t)).json()["items"][0]
    assert item["id"] == memory_id and item["revoked"] is True and item["revision"] == 2
    history = api.get(f"/v1/tasks/{task}/memory/{memory_id}/revisions", headers=auth(t)).json()["items"]
    assert history[-1]["changeKind"] == "revoked" and history[-1]["revision"] == 2
