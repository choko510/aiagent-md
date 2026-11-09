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

from aiagent_conformance.control import Control, FencedOut  # noqa: E402
from aiagent_conformance.gateways import ModelGateway, ToolGateway  # noqa: E402


def load(candidate: str, ctl: Control, mgw: ModelGateway, tgw: ToolGateway, db: str, args: argparse.Namespace):
    if candidate == "a":
        from candidates.candidate_a import CandidateA

        return CandidateA(ctl, mgw, tgw, db, parallel_mode=args.parallel_mode, compaction=args.compaction, executor_id=args.owner)
    from candidates.candidate_b import CandidateB

    return CandidateB(ctl, mgw, tgw, db, parallel=args.parallel_mode != "sequential", compaction=args.compaction)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--policy", default="notify")
    ap.add_argument("--parallel-mode", default="sequential")
    ap.add_argument("--compaction", action="store_true")
    ap.add_argument("--naive", action="store_true")
    ap.add_argument("--lease", type=float, default=2.0)
    ap.add_argument("--via-queue", action="store_true")
    ap.add_argument("--stale-claim", action="store_true", help="keep using the claim taken before a Steer")
    args = ap.parse_args()
    args.owner = f"worker-{os.getpid()}"
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

    t0 = time.perf_counter()
    ctl = Control(args.db)
    mgw, tgw = ModelGateway(ctl), ToolGateway(ctl)
    tgw.naive = args.naive
    claim = None
    deadline = time.time() + 15
    while claim is None:
        try:
            claim = ctl.claim(args.run, args.owner, lease_s=args.lease)
        except FencedOut:
            if time.time() > deadline:
                raise
            time.sleep(0.25)
    if args.stale_claim:
        ctl.steer(args.run)  # goal changes after this worker claimed: everything it does is stale
    adapter = load(args.candidate, ctl, mgw, tgw, args.db, args)
    t_launch = time.perf_counter()
    await adapter.launch()
    t_seg = time.perf_counter()
    queue_info: dict[str, object] = {}
    job = None
    if args.via_queue:
        if hasattr(adapter, "enqueue"):  # candidate brings its own durable queue (DBOS)
            queue_info = await adapter.enqueue(claim, args.policy)
        else:  # aiagent-md QueuePort fixture: Postgres FOR UPDATE SKIP LOCKED
            first = ctl.enqueue(args.run, dedupe_key=args.run)
            dup = ctl.enqueue(args.run, dedupe_key=args.run)
            job = ctl.dequeue(args.owner, lease_s=args.lease)
            queue_info = {"enqueued": first, "deduplicated": int(not dup), "attempts": job and job["attempts"]}
    rebuilds: list[str] = []
    while True:
        out = await adapter.run_segment(claim, args.policy)
        if out.status == "REBUILD" and len(rebuilds) < 2:
            rebuilds.append(out.detail or "")
            continue
        break
    t_end = time.perf_counter()
    if job is not None:
        queue_info["acked"] = ctl.ack(job["job_id"], args.owner)
    cpu = -1.0
    try:
        import psutil

        ct = psutil.Process().cpu_times()
        cpu = ct.user + ct.system

        rss = psutil.Process().memory_info().peak_wset / 2**20 if os.name == "nt" else 0.0
        if os.name != "nt":
            import resource

            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:  # pragma: no cover
        rss = -1.0
    if out.status == "FINAL":
        ctl.set_status(claim, "SUCCEEDED")
    elif out.status == "AWAITING_APPROVAL":
        ctl.set_status(claim, "WAITING_APPROVAL")
    elif out.status == "UNKNOWN_EFFECT":
        ctl.set_status(claim, "NEEDS_RECONCILE")
    print(json.dumps({
        "status": out.status, "text": out.text, "approval_ids": out.approval_ids, "detail": out.detail,
        "rebuilds": rebuilds, "queue": queue_info, "peak_rss_mb": round(rss, 1), "cpu_s": round(cpu, 3),
        "t_boot_s": round(t_launch - t0, 3), "t_launch_s": round(t_seg - t_launch, 3), "t_segment_s": round(t_end - t_seg, 3),
    }), flush=True)
    await adapter.close()
    os._exit(0)  # do not let framework background threads delay exit


if __name__ == "__main__":
    if os.name == "nt":  # psycopg async needs a selector loop on Windows
        import selectors

        asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
    else:
        asyncio.run(main())
