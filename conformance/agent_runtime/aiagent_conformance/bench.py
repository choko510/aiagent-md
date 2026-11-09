"""PYRT-T05 measurement: same fixture, N sequential runs per candidate, in a fresh database.

    python -m aiagent_conformance.bench --candidate a --python <candidate python> --pg <server url> -n 10

Per run = start segment (search -> approval pause) + approve + resume segment (send -> final).
Reports medians of worker peak RSS / process CPU seconds / boot / segment time, and DB writes split into the aiagent-md
schema and the framework schema(s): tuple inserts+updates+deletes and WAL bytes. Run alone on an
otherwise idle server; numbers are environment specific and only comparable within one invocation.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiagent_conformance.control import Control  # noqa: E402
from aiagent_conformance.fixtures import USER_TEXT  # noqa: E402

FRAMEWORK_SCHEMAS = {"a": ("dbos",), "b": ("public",)}


def tuple_writes(conn: psycopg.Connection, schemas: tuple[str, ...]) -> int:
    row = conn.execute(
        "SELECT coalesce(sum(n_tup_ins + n_tup_upd + n_tup_del), 0) FROM pg_stat_user_tables WHERE schemaname = ANY(%s)",
        (list(schemas),),
    ).fetchone()
    return int(row[0])


def rel_bytes(conn: psycopg.Connection, schemas: tuple[str, ...]) -> int:
    row = conn.execute(
        "SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = ANY(%s) AND c.relkind = 'r'",
        (list(schemas),),
    ).fetchone()
    return int(row[0])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--python", required=True)
    ap.add_argument("--pg", required=True)
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--mode", choices=["runtime", "queue"], default="runtime",
                    help="runtime: segments started directly; queue: every segment goes through the candidate's queue"
                         " (A: DBOS Queue, B: aiagent-md QueuePort) so queue costs are included")
    ap.add_argument("--out")
    a = ap.parse_args()
    db = f"bench_{a.candidate}_{a.mode}"
    with psycopg.connect(f"{a.pg}/postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {db}")
    url = f"{a.pg}/{db}"
    ctl = Control(url)
    ctl.reset()
    stat = psycopg.connect(url, autocommit=True)
    fw = FRAMEWORK_SCHEMAS[a.candidate]

    def worker(rid: str) -> dict:
        env = {**os.environ, "PYDANTIC_AI_NO_BANNER": "1"}
        extra = ["--via-queue"] if a.mode == "queue" else []
        p = subprocess.run([a.python, "-m", "aiagent_conformance.worker", "--candidate", a.candidate, "--db", url,
                            "--run", rid, "--policy", "notify", *extra], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        line = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
        assert p.returncode == 0 and line, p.stderr[-2000:]
        return json.loads(line[-1])

    # warm-up run (creates framework tables / migrations), excluded from the numbers
    rid = "warm"
    ctl.create_run(rid, USER_TEXT["notify"])
    worker(rid)
    [ctl.decide(x["approval_id"], "APPROVED") for x in ctl.approvals(rid)]
    worker(rid)
    time.sleep(1.0)

    samples = []
    for _ in range(a.n):
        rid = f"bench-{uuid.uuid4().hex[:8]}"
        ctl.create_run(rid, USER_TEXT["notify"])
        time.sleep(0.6)  # let stats from the previous worker land
        w0 = stat.execute("SELECT pg_current_wal_lsn()").fetchone()[0]
        t_fw0, t_ag0 = tuple_writes(stat, fw), tuple_writes(stat, ("aiagent",))
        s1 = worker(rid)
        [ctl.decide(x["approval_id"], "APPROVED") for x in ctl.approvals(rid)]
        s2 = worker(rid)
        assert s2["text"] == "done:CONFIRMED", s2
        time.sleep(0.6)
        wal = stat.execute("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), %s)", (w0,)).fetchone()[0]
        samples.append({
            "rss_mb": max(s1["peak_rss_mb"], s2["peak_rss_mb"]),
            "boot_s": (s1["t_boot_s"] + s2["t_boot_s"]) / 2,
            "segment_s": s1["t_segment_s"] + s2["t_segment_s"],
            "cpu_s": s1["cpu_s"] + s2["cpu_s"],
            "launch_s": s1["t_launch_s"] + s2["t_launch_s"],
            "wal_bytes": int(wal),
            "fw_tuple_writes": tuple_writes(stat, fw) - t_fw0,
            "aiagent_tuple_writes": tuple_writes(stat, ("aiagent",)) - t_ag0,
        })
    med = {k: round(statistics.median(s[k] for s in samples), 3) for k in samples[0]}
    result = {
        "candidate": a.candidate, "mode": a.mode, "n": a.n, "median": med,
        "min": {k: min(s[k] for s in samples) for k in samples[0]},
        "max": {k: max(s[k] for s in samples) for k in samples[0]},
        "framework_state_bytes_total": rel_bytes(stat, fw),
        "aiagent_state_bytes_total": rel_bytes(stat, ("aiagent",)),
        "runs_in_db": a.n + 1,
        "python": sys.version.split()[0], "platform": sys.platform,
    }
    print(json.dumps(result, indent=2))
    if a.out:
        Path(a.out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
