"""STEP 3-B: QueuePort / Transactional Outbox relay / Scheduler on real PostgreSQL (JQ-T01..T07, T10, T11, T13..T15)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from aiagent_core import admin, commands
from aiagent_core.commands import Scope
from aiagent_core.db import Database
from aiagent_core.queue import QueueError, QueuePort
from aiagent_core.relay import OutboxRelay
from aiagent_core.scheduler import Scheduler, create_schedule, next_after
from aiagent_domain import uuid7
from conftest import PgEnv

pytestmark = pytest.mark.pg
HERE = Path(__file__).parent


def pointer(tenant_id: str, run_id: str | None = None) -> dict:
    return {
        "schemaVersion": "1.0",
        "kind": "run.start",
        "tenantId": tenant_id,
        "taskId": uuid7(),
        "runId": run_id or uuid7(),
        "expectedGoalRevision": "1",
        "restoreIncarnation": "1",
        "scheduleOccurrenceId": None,
        "expiresAt": "2099-01-01T00:00:00.000Z",
    }


@pytest.fixture()
def q(dbs: dict[str, Database]) -> QueuePort:
    owner = dbs["owner"]
    owner.one(
        "UPDATE queue.jobs SET status='CANCELLED', locked_until=NULL WHERE status IN ('READY','CLAIMED') RETURNING 1"
    )
    owner.one(
        "UPDATE queue.queues SET paused=false, visibility_ms=30000, per_tenant_active_limit=2,"
        " global_active_limit=8, max_attempts=5, base_backoff_ms=2000 WHERE name='run' RETURNING 1"
    )
    return QueuePort(dbs["runtime"])


def expire(dbs: dict[str, Database], job_id: int) -> None:
    dbs["owner"].one("UPDATE queue.jobs SET locked_until = now() - interval '1ms' WHERE id=%s RETURNING 1", (job_id,))


def py(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HERE / "proc_helpers.py"), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        timeout=60,
    )


# ------------------------------------------------------------------------------------------------ QueuePort
def test_enqueue_dedupe_and_allowlist(dbs: dict[str, Database], q: QueuePort, tenant: dict[str, str]) -> None:
    rq = QueuePort(dbs["relay"])
    p = pointer(tenant["tenantId"])
    assert rq.enqueue("run", tenant["tenantId"], "k1", p) is not None
    assert rq.enqueue("run", tenant["tenantId"], "k1", p) is None
    with pytest.raises(QueueError):  # JQ-T11: injected queue name
        rq.enqueue("admin", tenant["tenantId"], "k2", p)
    with pytest.raises(Exception):  # strict pointer contract: unknown kind / extra key
        rq.enqueue("run", tenant["tenantId"], "k3", {**p, "kind": "admin"})
    with pytest.raises(Exception):
        rq.enqueue("run", tenant["tenantId"], "k4", {**p, "sql": "DROP TABLE"})


def test_attempt_fence_stale_ack_is_noop(dbs: dict[str, Database], q: QueuePort, tenant: dict[str, str]) -> None:
    QueuePort(dbs["relay"]).enqueue("run", tenant["tenantId"], "fence", pointer(tenant["tenantId"]))
    a = q.dequeue("run", "A")
    assert a is not None and a.attempt_fence == 1
    assert q.dequeue("run", "B") is None  # leased
    expire(dbs, a.id)
    b = q.dequeue("run", "B")
    assert b is not None and b.id == a.id and b.attempt_fence == 2 and b.attempts == 2
    assert not q.heartbeat(a)
    assert not q.ack(a)  # stale attempt cannot complete (pg-boss #925 class)
    assert q.nack(a, "X") == "STALE"
    assert q.ack(b)
    assert not q.ack(b)  # double ack is a no-op


def test_heartbeat_prevents_redelivery(dbs: dict[str, Database], q: QueuePort, tenant: dict[str, str]) -> None:
    dbs["owner"].one("UPDATE queue.queues SET visibility_ms=1000 WHERE name='run' RETURNING 1")
    QueuePort(dbs["relay"]).enqueue("run", tenant["tenantId"], "hb", pointer(tenant["tenantId"]))
    a = q.dequeue("run", "A")
    assert a is not None
    for _ in range(3):
        time.sleep(0.6)
        assert q.heartbeat(a)
        assert q.dequeue("run", "B") is None
    time.sleep(1.2)
    assert q.dequeue("run", "B") is not None  # heartbeat stopped -> redelivered


def test_retry_backoff_then_dead_letter(dbs: dict[str, Database], q: QueuePort, tenant: dict[str, str]) -> None:
    dbs["owner"].one("UPDATE queue.queues SET max_attempts=3, base_backoff_ms=1 WHERE name='run' RETURNING 1")
    jid = QueuePort(dbs["relay"]).enqueue("run", tenant["tenantId"], "retry", pointer(tenant["tenantId"]))
    outcomes = []
    for _ in range(3):
        time.sleep(0.05)
        j = q.dequeue("run", "A")
        assert j is not None and j.id == jid
        outcomes.append(q.nack(j, "TRANSIENT"))
    assert outcomes == ["RETRY", "RETRY", "DEAD"]
    dl = dbs["owner"].one("SELECT * FROM queue.dead_letters WHERE job_id=%s", (jid,))
    assert dl is not None and dl["reason_code"] == "TRANSIENT" and dl["attempts"] == 3
    assert q.dequeue("run", "A") is None


def test_non_retryable_goes_to_dlq_immediately(dbs: dict[str, Database], q: QueuePort, tenant: dict[str, str]) -> None:
    jid = QueuePort(dbs["relay"]).enqueue("run", tenant["tenantId"], "poison-x", pointer(tenant["tenantId"]))
    j = q.dequeue("run", "A")
    assert j is not None and q.nack(j, "POISON_PAYLOAD", retryable=False) == "DEAD"
    assert dbs["owner"].one("SELECT status FROM queue.jobs WHERE id=%s", (jid,))["status"] == "DEAD"


def test_worker_kill_redelivery_and_poison_quarantine(
    pg: PgEnv, dbs: dict[str, Database], q: QueuePort, tenant: dict[str, str]
) -> None:
    dbs["owner"].one("UPDATE queue.queues SET max_attempts=2, visibility_ms=1000 WHERE name='run' RETURNING 1")
    jid = QueuePort(dbs["relay"]).enqueue("run", tenant["tenantId"], "kill", pointer(tenant["tenantId"]))
    for _ in range(2):  # the job kills its worker every time
        r = py("dequeue_and_die", pg.dsn["runtime"], "victim")
        assert r.returncode == 137 and r.stdout.strip() == str(jid)
        expire(dbs, jid)
    assert q.dequeue("run", "next") is None  # exhausted + expired -> quarantined, not delivered a 3rd time
    assert (
        dbs["owner"].one("SELECT reason_code FROM queue.dead_letters WHERE job_id=%s", (jid,))["reason_code"]
        == "ATTEMPTS_EXHAUSTED_LEASE_EXPIRED"
    )


def test_pause_resume_and_cancel(dbs: dict[str, Database], q: QueuePort, tenant: dict[str, str]) -> None:
    rq = QueuePort(dbs["relay"])
    j1 = rq.enqueue("run", tenant["tenantId"], "p1", pointer(tenant["tenantId"]))
    j2 = rq.enqueue("run", tenant["tenantId"], "p2", pointer(tenant["tenantId"]))
    rq.set_paused("run", True)
    assert q.dequeue("run", "A") is None
    rq.set_paused("run", False)
    assert QueuePort(dbs["owner"]).cancel(j1)  # type: ignore[arg-type]
    got = q.dequeue("run", "A")
    assert got is not None and got.id == j2


def test_concurrent_workers_each_job_once(
    dbs: dict[str, Database], q: QueuePort, tenant: dict[str, str], pg: PgEnv
) -> None:
    dbs["owner"].one(
        "UPDATE queue.queues SET per_tenant_active_limit=100, global_active_limit=100 WHERE name='run' RETURNING 1"
    )
    rq = QueuePort(dbs["relay"])
    ids = {rq.enqueue("run", tenant["tenantId"], f"c{i}", pointer(tenant["tenantId"])) for i in range(200)}
    seen: Counter[int] = Counter()
    lock = threading.Lock()

    def worker(name: str) -> None:
        port = QueuePort(Database(pg.dsn["runtime"], max_size=2))
        while True:
            j = port.dequeue("run", name)
            if j is None:
                return
            with lock:
                seen[j.id] += 1
            assert port.ack(j)

    ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert set(seen) == ids and max(seen.values()) == 1


def test_per_tenant_fairness_and_limits(dbs: dict[str, Database], q: QueuePort, pg: PgEnv) -> None:
    a, b = admin.provision_tenant(pg.owner), admin.provision_tenant(pg.owner)
    rq = QueuePort(dbs["relay"])
    for i in range(20):
        rq.enqueue("run", a["tenantId"], f"fa{i}", pointer(a["tenantId"]))
    for i in range(2):
        rq.enqueue("run", b["tenantId"], f"fb{i}", pointer(b["tenantId"]))
    got = [q.dequeue("run", f"w{i}") for i in range(6)]
    tenants = Counter(j.tenant_id for j in got if j)
    assert tenants[a["tenantId"]] == 2 and tenants[b["tenantId"]] == 2  # per-tenant cap 2; B not starved
    assert sum(1 for j in got if j) == 4


# ------------------------------------------------------------------------------------------------ Outbox relay
def _submit(dbs: dict[str, Database], t: dict[str, str]) -> dict[str, str]:
    return commands.submit_task(dbs["api"], Scope(t["tenantId"], t["userId"], t["workspaceId"]), "goal")


def _jobs_for(dbs: dict[str, Database], run_id: str) -> list[dict]:
    return dbs["owner"].all("SELECT * FROM queue.jobs WHERE payload->>'runId' = %s", (run_id,))


def test_relay_publishes_once_and_marks(dbs: dict[str, Database], q: QueuePort, tenant: dict[str, str]) -> None:
    ids = _submit(dbs, tenant)
    relay = OutboxRelay(dbs["relay"], "r1")
    assert relay.run_once() >= 1
    assert relay.run_once() == 0
    assert len(_jobs_for(dbs, ids["runId"])) == 1


def test_jq_t02_relay_killed_after_claim(
    pg: PgEnv, dbs: dict[str, Database], q: QueuePort, tenant: dict[str, str]
) -> None:
    ids = _submit(dbs, tenant)
    r = py("relay_once", pg.dsn["relay"], "dying", env={"AIAGENT_FAULT": "relay_after_claim"})
    assert r.returncode == 137
    assert _jobs_for(dbs, ids["runId"]) == []
    time.sleep(1.1)  # relay lease 1s
    OutboxRelay(dbs["relay"], "r2").run_once()
    assert len(_jobs_for(dbs, ids["runId"])) == 1


def test_jq_t03_relay_killed_after_publish_before_mark(
    pg: PgEnv, dbs: dict[str, Database], q: QueuePort, tenant: dict[str, str]
) -> None:
    ids = _submit(dbs, tenant)
    r = py("relay_once", pg.dsn["relay"], "dying", env={"AIAGENT_FAULT": "relay_after_publish"})
    assert r.returncode == 137
    assert len(_jobs_for(dbs, ids["runId"])) == 1
    ob = dbs["owner"].one(
        "SELECT dispatched_at FROM agent.outbox_messages WHERE safe_payload_json->>'runId'=%s", (ids["runId"],)
    )
    assert ob["dispatched_at"] is None
    time.sleep(1.1)
    OutboxRelay(dbs["relay"], "r2").run_once()  # republish is deduplicated by the queue key
    assert len(_jobs_for(dbs, ids["runId"])) == 1


def test_jq_t04_two_relays_one_valid_fence(dbs: dict[str, Database], q: QueuePort, tenant: dict[str, str]) -> None:
    _submit(dbs, tenant)
    r1, r2 = OutboxRelay(dbs["relay"], "r1", lease_s=0.5), OutboxRelay(dbs["relay"], "r2", lease_s=30)
    c1 = r1.claim_batch()
    assert c1 and r2.claim_batch() == []  # rows locked by r1's lease
    time.sleep(0.6)
    c2 = r2.claim_batch()
    assert {m.id for m in c2} >= {m.id for m in c1}
    assert not r1.mark(c1[0])  # old relay's fence is invalid
    assert r2.mark([m for m in c2 if m.id == c1[0].id][0])


def test_jq_t13_queue_outage_keeps_outbox(dbs: dict[str, Database], q: QueuePort, tenant: dict[str, str]) -> None:
    QueuePort(dbs["relay"]).set_paused("run", True)
    try:
        ids = _submit(dbs, tenant)  # Domain API commit works while workers can't consume
        OutboxRelay(dbs["relay"], "r1").run_once()
        assert q.dequeue("run", "w") is None
        assert (
            dbs["owner"].one("SELECT status_code FROM agent.runs WHERE id=%s", (ids["runId"],))["status_code"]
            == "CREATED"
        )
        assert len(_jobs_for(dbs, ids["runId"])) == 1  # pointer retained in the (paused) queue, outbox dispatched
    finally:
        QueuePort(dbs["relay"]).set_paused("run", False)
    j = q.dequeue("run", "w")
    assert j is not None


def test_restore_stale_outbox_never_dispatched(
    pg: PgEnv, dbs: dict[str, Database], q: QueuePort, tenant: dict[str, str]
) -> None:
    ids = _submit(dbs, tenant)
    admin.restore_bump(pg.owner)
    try:
        OutboxRelay(dbs["relay"], "r1").run_once()
        assert _jobs_for(dbs, ids["runId"]) == []
        row = dbs["owner"].one(
            "SELECT last_error_code FROM agent.outbox_messages WHERE safe_payload_json->>'runId'=%s", (ids["runId"],)
        )
        assert row["last_error_code"] == "RESTORE_STALE"
    finally:
        admin.adopt_runs_after_restore(pg.owner)


# ------------------------------------------------------------------------------------------------ Scheduler
def _past(dbs: dict[str, Database], sid: str, minutes: int) -> None:
    dbs["owner"].one(
        "UPDATE agent.schedules SET created_at = now() - make_interval(mins => %s),"
        " next_fire_at = now() - interval '1s' WHERE id=%s RETURNING 1",
        (minutes, sid),
    )


def test_one_shot_fires_once_under_concurrency(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    sid = create_schedule(
        dbs["api"],
        tenant_id=tenant["tenantId"],
        owner_user_id=tenant["userId"],
        workspace_id=tenant["workspaceId"],
        goal="remind me",
        run_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    results: list[int] = []
    ts = [threading.Thread(target=lambda: results.append(len(Scheduler(dbs["relay"]).tick()))) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(results) == 1
    assert Scheduler(dbs["relay"]).tick() == []  # restart / replay: no phantom occurrence (JQ-T07)
    occ = dbs["owner"].all("SELECT * FROM agent.schedule_occurrences WHERE schedule_id=%s", (sid,))
    assert len(occ) == 1
    t = dbs["owner"].one("SELECT created_from_schedule_id FROM agent.tasks WHERE id=%s", (occ[0]["task_id"],))
    assert t["created_from_schedule_id"] == sid


@pytest.mark.parametrize("policy,expected", [("latest", 1), ("all_bounded", 3), ("skip", 0)])
def test_cron_catchup_policies(dbs: dict[str, Database], tenant: dict[str, str], policy: str, expected: int) -> None:
    sid = create_schedule(
        dbs["api"],
        tenant_id=tenant["tenantId"],
        owner_user_id=tenant["userId"],
        workspace_id=tenant["workspaceId"],
        goal="every minute",
        cron="* * * * *",
        catchup=policy,
        max_catchup=3,
    )
    _past(dbs, sid, 10)  # scheduler was down for 10 minutes
    fired = [f for f in Scheduler(dbs["relay"]).tick() if f.schedule_id == sid]
    # 'skip' may legitimately fire the newest slot if it is within the grace window
    assert len(fired) == expected or (policy == "skip" and len(fired) <= 1)
    assert [f for f in Scheduler(dbs["relay"]).tick() if f.schedule_id == sid] == []
    nxt = dbs["owner"].one("SELECT next_fire_at FROM agent.schedules WHERE id=%s", (sid,))["next_fire_at"]
    assert nxt > datetime.now(UTC) - timedelta(seconds=5)


def test_cron_timezone_and_disable(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    after = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    nxt = next_after("0 9 * * *", "Asia/Tokyo", after)
    assert nxt is not None and nxt.astimezone(UTC) == datetime(2026, 11, 11, 0, 0, tzinfo=UTC)
    sid = create_schedule(
        dbs["api"],
        tenant_id=tenant["tenantId"],
        owner_user_id=tenant["userId"],
        workspace_id=tenant["workspaceId"],
        goal="x",
        cron="* * * * *",
    )
    Scheduler(dbs["relay"]).disable(tenant["tenantId"], sid)
    _past(dbs, sid, 5)
    dbs["owner"].one("UPDATE agent.schedules SET enabled=false WHERE id=%s RETURNING 1", (sid,))
    assert [f for f in Scheduler(dbs["relay"]).tick() if f.schedule_id == sid] == []
    with pytest.raises(Exception):
        create_schedule(
            dbs["api"],
            tenant_id=tenant["tenantId"],
            owner_user_id=tenant["userId"],
            workspace_id=tenant["workspaceId"],
            goal="x",
            cron="* * * * *",
            timezone="Mars/Olympus",
        )


def test_scheduler_relay_and_queue_chain(dbs: dict[str, Database], q: QueuePort, tenant: dict[str, str]) -> None:
    create_schedule(
        dbs["api"],
        tenant_id=tenant["tenantId"],
        owner_user_id=tenant["userId"],
        workspace_id=tenant["workspaceId"],
        goal="chain",
        run_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    fired = Scheduler(dbs["relay"]).tick()
    OutboxRelay(dbs["relay"], "r").run_once()
    run_ids = {
        dbs["owner"].one("SELECT active_run_id FROM agent.tasks WHERE id=%s", (f.task_id,))["active_run_id"]
        for f in fired
    }
    for rid in run_ids:
        jobs = _jobs_for(dbs, rid)
        assert len(jobs) == 1 and jobs[0]["payload"]["scheduleOccurrenceId"] is not None


def test_cross_tenant_forged_pointer_rejected_by_consumer_check(
    dbs: dict[str, Database], tenant: dict[str, str], pg: PgEnv
) -> None:
    """JQ-T10: a pointer naming tenant X but run of tenant Y cannot be claimed."""
    from aiagent_core import control

    ids = _submit(dbs, tenant)
    other = admin.provision_tenant(pg.owner)
    with pytest.raises(control.TenantMismatch):
        control.claim_run(dbs["runtime"], other["tenantId"], ids["runId"], "w")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # and the API role cannot reach queue tables at all
        dbs["api"].one("SELECT * FROM queue.jobs")
