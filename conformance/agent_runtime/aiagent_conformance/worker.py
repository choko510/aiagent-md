"""Worker process entry point. Each invocation = one Agent Runtime Worker process lifetime.

    python -m aiagent_conformance.worker --candidate a|b --db URL --run RUN --policy notify

Fault injection via AIAGENT_FAULT (see control.fault). Prints one JSON line with the outcome and
process measurements (peak RSS, wall time).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiagent_conformance.control import Cancelled, Control, FencedOut, RunTerminal, StaleGoal, fault, race  # noqa: E402
from aiagent_conformance.gateways import ModelGateway, ToolGateway  # noqa: E402


def load(candidate: str, ctl: Control, mgw: ModelGateway, tgw: ToolGateway, db: str, args: argparse.Namespace):
    if candidate == "a":
        from candidates.candidate_a import CandidateA

        return CandidateA(ctl, mgw, tgw, db, parallel_mode=args.parallel_mode, compaction=args.compaction, executor_id=args.owner)
    from candidates.candidate_b import CandidateB

    return CandidateB(ctl, mgw, tgw, db, parallel=args.parallel_mode != "sequential", compaction=args.compaction)


def claim_with_wait(ctl: Control, run_id: str, owner: str, lease: float, wait_s: float = 15.0):
    deadline = time.time() + wait_s
    while True:
        try:
            return ctl.claim(run_id, owner, lease_s=lease)
        except FencedOut:
            if time.time() > deadline:
                raise
            time.sleep(0.25)


async def drive(adapter, claim, policy: str):
    rebuilds: list[str] = []
    while True:
        out = await adapter.run_segment(claim, policy)
        if out.status == "REBUILD" and len(rebuilds) < 2:
            rebuilds.append(out.detail or "")
            continue
        return out, rebuilds


def record_status(ctl: Control, claim, out) -> bool:
    """Returns whether the run status transition was accepted. CANCELLED / FENCEDOUT / STALEGOAL outcomes,
    a terminal run (e.g. cancelled while this segment finished) or a newer owner all leave the run untouched."""
    status = {"FINAL": "SUCCEEDED", "AWAITING_APPROVAL": "WAITING_APPROVAL", "UNKNOWN_EFFECT": "NEEDS_RECONCILE"}.get(out.status)
    if not status:
        return False
    race("before_record_status", ctl.url)
    try:
        ctl.set_status(claim, status)
        return True
    except (FencedOut, StaleGoal, RunTerminal):
        return False


def proc_stats() -> dict[str, float]:
    cpu, rss = -1.0, -1.0
    try:
        import psutil

        ct = psutil.Process().cpu_times()
        cpu = ct.user + ct.system
        if os.name == "nt":
            rss = psutil.Process().memory_info().peak_wset / 2**20
        else:
            import resource

            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:  # pragma: no cover
        pass
    return {"peak_rss_mb": round(rss, 1), "cpu_s": round(cpu, 3)}


async def queue_worker(adapter, ctl: Control, args: argparse.Namespace) -> list[dict[str, object]]:
    """Generic QueuePort consumer: claim job (attempt fence) -> claim run (lease fence) -> segment -> ACK."""
    done: list[dict[str, object]] = []
    idle_until = time.time() + args.idle
    while time.time() < idle_until:
        job = ctl.dequeue(args.owner, lease_s=args.lease)
        if job is None:
            await asyncio.sleep(0.1)
            continue
        idle_until = time.time() + args.idle
        try:
            claim = claim_with_wait(ctl, job["run_id"], args.owner, args.lease, wait_s=5)
        except RunTerminal as e:  # cancelled, or already finished (e.g. redelivered after a lost ACK)
            ctl.ack(job, "DONE")
            done.append({"job": job["job_id"], "run": job["run_id"], "status": "CANCELLED" if isinstance(e, Cancelled) else "TERMINAL"})
            continue
        policy = ctl.run(job["run_id"])["policy"]
        out, rebuilds = await drive(adapter, claim, policy)
        recorded = record_status(ctl, claim, out)
        fault("before_job_ack")  # segment finished, run state committed, ACK lost
        outcome = "WAITING" if out.status in ("AWAITING_APPROVAL", "UNKNOWN_EFFECT") else "DONE"
        acked = ctl.ack(job, outcome)
        done.append({"job": job["job_id"], "run": job["run_id"], "kind": job["kind"], "attempts": job["attempts"],
                     "status": out.status, "acked": acked, "recorded": recorded, "rebuilds": len(rebuilds)})
    return done


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--run", default="", help="one run id, or a comma list executed concurrently in this process")
    ap.add_argument("--policy", default="notify", help="policy, or a comma list matching --run")
    ap.add_argument("--parallel-mode", default="sequential")
    ap.add_argument("--compaction", action="store_true")
    ap.add_argument("--naive", action="store_true")
    ap.add_argument("--lease", type=float, default=2.0)
    ap.add_argument("--via-queue", action="store_true")
    ap.add_argument("--queue-worker", action="store_true", help="consume aiagent-md QueuePort jobs until idle")
    ap.add_argument("--idle", type=float, default=1.5)
    ap.add_argument("--stale-claim", action="store_true", help="keep using the claim taken before a Steer")
    args = ap.parse_args()
    args.owner = f"worker-{os.getpid()}"
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

    t0 = time.perf_counter()
    ctl = Control(args.db)
    mgw, tgw = ModelGateway(ctl), ToolGateway(ctl)
    tgw.naive = args.naive

    if args.queue_worker:
        adapter = load(args.candidate, ctl, mgw, tgw, args.db, args)
        await adapter.launch()
        jobs = await queue_worker(adapter, ctl, args)
        print(json.dumps({"jobs": jobs, **proc_stats()}), flush=True)
        await adapter.close()
        os._exit(0)

    runs = args.run.split(",")
    policies = args.policy.split(",")
    policies = policies * len(runs) if len(policies) == 1 else policies
    try:
        claims = [claim_with_wait(ctl, r, args.owner, args.lease) for r in runs]
    except RunTerminal as e:
        print(json.dumps({"status": "CANCELLED" if isinstance(e, Cancelled) else "TERMINAL", "text": None,
                          "approval_ids": [], "detail": "claim refused", "rebuilds": []}), flush=True)
        os._exit(0)
    if args.stale_claim:
        ctl.steer(runs[0])  # goal changes after this worker claimed: everything it does is stale
    adapter = load(args.candidate, ctl, mgw, tgw, args.db, args)
    t_launch = time.perf_counter()
    await adapter.launch()
    t_seg = time.perf_counter()
    queue_info: dict[str, object] = {}
    job = None
    if args.via_queue:
        if hasattr(adapter, "enqueue"):  # candidate brings its own durable queue (DBOS)
            queue_info = await adapter.enqueue(claims[0], policies[0])
        else:  # aiagent-md QueuePort fixture: Postgres FOR UPDATE SKIP LOCKED
            seg_key = f"{runs[0]}:seg{ctl.run(runs[0])['head_seq']}"  # one job per segment of the run
            first = ctl.enqueue(runs[0], dedupe_key=seg_key)
            dup = ctl.enqueue(runs[0], dedupe_key=seg_key)
            job = ctl.dequeue(args.owner, lease_s=args.lease, run_id=runs[0])
            queue_info = {"enqueued": first, "deduplicated": int(not dup), "attempts": job and job["attempts"]}
    results = await asyncio.gather(*(drive(adapter, c, p) for c, p in zip(claims, policies)))
    t_end = time.perf_counter()
    recorded = [record_status(ctl, c, out) for c, (out, _) in zip(claims, results)]
    if job is not None:
        queue_info["acked"] = ctl.ack(job, "WAITING" if results[0][0].status == "AWAITING_APPROVAL" else "DONE")
    stats = proc_stats()
    timing = {"t_boot_s": round(t_launch - t0, 3), "t_launch_s": round(t_seg - t_launch, 3), "t_segment_s": round(t_end - t_seg, 3)}

    def one(out, rebuilds, rec) -> dict[str, object]:
        return {"status": out.status, "text": out.text, "approval_ids": out.approval_ids, "detail": out.detail,
                "rebuilds": rebuilds, "recorded": rec}

    if len(runs) == 1:
        print(json.dumps({**one(*results[0], recorded[0]), "queue": queue_info, **stats, **timing}), flush=True)
    else:
        print(json.dumps({"runs": {r: one(*res, rec) for r, res, rec in zip(runs, results, recorded)}, **stats, **timing}), flush=True)
    await adapter.close()
    os._exit(0)  # do not let framework background threads delay exit


if __name__ == "__main__":
    if os.name == "nt":  # psycopg async needs a selector loop on Windows
        import selectors

        asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
    else:
        asyncio.run(main())
