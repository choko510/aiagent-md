"""Operator commands: create per-process DB logins, provision tenant/user/workspace/API token, bump the restore
incarnation after a restore, retention GC. Run with the migration-owner DSN only.

python -m aiagent_core.admin logins            (passwords from AIAGENT_LOGIN_<ROLE>_PASSWORD_FILE)
python -m aiagent_core.admin tenant            (prints a fresh API token once; only its SHA-256 is stored)
python -m aiagent_core.admin restore-bump      (after PITR / failover: invalidates claims, pointers, permits)
python -m aiagent_core.admin gc --days 30
"""

from __future__ import annotations

import argparse
import secrets
from typing import Any

from psycopg import sql

from aiagent_domain import uuid7
from aiagent_domain.canonical import sha256_bytes

from .config import secret
from .db import connect

GROUPS = {
    "api": "aiagent_api", "runtime": "aiagent_runtime", "checkpointer": "aiagent_checkpointer",
    "model_gateway": "aiagent_model_gateway", "tool_gateway": "aiagent_tool_gateway", "relay": "aiagent_relay",
    "sentinel": "aiagent_sentinel", "ops": "aiagent_ops",
}


def create_logins(owner_dsn: str, passwords: dict[str, str], *, prefix: str = "aiagent_login_") -> dict[str, str]:
    """One LOGIN role per process, member of exactly one group. search_path pinned per role."""
    out = {}
    with connect(owner_dsn) as c:
        for short, group in GROUPS.items():
            if short not in passwords:
                continue
            login = f"{prefix}{short}"
            exists = c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (login,)).fetchone()
            verb = sql.SQL("ALTER") if exists else sql.SQL("CREATE")
            c.execute(sql.SQL("{} ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}").format(
                verb, sql.Identifier(login), sql.Literal(passwords[short])))
            c.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(group), sql.Identifier(login)))
            path = "lg_checkpoint" if short == "checkpointer" else "agent"
            c.execute(sql.SQL("ALTER ROLE {} SET search_path = {}").format(sql.Identifier(login), sql.Identifier(path)))
            out[short] = login
    return out


def provision_tenant(owner_dsn: str) -> dict[str, Any]:
    t, u, w = uuid7(), uuid7(), uuid7()
    token = "aiag_" + secrets.token_urlsafe(32)
    with connect(owner_dsn) as c, c.transaction():
        c.execute("INSERT INTO agent.tenants(id) VALUES (%s)", (t,))
        c.execute("INSERT INTO agent.users(id) VALUES (%s)", (u,))
        c.execute("INSERT INTO agent.tenant_memberships(tenant_id, user_id) VALUES (%s,%s)", (t, u))
        c.execute("INSERT INTO agent.workspaces(tenant_id, id, owner_user_id) VALUES (%s,%s,%s)", (t, w, u))
        c.execute("INSERT INTO agent.api_credentials(token_sha256, tenant_id, user_id, workspace_id) VALUES (%s,%s,%s,%s)",
                  (sha256_bytes(token.encode()), t, u, w))
    return {"tenantId": t, "userId": u, "workspaceId": w, "token": token}


def add_user(owner_dsn: str, tenant_id: str) -> dict[str, Any]:
    """Second owner in the same tenant (owner isolation tests / multi-user tenants)."""
    u, w = uuid7(), uuid7()
    token = "aiag_" + secrets.token_urlsafe(32)
    with connect(owner_dsn) as c, c.transaction():
        c.execute("INSERT INTO agent.users(id) VALUES (%s)", (u,))
        c.execute("INSERT INTO agent.tenant_memberships(tenant_id, user_id) VALUES (%s,%s)", (tenant_id, u))
        c.execute("INSERT INTO agent.workspaces(tenant_id, id, owner_user_id) VALUES (%s,%s,%s)", (tenant_id, w, u))
        c.execute("INSERT INTO agent.api_credentials(token_sha256, tenant_id, user_id, workspace_id) VALUES (%s,%s,%s,%s)",
                  (sha256_bytes(token.encode()), tenant_id, u, w))
    return {"tenantId": tenant_id, "userId": u, "workspaceId": w, "token": token}


def restore_bump(owner_dsn: str) -> int:
    """After PITR/failover: new incarnation; queue dispatch paused until an operator verifies and unpauses."""
    with connect(owner_dsn) as c:
        row = c.execute(
            "UPDATE agent.schema_control SET restore_incarnation = restore_incarnation + 1, dispatch_paused = true,"
            " changed_at = now() RETURNING restore_incarnation"
        ).fetchone()
        c.execute("UPDATE agent.egress_permits SET revoked_at = now() WHERE revoked_at IS NULL")
    assert row is not None
    return int(row["restore_incarnation"])


def adopt_runs_after_restore(owner_dsn: str) -> int:
    """Operator step after verifying a restore: non-terminal runs move to the new incarnation with their lease
    cleared; SENDING tool effects become UNKNOWN (they may or may not have happened) and are never auto-resent."""
    with connect(owner_dsn) as c, c.transaction():
        c.execute("UPDATE agent.tool_intents SET status_code='UNKNOWN', updated_at=now() WHERE status_code='SENDING'")
        c.execute("UPDATE agent.side_effect_records SET outcome_code='UNKNOWN', updated_at=now() WHERE outcome_code='SENDING'")
        cur = c.execute(
            "UPDATE agent.runs r SET restore_incarnation = s.restore_incarnation, lease_expires_at = NULL,"
            " framework_ref = NULL FROM agent.schema_control s WHERE r.status_code NOT IN"
            " ('SUCCEEDED','PARTIAL','FAILED','CANCELLED','EXPIRED','BUDGET_EXHAUSTED')"
        )
        c.execute("UPDATE agent.schema_control SET dispatch_paused = false, changed_at = now()")
        return cur.rowcount


def gc(owner_dsn: str, days: int) -> dict[str, int]:
    with connect(owner_dsn) as c:
        a = c.execute("SELECT lg_checkpoint.gc(make_interval(days => %s)) AS n", (days,)).fetchone()
        b = c.execute("SELECT agent.gc_terminal_runs(make_interval(days => %s)) AS n", (days,)).fetchone()
    assert a is not None and b is not None
    return {"framework_threads": int(a["n"]), "stream_chunks": int(b["n"])}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aiagent-admin")
    ap.add_argument("cmd", choices=["logins", "tenant", "restore-bump", "adopt-runs", "gc"])
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args(argv)
    s = secret("AIAGENT_MIGRATE_DSN")
    assert s is not None
    dsn = s.reveal()
    if a.cmd == "logins":
        pw = {}
        for short in GROUPS:
            v = secret(f"AIAGENT_LOGIN_{short.upper()}_PASSWORD", required=False)
            if v is not None:
                pw[short] = v.reveal()
        print("logins:", ", ".join(create_logins(dsn, pw).values()))
    elif a.cmd == "tenant":
        r = provision_tenant(dsn)
        print(f"tenant={r['tenantId']} user={r['userId']} workspace={r['workspaceId']}")
        print(f"token={r['token']}  (shown once)")
    elif a.cmd == "restore-bump":
        print("restore_incarnation:", restore_bump(dsn))
    elif a.cmd == "adopt-runs":
        print("adopted runs:", adopt_runs_after_restore(dsn))
    else:
        print(gc(dsn, a.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
