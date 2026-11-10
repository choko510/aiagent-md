"""Runtime Worker process: QueuePort consumer -> pointer re-validation -> fenced Run claim -> heartbeat -> segment ->
state commit -> ACK. `python -m aiagent_runtime.worker`.

The queue job is only a pointer. Before any work the worker reloads tenant/run/goal/restore generation from the DB;
stale pointers (terminal run, older goal than a later steer, expired, other restore incarnation) are ACKed without
work. The worker never executes generated code and never holds provider or receiver credentials.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from datetime import UTC, datetime
from typing import Any

from aiagent_core import config, control
from aiagent_core.db import Database
from aiagent_core.logging import log, setup
from aiagent_core.migrate import check_compatible
from aiagent_core.queue import Job, QueuePort
from aiagent_core.rpc import RpcClient
from aiagent_core.service import Lifecycle
from pydantic import ValidationError

from .adapter import DeepAgentsRuntime, SegmentOutcome
from .agent import Gateways, fault

logger = logging.getLogger("aiagent.worker")
MAX_REBUILDS = 2


class RuntimeWorker:
    def __init__(
        self,
        db: Database,
        runtime: DeepAgentsRuntime,
        *,
        worker_id: str,
        concurrency: int = 2,
        lease_s: float = 30.0,
        poll_s: float = 0.5,
    ) -> None:
        self.db, self.runtime, self.worker_id = db, runtime, worker_id
        self.queue = QueuePort(db)
        self.sem = asyncio.Semaphore(concurrency)
        self.lease_s, self.poll_s = lease_s, poll_s
        self.inflight: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ main loop
    async def run(self, stopping: asyncio.Event, *, max_jobs: int | None = None, idle_exit: bool = False) -> int:
        done = 0
        while not stopping.is_set():
            await self.sem.acquire()
            job = await asyncio.to_thread(self.queue.dequeue, "run", self.worker_id)
            if job is None:
                self.sem.release()
                if idle_exit and not self.inflight:
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stopping.wait(), timeout=self.poll_s)
                continue
            t = asyncio.create_task(self._guarded(job))
            self.inflight.add(t)
            t.add_done_callback(self.inflight.discard)
            done += 1
            if max_jobs is not None and done >= max_jobs:
                break
        if self.inflight:
            await asyncio.wait(self.inflight)
        return done

    async def drain(self, deadline_s: float) -> None:
        """Graceful shutdown: in-flight segments get `deadline_s`; then they are cancelled. A cancelled segment's
        claim simply expires (gateways refuse its stale fence) and the job is redelivered."""
        if not self.inflight:
            return
        _, pending = await asyncio.wait(self.inflight, timeout=deadline_s)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=5)

    async def _guarded(self, job: Job) -> None:
        try:
            await self.handle(job)
        except Exception as e:
            log(logger, logging.ERROR, "job failed", job=job.id, exc=type(e).__name__)
            await asyncio.to_thread(self.queue.nack, job, "WORKER_ERROR")
        finally:
            self.sem.release()

    # ------------------------------------------------------------------ one job
    async def handle(self, job: Job) -> str:
        try:
            p = job.pointer()
        except ValidationError:
            await asyncio.to_thread(self.queue.nack, job, "POISON_PAYLOAD", retryable=False)
            return "POISON"
        if p.tenant_id != job.tenant_id:
            await asyncio.to_thread(self.queue.nack, job, "POINTER_TENANT_MISMATCH", retryable=False)
            return "POISON"
        stale = await asyncio.to_thread(self._stale_reason, p)
        if stale is not None:
            await asyncio.to_thread(self.queue.ack, job, stale)
            return stale
        try:
            claim = await asyncio.to_thread(
                control.claim_run,
                self.db,
                p.tenant_id,
                p.run_id,
                self.worker_id,
                lease_s=self.lease_s,
                expected_incarnation=p.restore_incarnation,
            )
        except (control.RunTerminal, control.TenantMismatch, control.RestoreFenced) as e:
            await asyncio.to_thread(self.queue.ack, job, f"STALE_{e.code}")
            return f"STALE_{e.code}"
        except control.ScopeRevoked:
            state = await asyncio.to_thread(control.settle_stopped, self.db, p.tenant_id, p.run_id, revoked=True)
            if state == "SCOPE_RESTORED":
                await asyncio.to_thread(self.queue.nack, job, "SCOPE_RESTORED")
            else:
                await asyncio.to_thread(self.queue.ack, job, "SCOPE_REVOKED")
            return state
        except control.NotClaimable as e:
            if str(e) == "paused":
                state = await asyncio.to_thread(control.settle_stopped, self.db, p.tenant_id, p.run_id, revoked=False)
                if state == "PAUSE_CLEARED":
                    await asyncio.to_thread(self.queue.nack, job, "PAUSE_CLEARED")
                else:
                    await asyncio.to_thread(self.queue.ack, job, "PAUSED")
                return state
            await asyncio.to_thread(self.queue.nack, job, e.code)
            return e.code
        except control.FencedOut as e:
            await asyncio.to_thread(self.queue.nack, job, e.code)
            return e.code
        fault("worker_after_claim")
        profile = await asyncio.to_thread(self._profile, claim)
        hb = asyncio.create_task(self._heartbeat(claim, job))
        try:
            try:
                await asyncio.to_thread(control.mark_running, self.db, claim)
            except control.Cancelled:
                outcome = SegmentOutcome("CANCELLED")
            except control.Paused:
                outcome = SegmentOutcome("PAUSED")
            except control.ScopeRevoked:
                outcome = SegmentOutcome("REVOKED", detail="SCOPE_REVOKED")
            else:
                outcome = SegmentOutcome("CANCELLED")
                for _ in range(MAX_REBUILDS + 1):
                    outcome = await self.runtime.run_segment(claim, profile)
                    if outcome.status != "REBUILD":
                        break
                    log(logger, logging.WARNING, "rebuild from HEAD", run=claim.run_id, detail=outcome.detail)
        finally:
            hb.cancel()
            with contextlib.suppress(BaseException):
                await hb
        last_attempt = job.attempts >= job.max_attempts
        result = await asyncio.to_thread(self._finish, claim, outcome, last_attempt)
        fault("worker_before_ack")
        if result == "RETRY":
            code = "".join(ch if ch.isalnum() else "_" for ch in (outcome.detail or "SEGMENT_ERROR").upper())[:60]
            await asyncio.to_thread(
                self.queue.nack, job, code, min_delay_s=5.0 if outcome.detail == "GATEWAY_UNAVAILABLE" else 0.0
            )
        else:
            await asyncio.to_thread(self.queue.ack, job, result)
        log(
            logger,
            logging.INFO,
            "segment done",
            run=claim.run_id,
            outcome=outcome.status,
            result=result,
            detail=outcome.detail,
        )
        return result

    def _stale_reason(self, p: Any) -> str | None:
        if datetime.fromisoformat(p.expires_at.replace("Z", "+00:00")) < datetime.now(UTC):
            return "STALE_EXPIRED"
        row = self.db.one(
            "SELECT r.status_code, t.goal_revision, s.restore_incarnation FROM agent.runs r JOIN agent.tasks t"
            " ON t.tenant_id=r.tenant_id AND t.id=r.task_id CROSS JOIN agent.schema_control s"
            " WHERE r.tenant_id=%s AND r.id=%s AND r.task_id=%s",
            (p.tenant_id, p.run_id, p.task_id),
        )
        if row is None:
            return "STALE_NOT_FOUND"
        if row["status_code"] in control.TERMINAL:
            return "STALE_TERMINAL"
        if p.restore_incarnation != row["restore_incarnation"]:
            return "STALE_RESTORE"
        if p.expected_goal_revision < row["goal_revision"]:
            return "STALE_GOAL_POINTER"  # a steer committed later and enqueued its own resume job
        return None

    def _profile(self, claim: control.Claim) -> str:
        row = self.db.one(
            "SELECT profile_code FROM agent.tasks WHERE tenant_id=%s AND id=%s", (claim.tenant_id, claim.task_id)
        )
        assert row is not None
        return str(row["profile_code"])

    def _finish(self, claim: control.Claim, o: SegmentOutcome, last_attempt: bool = False) -> str:
        fault("worker_before_finish")
        try:
            if o.status == "FINAL":
                return control.finish_segment(self.db, claim, "SUCCEEDED", result_text=o.text)
            if o.status == "AWAITING_APPROVAL":
                return control.finish_segment(self.db, claim, "WAITING_APPROVAL")
            if o.status == "UNKNOWN_EFFECT":
                return control.finish_segment(self.db, claim, "WAITING_RECONCILIATION", error_code="UNKNOWN_EFFECT")
            if o.status == "CANCELLED":
                return control.finish_segment(self.db, claim, "CANCELLED")
            if o.status == "PAUSED":
                return control.finish_segment(self.db, claim, "PAUSED")
            if o.status == "REVOKED":
                state = control.settle_stopped(self.db, claim.tenant_id, claim.run_id, revoked=True)
                return "RETRY" if state == "SCOPE_RESTORED" else state
            if o.status == "BUDGET":
                return control.finish_segment(self.db, claim, "BUDGET_EXHAUSTED", error_code="BUDGET_EXHAUSTED")
            if o.status == "STALEGOAL":
                control.release(self.db, claim)
                return "STALE_GOAL"
            if o.status == "FENCEDOUT":
                return "FENCED"
            if last_attempt:  # retries exhausted: the run must not stay RUNNING behind a dead-lettered job
                return control.finish_segment(self.db, claim, "FAILED", error_code=(o.detail or "ERROR")[:60])
            control.release(self.db, claim)
            return "RETRY"  # ERROR / REBUILD exhausted: queue retry with backoff
        except control.Cancelled:
            return "CANCELLED"
        except control.Paused:
            return control.finish_segment(self.db, claim, "PAUSED")
        except control.ScopeRevoked:
            return control.settle_stopped(self.db, claim.tenant_id, claim.run_id, revoked=True)
        except (control.FencedOut, control.RunTerminal):
            return "FENCED"
        except control.StaleGoal:
            control.release(self.db, claim)
            return "STALE_GOAL"

    async def _heartbeat(self, claim: control.Claim, job: Job) -> None:
        while True:
            await asyncio.sleep(self.lease_s / 3)
            ok_run = await asyncio.to_thread(control.heartbeat, self.db, claim, lease_s=self.lease_s)
            ok_job = await asyncio.to_thread(self.queue.heartbeat, job, self.lease_s)
            if not ok_run:
                log(logger, logging.WARNING, "lease lost", run=claim.run_id, job_alive=ok_job)
                return  # gateways refuse the stale fence at the next admission


def build_from_env() -> tuple[RuntimeWorker, Database, DeepAgentsRuntime]:
    keys = {k: bytes.fromhex(config.require_secret(f"AIAGENT_RUNTIME_{k.upper()}_RPC_KEY")) for k in ("model", "tool")}
    gws = Gateways(
        RpcClient(config.env("AIAGENT_MODEL_GATEWAY_ENDPOINT"), "runtime", keys["model"]),
        RpcClient(config.env("AIAGENT_TOOL_GATEWAY_ENDPOINT"), "runtime", keys["tool"]),
    )
    db = Database(config.require_secret("AIAGENT_RUNTIME_DSN"), max_size=16, name="runtime")
    rt = DeepAgentsRuntime(
        db,
        config.require_secret("AIAGENT_CHECKPOINTER_DSN"),
        gws,
        model=os.environ.get("AIAGENT_MODEL", "fake-gateway"),
        max_input_tokens=config.env_int("AIAGENT_MAX_INPUT_TOKENS", 100_000),
    )
    w = RuntimeWorker(
        db,
        rt,
        worker_id=os.environ.get("AIAGENT_WORKER_ID", f"{socket.gethostname()}:{os.getpid()}"),
        concurrency=config.env_int("AIAGENT_WORKER_CONCURRENCY", 2),
        lease_s=float(config.env_int("AIAGENT_LEASE_S", 30)),
    )
    return w, db, rt


async def main_async() -> None:
    setup("runtime_worker")
    config.require_isolation("runtime_worker")
    w, db, rt = build_from_env()
    with db.conn() as c:
        check_compatible(c)
    life = Lifecycle(
        "runtime_worker",
        health_port=config.env_int("AIAGENT_HEALTH_PORT", 0) or None,
        health_host=os.environ.get("AIAGENT_HEALTH_HOST", "127.0.0.1"),
    )
    life.install_signals()
    await rt.launch()
    await life.start_health()
    once = os.environ.get("AIAGENT_WORKER_ONCE") == "1"
    loop_task = asyncio.create_task(w.run(life.stopping, idle_exit=once))
    stop_task = asyncio.create_task(life.stopping.wait())
    await asyncio.wait({loop_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    if not loop_task.done():
        await w.drain(float(os.environ.get("AIAGENT_DRAIN_S", "25")))
        loop_task.cancel()
        with contextlib.suppress(BaseException):
            await loop_task
    stop_task.cancel()
    await rt.close()
    await life.close()
    db.close()


def main() -> None:
    import sys

    if sys.platform == "win32":  # psycopg async needs a selector loop on Windows (development only)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
