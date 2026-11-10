"""QueuePort: self-built PostgreSQL queue (ADR-039 candidate) - `FOR UPDATE SKIP LOCKED` + lease + attempt fence.

Delivery is at-least-once. The queue is a transport, never an authority: consumers re-validate the pointer against
agent.* and the Effect Ledger before any work, and queue retries never re-send an external effect (Tool Gateway owns
that decision). Attempt fence: every claim increments `attempt_fence`; heartbeat/ack/nack of an older attempt are
no-ops (pg-boss #925 class of bug). Expired leases are re-delivered; exhausted attempts go to the dead-letter table.
Fairness: a claim prefers tenants with fewer active jobs and never exceeds per-tenant / global active limits.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from aiagent_domain import sha256_hex
from aiagent_domain.generated import RunJobPointerV1

from .db import Database, Row

QUEUES = frozenset({"run"})
MAX_PAYLOAD = 8192


class QueueError(Exception):
    pass


@dataclass(frozen=True)
class Job:
    id: int
    queue: str
    tenant_id: str
    dedupe_key: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    attempt_fence: int
    worker: str

    def pointer(self) -> RunJobPointerV1:
        return RunJobPointerV1.model_validate(self.payload)


def _check(queue: str, payload: dict[str, Any]) -> None:
    if queue not in QUEUES:
        raise QueueError("unknown queue")
    if len(sha256_hex(payload)) != 64 or len(str(payload).encode()) > MAX_PAYLOAD:
        raise QueueError("payload too large")
    if queue == "run":
        RunJobPointerV1.model_validate(payload)  # strict contract; unknown keys / types rejected


def enqueue(c: psycopg.Connection[Row], queue: str, tenant_id: str, dedupe_key: str, payload: dict[str, Any],
            *, delay_s: float = 0.0, priority: int = 0) -> int | None:
    """Insert unless (queue, dedupe_key) exists. Returns the new job id or None for a duplicate."""
    _check(queue, payload)
    row = c.execute(
        "INSERT INTO queue.jobs(queue_name, tenant_id, dedupe_key, payload, payload_sha256, max_attempts, priority,"
        " run_after) SELECT %s,%s,%s,%s,%s, q.max_attempts, %s, now() + make_interval(secs => %s)"
        " FROM queue.queues q WHERE q.name=%s ON CONFLICT (queue_name, dedupe_key) DO NOTHING RETURNING id",
        (queue, tenant_id, dedupe_key, Jsonb(payload), sha256_hex(payload), priority, delay_s, queue),
    ).fetchone()
    return int(row["id"]) if row else None


_DEQUEUE = """
WITH q AS (SELECT * FROM queue.queues WHERE name = %(q)s AND NOT paused),
active AS (
  SELECT tenant_id, count(*) AS n FROM queue.jobs
  WHERE queue_name = %(q)s AND status = 'CLAIMED' AND locked_until > now() GROUP BY tenant_id
),
total AS (SELECT coalesce(sum(n), 0) AS n FROM active),
cand AS (
  SELECT j.id FROM queue.jobs j CROSS JOIN q CROSS JOIN total
  LEFT JOIN active a ON a.tenant_id = j.tenant_id
  WHERE j.queue_name = %(q)s
    AND ((j.status = 'READY' AND j.run_after <= now()) OR (j.status = 'CLAIMED' AND j.locked_until <= now()))
    AND j.attempts < j.max_attempts
    AND coalesce(a.n, 0) < q.per_tenant_active_limit
    AND total.n < q.global_active_limit
  ORDER BY coalesce(a.n, 0), j.priority DESC, j.run_after, j.id
  LIMIT 1
  FOR UPDATE OF j SKIP LOCKED
)
UPDATE queue.jobs j SET status = 'CLAIMED', attempts = j.attempts + 1, attempt_fence = j.attempt_fence + 1,
  locked_by = %(w)s, locked_until = now() + make_interval(secs => (SELECT visibility_ms FROM q) / 1000.0),
  updated_at = now()
FROM cand WHERE j.id = cand.id
RETURNING j.id, j.queue_name, j.tenant_id, j.dedupe_key, j.payload, j.attempts, j.max_attempts, j.attempt_fence
"""


class QueuePort:
    def __init__(self, db: Database) -> None:
        self.db = db

    def enqueue(self, queue: str, tenant_id: str, dedupe_key: str, payload: dict[str, Any], **kw: Any) -> int | None:
        with self.db.tx() as c:
            return enqueue(c, queue, tenant_id, dedupe_key, payload, **kw)

    def dequeue(self, queue: str, worker: str) -> Job | None:
        self.dead_letter_exhausted(queue)
        with self.db.tx() as c:
            row = c.execute(_DEQUEUE, {"q": queue, "w": worker}).fetchone()
            if row is None:
                return None
            c.execute(
                "INSERT INTO queue.job_attempts(job_id, attempt_fence, worker_ref) VALUES (%s,%s,%s)",
                (row["id"], row["attempt_fence"], worker),
            )
            return Job(row["id"], row["queue_name"], str(row["tenant_id"]), row["dedupe_key"], dict(row["payload"]),
                       row["attempts"], row["max_attempts"], row["attempt_fence"], worker)

    def heartbeat(self, job: Job, extend_s: float | None = None) -> bool:
        """Extend visibility only for the current attempt. False = this attempt lost the job (stop at next boundary)."""
        with self.db.conn() as c:
            cur = c.execute(
                "UPDATE queue.jobs j SET locked_until = now() + make_interval(secs => coalesce(%s, q.visibility_ms / 1000.0)),"
                " updated_at = now() FROM queue.queues q WHERE q.name = j.queue_name AND j.id=%s AND j.attempt_fence=%s"
                " AND j.status='CLAIMED'",
                (extend_s, job.id, job.attempt_fence),
            )
            return cur.rowcount == 1

    def ack(self, job: Job, outcome: str = "DONE") -> bool:
        """Complete only if this attempt still owns the job. Lease expiry alone does not reject the ACK; a re-claim by
        another worker does (attempt_fence changed). Re-claim and ACK update the same row and serialize."""
        with self.db.tx() as c:
            cur = c.execute(
                "UPDATE queue.jobs SET status='DONE', outcome_code=%s, locked_until=NULL, finished_at=now(), updated_at=now()"
                " WHERE id=%s AND attempt_fence=%s AND status='CLAIMED'",
                (outcome, job.id, job.attempt_fence),
            )
            c.execute("UPDATE queue.job_attempts SET ended_at=now(), outcome_code=%s WHERE job_id=%s AND attempt_fence=%s",
                      (outcome, job.id, job.attempt_fence))
            return cur.rowcount == 1

    def nack(self, job: Job, error_code: str, *, retryable: bool = True, min_delay_s: float = 0.0) -> str:
        """Retry with bounded exponential backoff + full jitter, or dead-letter (non-retryable / exhausted)."""
        with self.db.tx() as c:
            q = c.execute("SELECT base_backoff_ms, max_backoff_ms FROM queue.queues WHERE name=%s", (job.queue,)).fetchone()
            assert q is not None
            c.execute("UPDATE queue.job_attempts SET ended_at=now(), outcome_code=%s WHERE job_id=%s AND attempt_fence=%s",
                      (error_code, job.id, job.attempt_fence))
            if not retryable or job.attempts >= job.max_attempts:
                return "DEAD" if self._dead(c, job.id, job.attempt_fence, error_code) else "STALE"
            delay_ms = max(min_delay_s * 1000, random.uniform(  # noqa: S311
                0, min(q["max_backoff_ms"], q["base_backoff_ms"] * 2 ** (job.attempts - 1))))
            cur = c.execute(
                "UPDATE queue.jobs SET status='READY', locked_until=NULL, locked_by=NULL, last_error_code=%s,"
                " run_after = now() + make_interval(secs => %s / 1000.0), updated_at=now()"
                " WHERE id=%s AND attempt_fence=%s AND status='CLAIMED'",
                (error_code, delay_ms, job.id, job.attempt_fence),
            )
            return "RETRY" if cur.rowcount == 1 else "STALE"

    def _dead(self, c: psycopg.Connection[Row], job_id: int, fence: int | None, reason: str) -> bool:
        cond = " AND attempt_fence=%s AND status='CLAIMED'" if fence is not None else " AND status IN ('READY','CLAIMED')"
        args: tuple[Any, ...] = (reason, job_id) + ((fence,) if fence is not None else ())
        row = c.execute(
            "UPDATE queue.jobs SET status='DEAD', last_error_code=%s, locked_until=NULL, finished_at=now(), updated_at=now()"
            " WHERE id=%s" + cond + " RETURNING id, queue_name, tenant_id, attempts",
            args,
        ).fetchone()
        if row is None:
            return False
        c.execute(
            "INSERT INTO queue.dead_letters(job_id, queue_name, tenant_id, reason_code, attempts) VALUES (%s,%s,%s,%s,%s)"
            " ON CONFLICT (job_id) DO UPDATE SET reason_code=EXCLUDED.reason_code, moved_at=now(), replayed_at=NULL",
            (row["id"], row["queue_name"], row["tenant_id"], reason, row["attempts"]),
        )
        return True

    def dead_letter_exhausted(self, queue: str) -> int:
        """Poison jobs: a lease expired after the last allowed attempt (worker killed every time) -> dead-letter."""
        n = 0
        with self.db.tx() as c:
            rows = c.execute(
                "SELECT id FROM queue.jobs WHERE queue_name=%s AND status='CLAIMED' AND locked_until <= now()"
                " AND attempts >= max_attempts FOR UPDATE SKIP LOCKED",
                (queue,),
            ).fetchall()
            for r in rows:
                n += self._dead(c, r["id"], None, "ATTEMPTS_EXHAUSTED_LEASE_EXPIRED") is True
        return n

    def cancel(self, job_id: int) -> bool:
        with self.db.conn() as c:
            cur = c.execute(
                "UPDATE queue.jobs SET status='CANCELLED', locked_until=NULL, finished_at=now(), updated_at=now()"
                " WHERE id=%s AND status IN ('READY','CLAIMED')",
                (job_id,),
            )
            return cur.rowcount == 1

    def set_paused(self, queue: str, paused: bool) -> None:
        with self.db.conn() as c:
            c.execute("UPDATE queue.queues SET paused=%s, updated_at=now() WHERE name=%s", (paused, queue))

    def replay_dead_letter(self, job_id: int, operator: str) -> bool:
        """Operator replay re-delivers the *pointer* only. The consumer re-validates run/goal/ledger; a replay of a
        cancelled/terminal run is acked as stale without any tool execution (JQ-T15)."""
        with self.db.tx() as c:
            cur = c.execute(
                "UPDATE queue.jobs SET status='READY', attempts=0, run_after=now(), locked_until=NULL, updated_at=now()"
                " WHERE id=%s AND status='DEAD'",
                (job_id,),
            )
            c.execute("UPDATE queue.dead_letters SET replayed_at=now(), replay_decision=%s WHERE job_id=%s",
                      (f"replayed_by:{operator}", job_id))
            return cur.rowcount == 1

    def stats(self, queue: str) -> dict[str, int]:
        rows = self.db.all("SELECT status, count(*) AS n FROM queue.jobs WHERE queue_name=%s GROUP BY status", (queue,))
        return {r["status"]: int(r["n"]) for r in rows}
