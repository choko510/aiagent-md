"""JQ-T12-style load measurement for the PostgreSQL QueuePort + Outbox relay (ADR-039 evidence).

Seeds N outbox rows (real Task/Run/Event/Outbox transactions are too slow to seed at scale, so rows are inserted in
bulk against one seeded event), then runs R relay threads and C consumer threads; the queue is paused for
`--outage` seconds in the middle (JQ-T13 behaviour under load). Reports throughput, end-to-end latency percentiles,
max connections used, WAL bytes, duplicates (must be 0 domain-level duplicates: each job acked exactly once).

  uv run python tools/bench_queue.py --pg postgresql://postgres:pw@127.0.0.1:55432/postgres -n 50000
Numbers are environment-specific (laptop / Docker Desktop); they are not an SLO.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from aiagent_core import admin, commands, migrate
from aiagent_core.commands import Scope
from aiagent_core.db import Database, connect
from aiagent_core.queue import QueuePort
from aiagent_core.relay import OutboxRelay


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg", required=True)
    ap.add_argument("-n", type=int, default=50000)
    ap.add_argument("--relays", type=int, default=8)
    ap.add_argument("--consumers", type=int, default=8)
    ap.add_argument("--outage", type=float, default=5.0)
    a = ap.parse_args()
    name = f"aiagent_bench_{secrets.token_hex(3)}"
    with connect(a.pg) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    from conftest import _with_db  # reuse DSN helper

    owner = _with_db(a.pg, name)
    try:
        migrate.up(owner)
        pw = secrets.token_hex(8)
        pre = f"aiagent_b{secrets.token_hex(2)}_"
        logins = admin.create_logins(owner, {"relay": pw, "runtime": pw, "api": pw}, prefix=pre)
        dsn = {k: _with_db(a.pg, name, v, pw) for k, v in logins.items()}
        t = admin.provision_tenant(owner)
        ids = commands.submit_task(Database(dsn["api"]), Scope(t["tenantId"], t["userId"], t["workspaceId"]), "seed")
        od = Database(owner, max_size=2)
        od.one(
            "UPDATE queue.queues SET per_tenant_active_limit=1000, global_active_limit=1000 WHERE name='run' RETURNING 1"
        )
        tmpl = od.one("SELECT * FROM agent.outbox_messages LIMIT 1")
        assert tmpl is not None
        t0 = time.time()
        with od.conn() as c:
            c.execute(
                "INSERT INTO agent.outbox_messages(tenant_id, id, topic, dedupe_key, event_id, safe_payload_json,"
                " payload_sha256, restore_incarnation) SELECT tenant_id, gen_random_uuid(), 'run', 'bench:' || g,"
                " event_id, safe_payload_json, payload_sha256, restore_incarnation FROM agent.outbox_messages,"
                " generate_series(1, %s) g WHERE id = %s",
                (a.n, tmpl["id"]),
            )
            wal0 = c.execute("SELECT pg_current_wal_lsn() AS l").fetchone()["l"]  # type: ignore[index]
        seed_s = time.time() - t0
        done: Counter[int] = Counter()
        lat: list[float] = []
        lock = threading.Lock()
        stop = threading.Event()
        max_conns = [0]

        def relay_loop(i: int) -> None:
            r = OutboxRelay(Database(dsn["relay"], max_size=2), f"r{i}", batch=100)
            while not stop.is_set():
                if r.run_once() == 0:
                    time.sleep(0.05)

        def consume_loop(i: int) -> None:
            q = QueuePort(Database(dsn["runtime"], max_size=2))
            while not stop.is_set():
                j = q.dequeue("run", f"c{i}")
                if j is None:
                    time.sleep(0.02)
                    continue
                if q.ack(j):
                    with lock:
                        done[j.id] += 1

        def watch() -> None:
            while not stop.is_set():
                row = od.one("SELECT count(*) n FROM pg_stat_activity WHERE datname=%s", (name,))
                max_conns[0] = max(max_conns[0], int(row["n"]))  # type: ignore[index]
                time.sleep(0.5)

        start = time.time()
        threads = [threading.Thread(target=relay_loop, args=(i,)) for i in range(a.relays)]
        threads += [threading.Thread(target=consume_loop, args=(i,)) for i in range(a.consumers)]
        threads.append(threading.Thread(target=watch))
        for th in threads:
            th.start()
        outage_at = None
        while True:
            time.sleep(0.5)
            with lock:
                n = len(done)
            if outage_at is None and n >= a.n // 3:
                QueuePort(od).set_paused("run", True)
                outage_at = time.time()
                time.sleep(a.outage)
                QueuePort(od).set_paused("run", False)
            if n >= a.n + 1 or time.time() - start > 1800:
                break
        stop.set()
        for th in threads:
            th.join()
        elapsed = time.time() - start
        rows = od.all(
            "SELECT extract(epoch FROM (j.finished_at - o.created_at)) AS s FROM queue.jobs j JOIN agent.outbox_messages o"
            " ON j.dedupe_key = 'outbox:' || o.id::text WHERE j.status='DONE'"
        )
        lat = sorted(float(r["s"]) for r in rows)
        with od.conn() as c:
            wal = c.execute("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), %s) AS b", (wal0,)).fetchone()["b"]  # type: ignore[index]
        pending = od.one("SELECT count(*) n FROM agent.outbox_messages WHERE dispatched_at IS NULL")["n"]  # type: ignore[index]

        def pct(p: float) -> float:
            return round(lat[min(len(lat) - 1, int(p * len(lat)))], 3) if lat else 0.0

        report = {
            "rows": a.n + 1,
            "relays": a.relays,
            "consumers": a.consumers,
            "seed_s": round(seed_s, 2),
            "elapsed_s": round(elapsed, 2),
            "acked": len(done),
            "duplicate_acks": sum(1 for v in done.values() if v > 1),
            "outbox_pending_after": pending,
            "throughput_per_s": round(len(done) / elapsed, 1),
            "latency_p50_s": pct(0.5),
            "latency_p95_s": pct(0.95),
            "latency_p99_s": pct(0.99),
            "queue_outage_s": a.outage,
            "max_db_connections": max_conns[0],
            "wal_mb": round(float(wal) / 1e6, 1),
        }
        print(json.dumps(report, indent=2))
        _ = ids
        return 0 if report["duplicate_acks"] == 0 and pending == 0 and len(done) == a.n + 1 else 1
    finally:
        with connect(a.pg) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


if __name__ == "__main__":
    raise SystemExit(main())
