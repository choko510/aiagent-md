#!/usr/bin/env python3
"""Read-only inspection of a separately restored, QUARANTINED PostgreSQL instance.

Never restores a database, rotates keys, resumes jobs, contacts egress, or changes state.
This is one REC safety check, not evidence of successful PITR or external reconciliation.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import urlsplit

ENV = "AIAGENT_HV_RESTORE_DSN"


def safety_checks(snapshot: dict[str, int | bool]) -> dict[str, object]:
    conditions = {
        "restore_incarnation_bumped": snapshot.get("incarnation", 0) > 1,
        "dispatch_paused": snapshot.get("dispatch_paused") is True,
        "queue_all_paused": snapshot.get("active_queues", 1) == 0,
        "no_unrevoked_permits": snapshot.get("unrevoked_permits", 1) == 0,
        "no_sending_intents": snapshot.get("sending_intents", 1) == 0,
        "no_sending_effects": snapshot.get("sending_effects", 1) == 0,
        "no_live_run_leases": snapshot.get("live_leases", 1) == 0,
    }
    return {
        "kind": "restore_quarantine_inspection",
        "status": "QUARANTINE_READONLY_PASS" if all(conditions.values()) else "BLOCKED",
        "checks": conditions,
        "unknownEffectsRequireReconcile": snapshot.get("unknown_effects", 0),
        "note": "A passing snapshot does not prove actual PITR, secret revocation or absence of external effects.",
    }


def query_readonly(dsn: str) -> dict[str, int | bool]:
    # psycopg is an already pinned backend dependency; do not log the DSN.
    import psycopg
    from psycopg.rows import dict_row

    data: dict[str, int | bool] = {}
    with psycopg.connect(
        dsn, connect_timeout=5, row_factory=dict_row,
        options="-c default_transaction_read_only=on -c statement_timeout=5000",
    ) as c:
        if c.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            raise RuntimeError("connection unexpectedly started in a transaction")
        with c.transaction():
            read_only = c.execute("SHOW transaction_read_only").fetchone()
            if not read_only or read_only["transaction_read_only"] != "on":
                raise RuntimeError("DB not in read-only mode")
            row = c.execute(
                "SELECT restore_incarnation, dispatch_paused FROM agent.schema_control WHERE singleton"
            ).fetchone()
            if not row:
                raise RuntimeError("schema_control absent")
            data["incarnation"] = int(row["restore_incarnation"])
            data["dispatch_paused"] = row["dispatch_paused"] is True
            queries = {
                "active_queues": "SELECT count(*) AS n FROM queue.queues WHERE paused IS NOT TRUE",
                "unrevoked_permits": "SELECT count(*) AS n FROM agent.egress_permits WHERE revoked_at IS NULL",
                "sending_intents": "SELECT count(*) AS n FROM agent.tool_intents WHERE status_code='SENDING'",
                "sending_effects": "SELECT count(*) AS n FROM agent.side_effect_records WHERE outcome_code='SENDING'",
                "live_leases": "SELECT count(*) AS n FROM agent.runs WHERE lease_expires_at > now()",
                "unknown_effects": "SELECT count(*) AS n FROM agent.side_effect_records WHERE outcome_code='UNKNOWN'",
            }
            for name, sql in queries.items():
                data[name] = int(c.execute(sql).fetchone()["n"])
    return data


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inspect", action="store_true", help="Explicit read-only query; default is NOT_RUN")
    a = p.parse_args(argv)
    dsn = os.environ.get(ENV, "")
    try:
        uri = urlsplit(dsn)
        # Prevent touching remote/prod hosts; use an SSH tunnel on localhost if needed.
        safe = (uri.scheme in ("postgresql", "postgres") and uri.hostname in ("localhost", "127.0.0.1", "::1")
                and uri.path == "/aiagent_hv_restored_quarantine")
    except ValueError:
        safe = False
    if not a.inspect or not safe:
        print(json.dumps({"status": "NOT_RUN",
                          "reason": "requires --inspect and a loopback isolated DB named aiagent_hv_restored_quarantine"}))
        return 2 if a.inspect else 0
    try:
        result = safety_checks(query_readonly(dsn))
    except Exception:
        # No DSN, SQL exception detail, host identifiers or secret values in stderr/stdout.
        print(json.dumps({"status": "BLOCKED", "reason": "read-only quarantine query failed (details redacted)"}))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "QUARANTINE_READONLY_PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
