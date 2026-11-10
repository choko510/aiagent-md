#!/usr/bin/env python3
"""Bounded JQ-T12 smoke / dedicated load gate. Explicitly destructive to SCRATCH databases only.

The existing backend/tools/bench_queue.py creates + drops its own database. This wrapper
never passes the database URL on the OS command line or prints it in errors.
It never runs on CI by default and cannot identify a production database reliably:
the operator MUST provide a separately prepared disposable PostgreSQL cluster.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

BACKEND = Path(__file__).resolve().parents[2] / "backend"
ENV_VAR = "AIAGENT_HV_DISPOSABLE_PG_URL"
MARKER = "aiagent_hv_disposable"
MAX_SMOKE = 10_000
MAX_DEDICATED = 1_000_000
CHILD = """
import os, runpy, sys
sys.argv = ['bench_queue.py', '--pg', os.environ['AIAGENT_HV_DISPOSABLE_PG_URL'],
            '-n', sys.argv[1], '--relays', sys.argv[2], '--consumers', sys.argv[3],
            '--outage', sys.argv[4]]
runpy.run_path('tools/bench_queue.py', run_name='__main__')
"""


def safe_target(url: str) -> bool:
    """Restrict writes to a named disposable DB on loopback (dedicated VM may be SSH-tunneled)."""
    try:
        u = urlsplit(url)
        return (u.scheme in ("postgresql", "postgres") and u.hostname in ("127.0.0.1", "::1", "localhost")
                and u.path == "/" + MARKER and bool(u.username) and bool(u.password)
                and u.port is not None and 1 <= u.port <= 65535)
    except (TypeError, ValueError):
        return False


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rows", type=int, default=1000)
    p.add_argument("--relays", type=int, default=2)
    p.add_argument("--consumers", type=int, default=2)
    p.add_argument("--outage", type=float, default=2.0)
    p.add_argument("--max-seconds", type=int, default=180)
    p.add_argument("--dedicated-host", action="store_true",
                   help="Only for the dedicated 1M JQ-T12 environment; not Windows/WSL daily driver")
    p.add_argument("--execute", action="store_true", help="Default is a read-only DRY RUN")
    p.add_argument("--ack", default="", help="Type ALLOW_DISPOSABLE_DB_WRITE after independent target review")
    return p.parse_args(argv)


def validate_limits(a: argparse.Namespace) -> list[str]:
    problems = []
    limit = MAX_DEDICATED if a.dedicated_host else MAX_SMOKE
    if not 1 <= a.rows <= limit:
        problems.append("rows outside bounded mode")
    if not 1 <= a.relays <= 8 or not 1 <= a.consumers <= 8:
        problems.append("worker count outside 1..8")
    if not 0 <= a.outage <= 30:
        problems.append("outage must be 0..30 seconds")
    if not 30 <= a.max_seconds <= 3600:
        problems.append("max-seconds must be 30..3600")
    if a.dedicated_host and a.rows > MAX_SMOKE and a.max_seconds < 1800:
        problems.append("large benchmark needs explicit longer wall-clock bound")
    return problems


def plan(a: argparse.Namespace, url: str | None) -> dict[str, object]:
    issues = validate_limits(a)
    if not safe_target(url or ""):
        issues.append("missing or unsafe disposable database URL (loopback and aiagent_hv_disposable required)")
    if a.execute and (a.ack != "ALLOW_DISPOSABLE_DB_WRITE"
                      or os.getenv("AIAGENT_HV_ALLOW_WRITE") != "YES"):
        issues.append("operator dual-confirmation missing")
    return {
        "kind": "bounded_queue_benchmark", "mode": "DEDICATED" if a.dedicated_host else "SMOKE",
        "rows": a.rows, "relays": a.relays, "consumers": a.consumers,
        "maxSeconds": a.max_seconds, "execute": a.execute, "target": "REDACTED",
        "status": "BLOCKED" if issues else ("READY_TO_EXECUTE" if a.execute else "DRY_RUN"),
        "blockers": issues,
        "warning": "Existing bench creates and drops an ephemeral DB; failure/timeout may leave a scratch DB.",
    }


def main(argv: list[str] | None = None) -> int:
    a = arguments(argv)
    target = os.environ.get(ENV_VAR)
    result = plan(a, target)
    if not a.execute or result["blockers"]:
        print(json.dumps(result, indent=2))
        return 2 if a.execute or validate_limits(a) else 0
    # No secret in argv, stdout, JSON report, or exception text.
    env = {**os.environ}
    try:
        done = subprocess.run(
            [sys.executable, "-c", CHILD, str(a.rows), str(a.relays), str(a.consumers), str(a.outage)],
            cwd=BACKEND, env=env, capture_output=True, text=True,
            timeout=a.max_seconds, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(json.dumps({"status": "NOT_RUN", "reason": "execution failed or timed out; inspect scratch DB manually"}))
        return 2
    if done.returncode != 0:
        print(json.dumps({"status": "FAILED", "reason": "benchmark process failed; stderr intentionally redacted"}))
        return 2
    try:
        metrics = json.loads(done.stdout)
    except ValueError:
        print(json.dumps({"status": "FAILED", "reason": "unparseable or incomplete benchmark result"}))
        return 2
    required = {"rows", "acked", "duplicate_acks", "outbox_pending_after", "max_db_connections", "wal_mb"}
    success = (isinstance(metrics, dict) and required <= metrics.keys()
               and metrics["rows"] == a.rows + 1 and metrics["acked"] == a.rows + 1
               and metrics["duplicate_acks"] == 0 and metrics["outbox_pending_after"] == 0
               and metrics["max_db_connections"] <= 40)
    # Avoid exposing free-form output from the child. Metrics are fixed known-number keys.
    public = {k: metrics.get(k) for k in (
        "rows", "acked", "duplicate_acks", "outbox_pending_after", "max_db_connections",
        "wal_mb", "elapsed_s", "latency_p50_s", "latency_p95_s", "latency_p99_s",
    )} if isinstance(metrics, dict) else {}
    print(json.dumps({"status": "PASS" if success else "FAILED", "metrics": public,
                      "evidenceClass": "LOCAL_BOUNDED_LOAD_NOT_PRODUCTION_KVM"}, indent=2))
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
