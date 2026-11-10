"""Pause/resume and membership revocation across QueuePort -> RuntimeWorker -> run/task state.

These tests execute the actual PostgreSQL job claim/ACK and state machine. A deterministic
fake segment stands in for the model to force a pause/revocation precisely at the boundary.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys

import pytest
from aiagent_core import commands, control
from aiagent_core.commands import Scope
from aiagent_core.db import Database
from aiagent_core.queue import QueuePort
from aiagent_domain import uuid7
from aiagent_runtime.adapter import _CODE_TO_STATUS, SegmentOutcome
from aiagent_runtime.worker import RuntimeWorker
from conftest import PgEnv

pytestmark = pytest.mark.pg


def _pointer(tenant: dict[str, str], ids: dict[str, str]) -> dict:
    return {
        "schemaVersion": "1.0",
        "kind": "run.start",
        "tenantId": tenant["tenantId"],
        "taskId": ids["taskId"],
        "runId": ids["runId"],
        "expectedGoalRevision": "1",
        "restoreIncarnation": "1",
        "scheduleOccurrenceId": None,
        "expiresAt": "2099-01-01T00:00:00.000Z",
    }


def _job(dbs: dict[str, Database], tenant: dict[str, str], ids: dict[str, str]) -> object:
    # Other tests share the ephemeral database; make sure unrelated leased jobs
    # cannot exhaust the queue's small default global concurrency budget.
    dbs["owner"].one(
        "UPDATE queue.queues SET paused=false, global_active_limit=100,"
        " per_tenant_active_limit=100 WHERE name='run' RETURNING 1"
    )
    queue = QueuePort(dbs["relay"])
    pointer = _pointer(tenant, ids)
    incarnation = dbs["owner"].one("SELECT restore_incarnation FROM agent.schema_control")
    assert incarnation is not None
    pointer["restoreIncarnation"] = str(incarnation["restore_incarnation"])
    assert queue.enqueue("run", tenant["tenantId"], f"state-{uuid7()}", pointer)
    job = QueuePort(dbs["runtime"]).dequeue("run", "state-worker")
    assert job is not None
    return job


def _run_state(dbs: dict[str, Database], run_id: str) -> str:
    return str(dbs["owner"].one("SELECT status_code FROM agent.runs WHERE id=%s", (run_id,))["status_code"])


def _queue_state(dbs: dict[str, Database], job_id: int) -> str:
    return str(dbs["owner"].one("SELECT status FROM queue.jobs WHERE id=%s", (job_id,))["status"])


@pytest.mark.asyncio
async def test_running_pause_then_resume_acks_without_retry_or_dlq(
    dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    ids = commands.submit_task(
        dbs["api"], Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"]), "test pause"
    )
    user_scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])

    class ControlledSegment:
        calls = 0

        async def run_segment(self, claim: control.Claim, profile: str) -> SegmentOutcome:
            self.calls += 1
            if self.calls == 1:
                commands.pause(dbs["api"], user_scope, ids["taskId"])
                return SegmentOutcome("PAUSED")
            return SegmentOutcome("FINAL", text="done")

    worker = RuntimeWorker(dbs["runtime"], ControlledSegment(), worker_id="state-worker")
    job1 = _job(dbs, tenant, ids)
    result1 = await worker.handle(job1)
    assert result1 == "PAUSED"
    assert _run_state(dbs, ids["runId"]) == "PAUSED"
    assert _queue_state(dbs, job1.id) == "DONE"
    commands.resume(dbs["api"], user_scope, ids["taskId"])
    job2 = _job(dbs, tenant, ids)
    assert await worker.handle(job2) == "SUCCEEDED"
    assert _run_state(dbs, ids["runId"]) == "SUCCEEDED"
    assert _queue_state(dbs, job2.id) == "DONE"
    assert (
        dbs["owner"].one("SELECT count(*) n FROM queue.dead_letters WHERE job_id IN (%s,%s)", (job1.id, job2.id))["n"]
        == 0
    )


@pytest.mark.asyncio
async def test_revoked_before_claim_acks_with_audit_not_dlq(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    ids = commands.submit_task(
        dbs["api"], Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"]), "test revocation"
    )
    job = _job(dbs, tenant, ids)
    dbs["owner"].one(
        "UPDATE agent.tenant_memberships SET disabled_at=now() WHERE tenant_id=%s AND user_id=%s RETURNING 1",
        (tenant["tenantId"], tenant["userId"]),
    )

    class NeverRun:
        async def run_segment(self, claim: control.Claim, profile: str) -> SegmentOutcome:
            raise AssertionError("revoked job must never execute an agent segment")

    worker = RuntimeWorker(dbs["runtime"], NeverRun(), worker_id="state-worker")
    assert await worker.handle(job) == "CANCELLED"
    assert _run_state(dbs, ids["runId"]) == "CANCELLED"
    assert _queue_state(dbs, job.id) == "DONE"
    assert dbs["owner"].one("SELECT count(*) n FROM queue.dead_letters WHERE job_id=%s", (job.id,))["n"] == 0
    audit = dbs["owner"].all(
        "SELECT payload_json FROM agent.domain_events WHERE run_id=%s AND event_kind='run.state_changed'",
        (ids["runId"],),
    )
    assert len(audit) == 1 and audit[0]["payload_json"]["cause"] == "SCOPE_REVOKED"


def test_gateway_rejections_have_distinct_runtime_outcomes() -> None:
    assert _CODE_TO_STATUS["PAUSED"] == "PAUSED"
    assert _CODE_TO_STATUS["SCOPE_REVOKED"] == "REVOKED"


def _pending_intent(dbs: dict[str, Database], tenant: dict[str, str], ids: dict[str, str], status: str) -> None:
    assert status in ("SENDING", "UNKNOWN")
    intent_id = uuid7()
    dbs["owner"].one(
        "INSERT INTO agent.tool_intents (tenant_id, id, task_id, run_id, tool_key, invocation_key,"
        " request_sha256, safe_arguments_json, risk_class, goal_revision, status_code)"
        " VALUES (%s,%s,%s,%s,'send_message',%s,%s,'{}'::jsonb,'external_irreversible',1,%s) RETURNING id",
        (tenant["tenantId"], intent_id, ids["taskId"], ids["runId"], intent_id, "a" * 64, status),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["SENDING", "UNKNOWN"])
async def test_paused_redelivery_preserves_unresolved_effect_and_acks(
    dbs: dict[str, Database], tenant: dict[str, str], effect: str
) -> None:
    user_scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    ids = commands.submit_task(dbs["api"], user_scope, "pause while effect is unconfirmed")
    claim = control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "original-worker")
    _pending_intent(dbs, tenant, ids, effect)
    job = _job(dbs, tenant, ids)
    commands.pause(dbs["api"], user_scope, ids["taskId"])

    class NeverRun:
        async def run_segment(self, claim: control.Claim, profile: str) -> SegmentOutcome:
            raise AssertionError("a paused delivery must not run the model or tools")

    worker = RuntimeWorker(dbs["runtime"], NeverRun(), worker_id="state-worker")
    assert await worker.handle(job) == "WAITING_RECONCILIATION"
    assert _run_state(dbs, ids["runId"]) == "WAITING_RECONCILIATION"
    assert (
        dbs["owner"].one("SELECT status_code FROM agent.tasks WHERE id=%s", (ids["taskId"],))["status_code"]
        == "WAITING_RECONCILIATION"
    )
    assert (
        dbs["owner"].one("SELECT lease_epoch FROM agent.runs WHERE id=%s", (ids["runId"],))["lease_epoch"]
        > claim.lease_epoch
    )
    assert _queue_state(dbs, job.id) == "DONE"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="requires a real SIGKILL of the Worker process")
@pytest.mark.parametrize("effect", [None, "SENDING", "UNKNOWN"])
async def test_pause_cancel_worker_kill_queue_redelivery_settles_without_dlq(
    dbs: dict[str, Database], pg: PgEnv, tenant: dict[str, str], effect: str | None
) -> None:
    """Actual Worker subprocess: RUNNING -> Pause -> Cancel -> SIGKILL -> redelivery."""
    user_scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    ids = commands.submit_task(dbs["api"], user_scope, "crash after pause and cancel")
    dbs["owner"].one(
        "UPDATE queue.queues SET paused=false, global_active_limit=100,"
        " per_tenant_active_limit=100 WHERE name='run' RETURNING 1"
    )
    pointer = _pointer(tenant, ids)
    incarnation = dbs["owner"].one("SELECT restore_incarnation FROM agent.schema_control")
    assert incarnation is not None
    pointer["restoreIncarnation"] = str(incarnation["restore_incarnation"])
    job_id = QueuePort(dbs["relay"]).enqueue("run", tenant["tenantId"], f"kill-{uuid7()}", pointer, priority=999)
    assert job_id is not None

    child_code = """
import asyncio
import os

from aiagent_core.db import Database
from aiagent_runtime.worker import RuntimeWorker

class HangingRuntime:
    async def run_segment(self, claim, profile):
        print("RUNNING", flush=True)
        await asyncio.sleep(120)

async def main():
    db = Database(os.environ["TEST_RUNTIME_DSN"])
    w = RuntimeWorker(db, HangingRuntime(), worker_id="killed-worker", lease_s=30.0)
    job = w.queue.dequeue("run", "killed-worker")
    assert job is not None and job.id == int(os.environ["TEST_JOB_ID"]), job
    await w.handle(job)

asyncio.run(main())
"""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", child_code],
        env={**os.environ, "TEST_RUNTIME_DSN": pg.dsn["runtime"], "TEST_JOB_ID": str(job_id)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 25)
        assert ready, "Worker never reached RUNNING"
        assert process.stdout.readline().strip() == "RUNNING"
        if effect is not None:
            _pending_intent(dbs, tenant, ids, effect)
        commands.pause(dbs["api"], user_scope, ids["taskId"])
        assert commands.cancel(dbs["api"], user_scope, ids["taskId"])["state"] == "CANCELLING"
        assert _run_state(dbs, ids["runId"]) == "CANCELLING"
        process.kill()
        assert process.wait(timeout=5) < 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()

    # Real queue and Run lease expiry after the OS worker is gone.
    dbs["owner"].one(
        "UPDATE queue.jobs SET locked_until=now()-interval '1 millisecond' WHERE id=%s RETURNING 1", (job_id,)
    )
    dbs["owner"].one(
        "UPDATE agent.runs SET lease_expires_at=now()-interval '1 millisecond' WHERE id=%s RETURNING 1", (ids["runId"],)
    )

    class NeverRun:
        async def run_segment(self, claim: control.Claim, profile: str) -> SegmentOutcome:
            raise AssertionError("a cancelled run must not enter an agent segment")

    worker = RuntimeWorker(dbs["runtime"], NeverRun(), worker_id="recovery-worker")
    redelivered = worker.queue.dequeue("run", "recovery-worker")
    assert redelivered is not None and redelivered.id == job_id
    assert redelivered.attempts == 2
    expected = "WAITING_RECONCILIATION" if effect else "CANCELLED"
    assert await worker.handle(redelivered) == expected
    assert _run_state(dbs, ids["runId"]) == expected
    assert (
        dbs["owner"].one("SELECT status_code FROM agent.tasks WHERE id=%s", (ids["taskId"],))["status_code"] == expected
    )
    assert _queue_state(dbs, job_id) == "DONE"
    assert dbs["owner"].one("SELECT count(*) AS n FROM queue.dead_letters WHERE job_id=%s", (job_id,))["n"] == 0
    audit = dbs["owner"].all(
        "SELECT payload_json FROM agent.domain_events WHERE run_id=%s"
        " AND event_kind='run.state_changed' ORDER BY delivery_offset",
        (ids["runId"],),
    )
    assert audit[-1]["payload_json"]["to"] == expected
    assert audit[-1]["payload_json"]["cause"] == "cancel_at_admission"
