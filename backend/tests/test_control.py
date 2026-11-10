"""STEP 3-A: Run state management on the formal schema (claim / fence / goal / cancel / HEAD CAS / tenant)."""

from __future__ import annotations

import threading

import psycopg
import pytest
from aiagent_core import admin, commands, control
from aiagent_core.commands import CommandError, Scope
from aiagent_core.db import Database, connect
from conftest import PgEnv

pytestmark = pytest.mark.pg


def scope(t: dict[str, str]) -> Scope:
    return Scope(t["tenantId"], t["userId"], t["workspaceId"])


def submit(
    dbs: dict[str, Database], t: dict[str, str], goal: str = "find widget price", **kw: object
) -> dict[str, str]:
    return commands.submit_task(dbs["api"], scope(t), goal, **kw)  # type: ignore[arg-type]


def run_row(dbs: dict[str, Database], run_id: str) -> dict:
    r = dbs["owner"].one("SELECT * FROM agent.runs WHERE id=%s", (run_id,))
    assert r is not None
    return r


def test_submit_is_atomic_and_idempotent(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    a = submit(dbs, tenant, idempotency_key="key-00000001")
    b = submit(dbs, tenant, idempotency_key="key-00000001")
    assert a == b
    with pytest.raises(CommandError, match="IDEMPOTENCY_KEY_REUSED"):
        submit(dbs, tenant, "different goal", idempotency_key="key-00000001")
    o = dbs["owner"]
    assert o.one("SELECT count(*) n FROM agent.tasks WHERE tenant_id=%s", (tenant["tenantId"],))["n"] == 1
    kinds = [
        r["event_kind"]
        for r in o.all(
            "SELECT event_kind FROM agent.domain_events WHERE task_id=%s ORDER BY delivery_offset", (a["taskId"],)
        )
    ]
    assert kinds == ["task.accepted", "run.created"]
    ob = o.all("SELECT * FROM agent.outbox_messages WHERE tenant_id=%s", (tenant["tenantId"],))
    assert len(ob) == 1 and ob[0]["safe_payload_json"]["kind"] == "run.start"
    assert run_row(dbs, a["runId"])["status_code"] == "CREATED"


def test_rollback_leaves_nothing(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    from aiagent_core.events import append_event

    with pytest.raises(ValueError), dbs["api"].tx(tenant=tenant["tenantId"]) as c:
        commands.create_task_tx(
            c,
            tenant_id=tenant["tenantId"],
            owner_user_id=tenant["userId"],
            workspace_id=tenant["workspaceId"],
            goal="x",
            actor={"type": "user", "id": "u"},
        )
        append_event(
            c,
            tenant_id=tenant["tenantId"],
            aggregate_type="task",
            aggregate_id=tenant["tenantId"],
            kind="task.accepted",
            payload={"content": "raw"},
            actor={},
            owner_user_id=None,
        )
    assert dbs["owner"].one("SELECT count(*) n FROM agent.tasks WHERE tenant_id=%s", (tenant["tenantId"],))["n"] == 0


def test_claim_fencing_and_stale_worker(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    ids = submit(dbs, tenant)
    rt = dbs["runtime"]
    c1 = control.claim_run(rt, tenant["tenantId"], ids["runId"], "w1", lease_s=30)
    assert c1.lease_epoch == 1
    with pytest.raises(control.FencedOut):
        control.claim_run(rt, tenant["tenantId"], ids["runId"], "w2")
    dbs["owner"].one(
        "UPDATE agent.runs SET lease_expires_at = now() - interval '1s' WHERE id=%s RETURNING 1", (ids["runId"],)
    )
    c2 = control.claim_run(rt, tenant["tenantId"], ids["runId"], "w2")
    assert c2.lease_epoch == 2 and c2.run_id == c1.run_id  # same run id, epoch++ on reclaim
    assert not control.heartbeat(rt, c1)
    with pytest.raises(control.FencedOut), dbs["model_gateway"].tx() as c:
        control.append_head(
            c, c1, "assistant", {"kind": "assistant", "content": "stale"}, writer="t", request_key="a" * 64
        )
    with pytest.raises(control.FencedOut):
        control.finish_segment(rt, c1, "SUCCEEDED", result_text="stale")
    with pytest.raises(control.FencedOut):
        control.set_framework_ref(rt, c1, "x")
    assert control.finish_segment(rt, c2, "SUCCEEDED", result_text="ok") == "SUCCEEDED"
    with pytest.raises(control.RunTerminal):
        control.claim_run(rt, tenant["tenantId"], ids["runId"], "w3")


def test_cancel_wins_over_completion(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    ids = submit(dbs, tenant)
    c = control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "w1")
    assert commands.cancel(dbs["api"], scope(tenant), ids["taskId"])["state"] == "CANCELLING"
    assert control.finish_segment(dbs["runtime"], c, "SUCCEEDED", result_text="done") == "CANCELLED"
    r = run_row(dbs, ids["runId"])
    assert r["status_code"] == "CANCELLED" and r["result_text"] is None
    with pytest.raises(psycopg.errors.CheckViolation), connect(dbs["owner"].url) as oc:
        oc.execute("UPDATE agent.runs SET status_code='SUCCEEDED' WHERE id=%s", (ids["runId"],))  # DB guard too


def test_cancel_idle_run_is_immediate(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    ids = submit(dbs, tenant)
    assert commands.cancel(dbs["api"], scope(tenant), ids["taskId"])["state"] == "CANCELLED"
    with pytest.raises(control.Cancelled):
        control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "w1")


def test_stale_goal_refused(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    ids = submit(dbs, tenant)
    c = control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "w1")
    commands.steer(dbs["api"], scope(tenant), ids["taskId"], "new goal", 1)
    with pytest.raises(control.StaleGoal), dbs["model_gateway"].tx() as t:
        control.append_head(
            t, c, "assistant", {"kind": "assistant", "content": "old"}, writer="t", request_key="b" * 64
        )
    with pytest.raises(control.StaleGoal):
        control.finish_segment(dbs["runtime"], c, "SUCCEEDED", result_text="old goal answer")
    with pytest.raises(CommandError, match="GOAL_REVISION_CONFLICT"):
        commands.steer(dbs["api"], scope(tenant), ids["taskId"], "again", 1)
    assert control.release(dbs["runtime"], c)
    c2 = control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "w2")
    assert c2.goal_revision == 2


def test_head_cas_concurrent_appends_and_hash_chain(
    dbs: dict[str, Database], tenant: dict[str, str], pg: PgEnv
) -> None:
    ids = submit(dbs, tenant)
    c = control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "w1")
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            with dbs["tool_gateway"].tx() as t:
                control.append_head(
                    t,
                    c,
                    "tool",
                    {"kind": "tool", "tool_call_id": f"call_{i}", "content": str(i)},
                    writer="tgw",
                    tool_call_id=f"call_{i}",
                )
        except BaseException as e:
            errors.append(e)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    with dbs["owner"].conn() as oc:
        revs = [e["checkpoint_revision"] for e in control.head_entries(oc, tenant["tenantId"], ids["runId"])]
        assert revs == list(range(1, 18))
        assert control.verify_head_chain(oc, tenant["tenantId"], ids["runId"])
        # duplicate tool result for the same call id is idempotent, not a second entry
        with oc.transaction():
            control.append_head(
                oc,
                c,
                "tool",
                {"kind": "tool", "tool_call_id": "call_1", "content": "1"},
                writer="t",
                tool_call_id="call_1",
            )
        assert len(control.head_entries(oc, tenant["tenantId"], ids["runId"])) == 17
        # tamper (superuser bypasses the trigger) -> chain verification detects it
        oc.execute("ALTER TABLE agent.run_checkpoints DISABLE TRIGGER run_checkpoints_append_only")
        oc.execute(
            'UPDATE agent.run_checkpoints SET checkpoint_json = \'{"kind":"tool","content":"evil"}\''
            " WHERE run_id=%s AND checkpoint_revision=3",
            (ids["runId"],),
        )
        oc.execute("ALTER TABLE agent.run_checkpoints ENABLE TRIGGER run_checkpoints_append_only")
        assert not control.verify_head_chain(oc, tenant["tenantId"], ids["runId"])


def test_framework_ref_conditional_discard(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    ids = submit(dbs, tenant)
    rt = dbs["runtime"]
    c1 = control.claim_run(rt, tenant["tenantId"], ids["runId"], "w1")
    control.set_framework_ref(rt, c1, "ref-1")
    dbs["owner"].one(
        "UPDATE agent.runs SET lease_expires_at = now() - interval '1s' WHERE id=%s RETURNING 1", (ids["runId"],)
    )
    c2 = control.claim_run(rt, tenant["tenantId"], ids["runId"], "w2")
    control.set_framework_ref(rt, c2, "ref-2")
    assert not control.discard_framework_ref(rt, c1, "ref-1")  # stale worker cannot erase the newer pointer
    assert control.framework_ref(rt, tenant["tenantId"], ids["runId"]) == "ref-2"
    assert not control.discard_framework_ref(rt, c2, "ref-1")
    assert control.discard_framework_ref(rt, c2, "ref-2")


def test_int64_fence_boundary(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    ids = submit(dbs, tenant)
    dbs["owner"].one("UPDATE agent.runs SET lease_epoch = 9007199254740992 WHERE id=%s RETURNING 1", (ids["runId"],))
    c = control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "w1")
    assert c.lease_epoch == 9007199254740993
    assert control.Claim.from_wire(c.to_wire()) == c
    assert c.to_wire()["leaseEpoch"] == "9007199254740993"
    dbs["owner"].one(
        "UPDATE agent.runs SET lease_epoch = 9223372036854775807, lease_expires_at = now() - interval '1s'"
        " WHERE id=%s RETURNING 1",
        (ids["runId"],),
    )
    with pytest.raises(psycopg.errors.NumericValueOutOfRange):  # overflow fails closed, never wraps
        control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "w2")


def test_tenant_and_owner_isolation(dbs: dict[str, Database], tenant: dict[str, str], pg: PgEnv) -> None:
    ids = submit(dbs, tenant)
    other_tenant = admin.provision_tenant(pg.owner)
    same_tenant_other_owner = admin.add_user(pg.owner, tenant["tenantId"])
    with pytest.raises(control.TenantMismatch):
        control.claim_run(dbs["runtime"], other_tenant["tenantId"], ids["runId"], "w")
    for s in (other_tenant, same_tenant_other_owner):
        with pytest.raises(CommandError, match="NOT_FOUND"):
            commands.cancel(dbs["api"], scope(s), ids["taskId"])
    c = control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "w")
    forged = control.Claim(
        other_tenant["tenantId"],
        c.owner_user_id,
        c.task_id,
        c.run_id,
        c.lease_epoch,
        c.goal_revision,
        c.worker_ref,
        c.restore_incarnation,
    )
    with pytest.raises(control.TenantMismatch), dbs["model_gateway"].tx() as t:
        control.check_current(t, forged)
    forged_owner = control.Claim(
        c.tenant_id,
        same_tenant_other_owner["userId"],
        c.task_id,
        c.run_id,
        c.lease_epoch,
        c.goal_revision,
        c.worker_ref,
        c.restore_incarnation,
    )
    with pytest.raises(control.TenantMismatch), dbs["model_gateway"].tx() as t:
        control.check_current(t, forged_owner)


def test_restore_incarnation_fences_old_claims(dbs: dict[str, Database], tenant: dict[str, str], pg: PgEnv) -> None:
    ids = submit(dbs, tenant)
    c = control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "w1")
    admin.restore_bump(pg.owner)
    try:
        with pytest.raises(control.RestoreFenced), dbs["tool_gateway"].tx() as t:
            control.check_current(t, c)
        with pytest.raises(control.NotClaimable):  # dispatch paused until the operator adopts runs
            control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "w2")
    finally:
        admin.adopt_runs_after_restore(pg.owner)
    c2 = control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "w2")
    assert c2.restore_incarnation == c.restore_incarnation + 1


def test_pause_resume(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    ids = submit(dbs, tenant)
    c = control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "w1")
    commands.pause(dbs["api"], scope(tenant), ids["taskId"])
    assert control.finish_segment(dbs["runtime"], c, "SUCCEEDED", result_text="x") == "PAUSED"
    with pytest.raises(control.NotClaimable):
        control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "w2")
    commands.resume(dbs["api"], scope(tenant), ids["taskId"])
    assert control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "w2").lease_epoch == 2


def test_revoked_scope_refuses_new_tasks_run_claims_and_gateway_admission(
    dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    ids = submit(dbs, tenant)
    current = control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "w")
    with dbs["runtime"].tx() as c:
        assert control.check_current(c, current)
    dbs["owner"].one(
        "UPDATE agent.tenant_memberships SET disabled_at=now() WHERE tenant_id=%s AND user_id=%s RETURNING 1",
        (tenant["tenantId"], tenant["userId"]),
    )
    with pytest.raises(commands.CommandError, match="FORBIDDEN|SCOPE_REVOKED"):
        submit(dbs, tenant)
    with pytest.raises(control.ScopeRevoked):
        control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "other")
    with dbs["runtime"].tx() as c, pytest.raises(control.ScopeRevoked):
        control.check_current(c, current)


def test_pause_blocks_new_gateway_admission(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    ids = submit(dbs, tenant)
    current = control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "w")
    commands.pause(dbs["api"], scope(tenant), ids["taskId"])
    with dbs["runtime"].tx() as c, pytest.raises(control.Paused):
        control.check_current(c, current)
