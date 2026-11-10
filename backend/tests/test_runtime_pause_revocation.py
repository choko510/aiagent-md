"""Pause/resume and membership revocation across QueuePort -> RuntimeWorker -> run/task state.

These tests execute the actual PostgreSQL job claim/ACK and state machine. A deterministic
fake segment stands in for the model to force a pause/revocation precisely at the boundary.
"""

from __future__ import annotations

import pytest
from aiagent_core import commands, control
from aiagent_core.commands import Scope
from aiagent_core.db import Database
from aiagent_core.queue import QueuePort
from aiagent_domain import uuid7
from aiagent_runtime.adapter import _CODE_TO_STATUS, SegmentOutcome
from aiagent_runtime.worker import RuntimeWorker

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
    queue = QueuePort(dbs["relay"])
    assert queue.enqueue("run", tenant["tenantId"], f"state-{uuid7()}", _pointer(tenant, ids))
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
    assert dbs["owner"].one("SELECT count(*) n FROM queue.dead_letters")["n"] == 0


@pytest.mark.asyncio
async def test_revoked_before_claim_acks_with_audit_not_dlq(
    dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
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
    assert dbs["owner"].one("SELECT count(*) n FROM queue.dead_letters")["n"] == 0
    audit = dbs["owner"].all(
        "SELECT payload_json FROM agent.domain_events WHERE run_id=%s AND event_kind='run.state_changed'",
        (ids["runId"],),
    )
    assert len(audit) == 1 and audit[0]["payload_json"]["cause"] == "SCOPE_REVOKED"


def test_gateway_rejections_have_distinct_runtime_outcomes() -> None:
    assert _CODE_TO_STATUS["PAUSED"] == "PAUSED"
    assert _CODE_TO_STATUS["SCOPE_REVOKED"] == "REVOKED"
