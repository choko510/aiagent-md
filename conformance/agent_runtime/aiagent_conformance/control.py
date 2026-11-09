"""aiagent-md side of the AgentRuntimePort boundary (framework independent).

Owns: Run claim/lease fence, Goal revision, Checkpoint HEAD (append-only, CAS on head_seq),
Approval records. Candidates A/B never write these tables directly; they go through `Control`.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SCHEMA_SQL = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")


class FencedOut(Exception):
    """The caller's fence token is stale: another worker owns the run."""


class StaleGoal(Exception):
    """The caller acts on an old Goal revision."""


class Cancelled(Exception):
    """The run was cancelled by the user/control plane."""


@dataclass(frozen=True)
class Claim:
    run_id: str
    fence: int
    goal_revision: int
    owner: str
    root_run_id: str
    tenant_id: str = "t1"


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()[:32]


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)


class Control:
    """One autocommit connection *per thread*: concurrent runs (asyncio tasks + to_thread workers) must never
    share a connection, or one run's statements could execute inside another run's transaction."""

    def __init__(self, url: str):
        self.url = url
        self._local = threading.local()

    @property
    def db(self) -> psycopg.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.closed:
            conn = self._local.conn = connect(self.url)
        return conn

    # ---------- setup ----------
    def install(self) -> None:
        self.db.execute(SCHEMA_SQL)

    def reset(self) -> None:
        self.db.execute("DROP SCHEMA IF EXISTS aiagent CASCADE")
        self.install()

    # ---------- runs ----------
    def create_run(self, run_id: str, user_text: str, history: list[dict[str, Any]] = (), tenant_id: str = "t1",  # type: ignore[assignment]
                   policy: str = "notify") -> None:
        entries = [*history, {"kind": "user", "content": user_text}]
        with self.db.transaction():
            self.db.execute("INSERT INTO aiagent.runs(run_id, tenant_id, policy) VALUES (%s,%s,%s)", (run_id, tenant_id, policy))
            for i, e in enumerate(entries, start=1):
                self.db.execute(
                    "INSERT INTO aiagent.head_entries(run_id, seq, kind, request_key, payload, goal_revision, fence)"
                    " VALUES (%s, %s, %s, %s, %s, 1, 0)",
                    (run_id, i, e["kind"], f"seed-{i}" if e["kind"] == "assistant" else None, Jsonb({"tool_calls": [], **e})),
                )
            self.db.execute("UPDATE aiagent.runs SET head_seq = %s WHERE run_id = %s", (len(entries), run_id))

    def claim(self, run_id: str, owner: str, lease_s: float = 30.0, force: bool = False) -> Claim:
        cond = "" if force else " AND (lease_until IS NULL OR lease_until < now() OR lease_owner = %(o)s)"
        row = self.db.execute(
            "UPDATE aiagent.runs SET fence = fence + 1, lease_owner = %(o)s,"
            " lease_until = now() + make_interval(secs => %(s)s), status = 'RUNNING'"
            " WHERE run_id = %(r)s AND status <> 'CANCELLED'" + cond + " RETURNING fence, goal_revision, tenant_id",
            {"o": owner, "s": lease_s, "r": run_id},
        ).fetchone()
        if row is None:
            if (r := self.run(run_id)) is not None and r["status"] == "CANCELLED":
                raise Cancelled(run_id)
            raise FencedOut(f"run {run_id} is leased by another worker")
        root = run_id.split("/")[0]
        return Claim(run_id, row["fence"], row["goal_revision"], owner, root, row["tenant_id"])

    def run(self, run_id: str) -> dict[str, Any]:
        return self.db.execute("SELECT * FROM aiagent.runs WHERE run_id = %s", (run_id,)).fetchone()

    def set_status(self, claim: Claim, status: str) -> None:
        cur = self.db.execute(
            "UPDATE aiagent.runs SET status = %s WHERE run_id = %s AND fence = %s",
            (status, claim.run_id, claim.fence),
        )
        if cur.rowcount != 1:
            raise FencedOut(claim.run_id)

    def set_framework_ref(self, claim: Claim, ref: str) -> None:
        self.db.execute(
            "UPDATE aiagent.runs SET framework_ref = %s WHERE run_id = %s AND fence = %s",
            (ref, claim.run_id, claim.fence),
        )

    def steer(self, run_id: str) -> int:
        """User changed the goal: bump Goal revision. Old snapshots/intents become stale."""
        return self.db.execute(
            "UPDATE aiagent.runs SET goal_revision = goal_revision + 1 WHERE run_id = %s RETURNING goal_revision",
            (run_id,),
        ).fetchone()["goal_revision"]

    def cancel(self, run_id: str) -> None:
        """User cancel: terminal for admission. In-flight sends that already passed final admission stay as they are."""
        self.db.execute("UPDATE aiagent.runs SET status = 'CANCELLED' WHERE run_id = %s", (run_id,))

    def check_current(self, claim: Claim) -> None:
        row = self.run(claim.root_run_id)
        if row["fence"] != claim.fence:
            raise FencedOut(claim.run_id)
        if row["goal_revision"] != claim.goal_revision:
            raise StaleGoal(claim.run_id)

    # ---------- checkpoint HEAD ----------
    def head(self, run_id: str, stream: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM aiagent.head_entries WHERE run_id = %s"
        args: tuple[Any, ...] = (run_id,)
        if stream is not None:
            q += " AND stream = %s"
            args = (run_id, stream)
        return self.db.execute(q + " ORDER BY seq", args).fetchall()

    def find_assistant(self, run_id: str, stream: str, request_key: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT payload FROM aiagent.head_entries WHERE run_id=%s AND stream=%s AND request_key=%s AND kind='assistant'",
            (run_id, stream, request_key),
        ).fetchone()
        return row["payload"] if row else None

    def find_tool(self, run_id: str, tool_call_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT payload FROM aiagent.head_entries WHERE run_id=%s AND tool_call_id=%s AND kind='tool'",
            (run_id, tool_call_id),
        ).fetchone()
        return row["payload"] if row else None

    def find_proposal(self, run_id: str, tool_call_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT tc FROM aiagent.head_entries, jsonb_array_elements(payload->'tool_calls') tc"
            " WHERE run_id=%s AND kind='assistant' AND tc->>'id' = %s",
            (run_id, tool_call_id),
        ).fetchone()
        return row["tc"] if row else None

    def append_head(
        self,
        claim: Claim,
        kind: str,
        payload: dict[str, Any],
        *,
        stream: str = "main",
        request_key: str | None = None,
        tool_call_id: str | None = None,
    ) -> int:
        """CAS-append one immutable entry. Fails closed on stale fence / goal revision."""
        with self.db.transaction():
            row = self.db.execute(
                "SELECT fence, goal_revision, head_seq FROM aiagent.runs WHERE run_id = %s FOR UPDATE",
                (claim.root_run_id,),
            ).fetchone()
            if row["fence"] != claim.fence:
                raise FencedOut(claim.run_id)
            if row["goal_revision"] != claim.goal_revision and kind == "assistant":
                raise StaleGoal(claim.run_id)
            seq = row["head_seq"] + 1
            ins = self.db.execute(
                "INSERT INTO aiagent.head_entries(run_id, seq, kind, stream, request_key, tool_call_id, payload, goal_revision, fence)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (claim.root_run_id, seq, kind, stream, request_key, tool_call_id, Jsonb(payload), claim.goal_revision, claim.fence),
            )
            if ins.rowcount == 1:
                self.db.execute("UPDATE aiagent.runs SET head_seq = %s WHERE run_id = %s", (seq, claim.root_run_id))
                return seq
            return row["head_seq"]

    def transcript(self, run_id: str, stream: str = "main") -> list[dict[str, Any]]:
        return [e["payload"] for e in self.head(run_id, stream) if e["kind"] in ("user", "assistant", "tool")]

    # ---------- approvals (human side; CHAT_03/08 own the real UI) ----------
    def approvals(self, run_id: str) -> list[dict[str, Any]]:
        return self.db.execute(
            "SELECT * FROM aiagent.approvals WHERE run_id = %s ORDER BY approval_id", (run_id,)
        ).fetchall()

    def decide(self, approval_id: str, status: str, actor: str = "user:owner") -> None:
        assert status in ("APPROVED", "REJECTED")
        self.db.execute(
            "UPDATE aiagent.approvals SET status=%s, decided_by=%s WHERE approval_id=%s AND status='PENDING'",
            (status, actor, approval_id),
        )

    def revoke(self, approval_id: str) -> None:
        self.db.execute("UPDATE aiagent.approvals SET status='REVOKED' WHERE approval_id=%s", (approval_id,))

    def expire(self, approval_id: str) -> None:
        self.db.execute(
            "UPDATE aiagent.approvals SET expires_at = now() - interval '1 second' WHERE approval_id = %s",
            (approval_id,),
        )

    # ---------- QueuePort fixture (FOR UPDATE SKIP LOCKED + attempt fence) ----------
    def enqueue(self, run_id: str, dedupe_key: str, kind: str = "start") -> bool:
        cur = self.db.execute(
            "INSERT INTO aiagent.jobs(dedupe_key, run_id, kind) VALUES (%s,%s,%s) ON CONFLICT (dedupe_key) DO NOTHING",
            (dedupe_key, run_id, kind),
        )
        return cur.rowcount == 1

    def dequeue(self, worker: str, lease_s: float = 30.0, run_id: str | None = None) -> dict[str, Any] | None:
        """Claim one job. Every claim mints a new attempt_id; an expired lease is re-delivered with attempts+1."""
        only = " AND run_id = %(r)s" if run_id else ""
        return self.db.execute(
            "UPDATE aiagent.jobs SET status='CLAIMED', attempts=attempts+1, attempt_id=%(a)s, locked_by=%(w)s,"
            " locked_until=now()+make_interval(secs=>%(s)s) WHERE job_id = ("
            "  SELECT job_id FROM aiagent.jobs WHERE (status='READY' OR (status='CLAIMED' AND locked_until < now()))" + only +
            "  ORDER BY job_id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *",
            {"w": worker, "s": lease_s, "a": uuid.uuid4().hex, "r": run_id},
        ).fetchone()

    def ack(self, job: dict[str, Any], outcome: str = "DONE") -> bool:
        """Complete only if this attempt still owns the job (attempt fence). A stale attempt's ACK is a no-op."""
        cur = self.db.execute(
            "UPDATE aiagent.jobs SET status=%s, locked_until=NULL WHERE job_id=%s AND attempt_id=%s AND status='CLAIMED'",
            (outcome, job["job_id"], job["attempt_id"]),
        )
        return cur.rowcount == 1

    def decide_and_enqueue(self, approval_id: str, status: str, actor: str = "user:owner") -> bool:
        """Approval decision and the resume job are committed atomically (outbox-style)."""
        with self.db.transaction():
            row = self.db.execute(
                "UPDATE aiagent.approvals SET status=%s, decided_by=%s WHERE approval_id=%s AND status='PENDING' RETURNING run_id",
                (status, actor, approval_id),
            ).fetchone()
            if row is None:
                return False
            self.db.execute(
                "INSERT INTO aiagent.jobs(dedupe_key, run_id, kind) VALUES (%s,%s,'resume') ON CONFLICT (dedupe_key) DO NOTHING",
                (f"{row['run_id']}:resume:{approval_id}", row["run_id"]),
            )
        return True

    # ---------- observation ----------
    def count(self, table: str, where: str = "TRUE", args: tuple[Any, ...] = ()) -> int:
        return self.db.execute(f"SELECT count(*) AS n FROM aiagent.{table} WHERE {where}", args).fetchone()["n"]

    def ledger(self, run_id: str) -> list[dict[str, Any]]:
        return self.db.execute(
            "SELECT * FROM aiagent.tool_ledger WHERE root_run_id = %s ORDER BY intent_key", (run_id,)
        ).fetchall()


def race(point: str, url: str) -> None:
    """Deterministic concurrency injection: at `point`, a *separate* connection (= another process from the
    database's point of view) commits a Steer / Cancel / Revoke. AIAGENT_RACE="<point>:<action>[,...]"."""
    for spec in os.environ.get("AIAGENT_RACE", "").split(","):
        if not spec or spec.rsplit(":", 1)[0] != point:
            continue
        action = spec.rsplit(":", 1)[1]
        other = Control(url)
        run_id = os.environ["AIAGENT_RACE_RUN"]
        if action == "steer":
            other.steer(run_id)
        elif action == "cancel":
            other.cancel(run_id)
        elif action == "revoke":
            for a in other.approvals(run_id):
                other.revoke(a["approval_id"])
        other.db.close()
        os.environ["AIAGENT_RACE"] = ",".join(x for x in os.environ["AIAGENT_RACE"].split(",") if x != spec)  # once


def fault(point: str) -> None:
    """Fault injection: kill this worker process abruptly (no cleanup, no ACK) at a named point.

    AIAGENT_FAULT is a comma separated list of points. `os._exit` mimics SIGKILL/OOM: no finally
    blocks, no framework shutdown hooks, open transactions roll back on the server side.
    """
    if point in os.environ.get("AIAGENT_FAULT", "").split(","):
        os._exit(137)
