"""User/API commands on Task/Run/Approval. Each command is ONE transaction: authorization (tenant via RLS + owner
predicate), state CAS, domain event and outbox row. The API never runs agent work inside the request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from aiagent_domain import sha256_hex, uuid7
from psycopg.types.json import Jsonb

from .control import TERMINAL
from .db import Database, Row
from .events import append_event, enqueue_run_job

DEFAULT_MODELS = {"default": ["fake-gateway"], "sandbox": ["fake-gateway"], "anthropic": ["claude-sonnet-5-5"]}


class CommandError(Exception):
    def __init__(self, code: str, http: int = 409, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.http, self.detail = code, http, detail


@dataclass(frozen=True)
class Scope:
    """Authenticated context. Never built from request bodies or queue payloads."""

    tenant_id: str
    user_id: str
    workspace_id: str

    @property
    def actor(self) -> dict[str, str]:
        return {"type": "user", "id": self.user_id}


def _idem_begin(c: psycopg.Connection[Row], s: Scope, op: str, key: str | None, req: Any) -> Row | None:
    if key is None:
        return None
    h = sha256_hex(req)
    c.execute(
        "INSERT INTO agent.api_idempotency(tenant_id, actor_user_id, operation_name, operation_key, request_sha256,"
        " expires_at) VALUES (%s,%s,%s,%s,%s, now() + interval '1 day') ON CONFLICT DO NOTHING",
        (s.tenant_id, s.user_id, op, key, h),
    )
    row = c.execute(
        "SELECT request_sha256, response_json, completed_at FROM agent.api_idempotency WHERE tenant_id=%s"
        " AND actor_user_id=%s AND operation_name=%s AND operation_key=%s FOR UPDATE",
        (s.tenant_id, s.user_id, op, key),
    ).fetchone()
    assert row is not None
    if row["request_sha256"] != h:
        raise CommandError("IDEMPOTENCY_KEY_REUSED", 409)
    return row if row["completed_at"] is not None else None


def _idem_end(c: psycopg.Connection[Row], s: Scope, op: str, key: str | None, resp: dict[str, Any]) -> None:
    if key is not None:
        c.execute(
            "UPDATE agent.api_idempotency SET response_json=%s, response_http_status=201, completed_at=now()"
            " WHERE tenant_id=%s AND actor_user_id=%s AND operation_name=%s AND operation_key=%s",
            (Jsonb(resp), s.tenant_id, s.user_id, op, key),
        )


def create_task_tx(
    c: psycopg.Connection[Row],
    *,
    tenant_id: str,
    owner_user_id: str,
    workspace_id: str,
    goal: str,
    profile: str = "default",
    actor: dict[str, str],
    schedule_id: str | None = None,
    trigger_key: str | None = None,
    occurrence_id: str | None = None,
) -> dict[str, str]:
    """Task + goal revision 1 + Run attempt 1 + seed HEAD entry + events + run.start outbox, inside caller's TX."""
    task_id, run_id, cp_id = uuid7(), uuid7(), uuid7()
    c.execute(
        "INSERT INTO agent.tasks(tenant_id, id, owner_user_id, workspace_id, profile_code, status_code,"
        " created_from_schedule_id, trigger_key) VALUES (%s,%s,%s,%s,%s,'QUEUED',%s,%s)",
        (tenant_id, task_id, owner_user_id, workspace_id, profile, schedule_id, trigger_key),
    )
    c.execute(
        "INSERT INTO agent.task_goal_revisions(tenant_id, task_id, revision, goal_text, goal_sha256, created_by_user_id)"
        " VALUES (%s,%s,1,%s,%s,%s)",
        (tenant_id, task_id, goal, sha256_hex(goal), owner_user_id),
    )
    inc = c.execute("SELECT restore_incarnation FROM agent.schema_control").fetchone()
    assert inc is not None
    c.execute(
        "INSERT INTO agent.runs(tenant_id, id, task_id, attempt_no, input_goal_revision, status_code, allowed_models,"
        " restore_incarnation, head_revision, current_checkpoint_id) VALUES (%s,%s,%s,1,1,'CREATED',%s,%s,1,%s)",
        (
            tenant_id,
            run_id,
            task_id,
            DEFAULT_MODELS.get(profile, DEFAULT_MODELS["default"]),
            inc["restore_incarnation"],
            cp_id,
        ),
    )
    entry = {"kind": "user", "content": goal, "tool_calls": []}
    snap = sha256_hex({"prev": None, "rev": 1, "kind": "user", "stream": "main", "payload": entry})
    c.execute(
        "INSERT INTO agent.run_checkpoints(tenant_id, id, task_id, run_id, checkpoint_revision, stream, entry_kind,"
        " request_key, writer_ref, writer_lease_epoch, applied_goal_revision, checkpoint_json, snapshot_sha256)"
        " VALUES (%s,%s,%s,%s,1,'main','user','seed-1','api',0,1,%s,%s)",
        (tenant_id, cp_id, task_id, run_id, Jsonb(entry), snap),
    )
    c.execute("UPDATE agent.tasks SET active_run_id=%s WHERE tenant_id=%s AND id=%s", (run_id, tenant_id, task_id))
    append_event(
        c,
        tenant_id=tenant_id,
        aggregate_type="task",
        aggregate_id=task_id,
        kind="task.accepted",
        payload={"goalRevision": "1", "profile": profile},
        actor=actor,
        owner_user_id=owner_user_id,
        task_id=task_id,
    )
    ev = append_event(
        c,
        tenant_id=tenant_id,
        aggregate_type="run",
        aggregate_id=run_id,
        kind="run.created",
        payload={"attemptNo": 1},
        actor=actor,
        owner_user_id=owner_user_id,
        task_id=task_id,
        run_id=run_id,
    )
    enqueue_run_job(
        c,
        tenant_id=tenant_id,
        task_id=task_id,
        run_id=run_id,
        kind="run.start",
        goal_revision=1,
        dedupe_key=f"run:{run_id}:start",
        event_id=ev,
        schedule_occurrence_id=occurrence_id,
    )
    return {"taskId": task_id, "runId": run_id}


def submit_task(
    db: Database, s: Scope, goal: str, *, profile: str | None = None, idempotency_key: str | None = None
) -> dict[str, str]:
    profile = profile or "default"
    if profile not in DEFAULT_MODELS:
        raise CommandError("UNKNOWN_PROFILE", 422)
    with db.tx(tenant=s.tenant_id) as c:
        prior = _idem_begin(c, s, "task.submit", idempotency_key, {"goal": goal, "profile": profile})
        if prior is not None:
            return dict(prior["response_json"])
        ws = c.execute(
            "SELECT 1 FROM agent.workspaces w JOIN agent.tenant_memberships m ON m.tenant_id=w.tenant_id"
            " AND m.user_id=w.owner_user_id WHERE w.tenant_id=%s AND w.id=%s AND w.owner_user_id=%s"
            " AND w.disabled_at IS NULL AND m.disabled_at IS NULL",
            (s.tenant_id, s.workspace_id, s.user_id),
        ).fetchone()
        if ws is None:
            raise CommandError("FORBIDDEN", 403)
        out = create_task_tx(
            c,
            tenant_id=s.tenant_id,
            owner_user_id=s.user_id,
            workspace_id=s.workspace_id,
            goal=goal,
            profile=profile,
            actor=s.actor,
        )
        _idem_end(c, s, "task.submit", idempotency_key, out)
        return out


def _lock_owned(c: psycopg.Connection[Row], s: Scope, task_id: str) -> tuple[Row, Row]:
    task = c.execute(
        "SELECT * FROM agent.tasks WHERE tenant_id=%s AND id=%s AND owner_user_id=%s FOR UPDATE",
        (s.tenant_id, task_id, s.user_id),
    ).fetchone()
    if task is None:
        raise CommandError("NOT_FOUND", 404)  # other owners' tasks are indistinguishable from missing ones
    run = c.execute(
        "SELECT * FROM agent.runs WHERE tenant_id=%s AND id=%s FOR UPDATE", (s.tenant_id, task["active_run_id"])
    ).fetchone()
    assert run is not None
    return task, run


def steer(db: Database, s: Scope, task_id: str, goal: str, expected_goal_revision: int) -> dict[str, str]:
    """New immutable goal revision (CAS on goal_revision). Old approvals/intents/snapshots become stale; the active
    run gets the new goal as a HEAD user entry and a resume job. The worker's old claim is refused at its next
    admission (StaleGoal), never silently continued."""
    with db.tx(tenant=s.tenant_id) as c:
        task, run = _lock_owned(c, s, task_id)
        if task["status_code"] in TERMINAL or run["status_code"] in TERMINAL:
            raise CommandError("TERMINAL", 409)
        if run["cancel_requested"]:
            raise CommandError("CANCELLED", 409)
        if task["goal_revision"] != expected_goal_revision:
            raise CommandError("GOAL_REVISION_CONFLICT", 409)
        rev = task["goal_revision"] + 1
        c.execute(
            "INSERT INTO agent.task_goal_revisions(tenant_id, task_id, revision, goal_text, goal_sha256, created_by_user_id)"
            " VALUES (%s,%s,%s,%s,%s,%s)",
            (s.tenant_id, task_id, rev, goal, sha256_hex(goal), s.user_id),
        )
        c.execute(
            "UPDATE agent.tasks SET goal_revision=%s, record_version=record_version+1, updated_at=now()"
            " WHERE tenant_id=%s AND id=%s AND goal_revision=%s",
            (rev, s.tenant_id, task_id, expected_goal_revision),
        )
        # Pending approvals bound to the old goal can never be consumed (gateway re-checks goal too).
        c.execute(
            "UPDATE agent.approval_records SET decision_code='REVOKED', decided_at=now(), record_version=record_version+1,"
            " decided_by_json=%s WHERE tenant_id=%s AND run_id=%s AND decision_code IN ('PENDING','APPROVED')",
            (Jsonb({"type": "service", "id": "steer"}), s.tenant_id, run["id"]),
        )
        prev = c.execute(
            "SELECT snapshot_sha256 FROM agent.run_checkpoints WHERE tenant_id=%s AND run_id=%s AND checkpoint_revision=%s",
            (s.tenant_id, run["id"], run["head_revision"]),
        ).fetchone()
        entry = {"kind": "user", "content": f"[goal revised to revision {rev}] {goal}", "tool_calls": []}
        hrev = run["head_revision"] + 1
        cp = uuid7()
        c.execute(
            "INSERT INTO agent.run_checkpoints(tenant_id, id, task_id, run_id, checkpoint_revision, stream, entry_kind,"
            " request_key, writer_ref, writer_lease_epoch, applied_goal_revision, checkpoint_json, snapshot_sha256)"
            " VALUES (%s,%s,%s,%s,%s,'main','user',%s,'api',%s,%s,%s,%s)",
            (
                s.tenant_id,
                cp,
                task_id,
                run["id"],
                hrev,
                f"seed-{hrev}",
                run["lease_epoch"],
                rev,
                Jsonb(entry),
                sha256_hex(
                    {
                        "prev": prev["snapshot_sha256"] if prev else None,
                        "rev": hrev,
                        "kind": "user",
                        "stream": "main",
                        "payload": entry,
                    }
                ),
            ),
        )
        c.execute(
            "UPDATE agent.runs SET head_revision=%s, current_checkpoint_id=%s, updated_at=now()"
            " WHERE tenant_id=%s AND id=%s AND head_revision=%s",
            (hrev, cp, s.tenant_id, run["id"], run["head_revision"]),
        )
        ev = append_event(
            c,
            tenant_id=s.tenant_id,
            aggregate_type="task",
            aggregate_id=task_id,
            kind="task.goal_revised",
            payload={"goalRevision": str(rev)},
            actor=s.actor,
            owner_user_id=s.user_id,
            task_id=task_id,
            run_id=run["id"],
        )
        enqueue_run_job(
            c,
            tenant_id=s.tenant_id,
            task_id=task_id,
            run_id=run["id"],
            kind="run.resume",
            goal_revision=rev,
            dedupe_key=f"run:{run['id']}:steer:{rev}",
            event_id=ev,
        )
        return {"taskId": task_id, "goalRevision": str(rev)}


def cancel(db: Database, s: Scope, task_id: str) -> dict[str, str]:
    """Cancel is terminal for admission immediately (gateways refuse new model/tool work); effects that already
    passed final admission keep their ledger state. With SENDING/UNKNOWN effects the run goes to
    WAITING_RECONCILIATION instead of CANCELLED (01-03)."""
    with db.tx(tenant=s.tenant_id) as c:
        _task, run = _lock_owned(c, s, task_id)
        if run["status_code"] in TERMINAL:
            return {"taskId": task_id, "state": run["status_code"]}
        pending = c.execute(
            "SELECT count(*) AS n FROM agent.tool_intents WHERE tenant_id=%s AND run_id=%s"
            " AND status_code IN ('SENDING','UNKNOWN')",
            (s.tenant_id, run["id"]),
        ).fetchone()
        assert pending is not None
        live = c.execute(
            "SELECT coalesce(lease_expires_at > now(), false) AS live FROM agent.runs WHERE tenant_id=%s AND id=%s",
            (s.tenant_id, run["id"]),
        ).fetchone()
        leased = run["status_code"] in ("CLAIMED", "RUNNING") and bool(live and live["live"])
        if leased:
            target = "CANCELLING"
        elif pending["n"]:
            target = "WAITING_RECONCILIATION"
        else:
            target = "CANCELLED"
        c.execute(
            "UPDATE agent.runs SET status_code=%s, cancel_requested=true, ended_at = CASE WHEN %s THEN now() END,"
            " record_version=record_version+1, updated_at=now() WHERE tenant_id=%s AND id=%s",
            (target, target == "CANCELLED", s.tenant_id, run["id"]),
        )
        c.execute(
            "UPDATE agent.approval_records SET decision_code='REVOKED', decided_at=now(), record_version=record_version+1"
            " WHERE tenant_id=%s AND run_id=%s AND decision_code='PENDING'",
            (s.tenant_id, run["id"]),
        )
        tstate = {"CANCELLING": "CANCELLING", "WAITING_RECONCILIATION": "WAITING_RECONCILIATION"}.get(
            target, "CANCELLED"
        )
        c.execute(
            "UPDATE agent.tasks SET status_code=%s, record_version=record_version+1, updated_at=now()"
            " WHERE tenant_id=%s AND id=%s",
            (tstate, s.tenant_id, task_id),
        )
        append_event(
            c,
            tenant_id=s.tenant_id,
            aggregate_type="run",
            aggregate_id=run["id"],
            kind="run.state_changed",
            payload={"from": run["status_code"], "to": target, "cause": "user_cancel"},
            actor=s.actor,
            owner_user_id=s.user_id,
            task_id=task_id,
            run_id=run["id"],
        )
        return {"taskId": task_id, "state": target}


def pause(db: Database, s: Scope, task_id: str) -> dict[str, str]:
    with db.tx(tenant=s.tenant_id) as c:
        _task, run = _lock_owned(c, s, task_id)
        if run["status_code"] in TERMINAL or run["cancel_requested"]:
            raise CommandError("NOT_PAUSABLE", 409)
        target = (
            "PAUSED" if run["status_code"] in ("CREATED", "WAITING_APPROVAL", "WAITING_INPUT") else run["status_code"]
        )
        c.execute(
            "UPDATE agent.runs SET pause_requested=true, status_code=%s, record_version=record_version+1,"
            " updated_at=now() WHERE tenant_id=%s AND id=%s",
            (target, s.tenant_id, run["id"]),
        )
        if target == "PAUSED":
            c.execute(
                "UPDATE agent.tasks SET status_code='PAUSED', record_version=record_version+1, updated_at=now()"
                " WHERE tenant_id=%s AND id=%s",
                (s.tenant_id, task_id),
            )
        append_event(
            c,
            tenant_id=s.tenant_id,
            aggregate_type="run",
            aggregate_id=run["id"],
            kind="run.state_changed",
            payload={"from": run["status_code"], "to": target, "cause": "user_pause"},
            actor=s.actor,
            owner_user_id=s.user_id,
            task_id=task_id,
            run_id=run["id"],
        )
        return {"taskId": task_id, "state": target}


def resume(db: Database, s: Scope, task_id: str) -> dict[str, str]:
    with db.tx(tenant=s.tenant_id) as c:
        task, run = _lock_owned(c, s, task_id)
        if run["status_code"] in TERMINAL or run["cancel_requested"]:
            raise CommandError("NOT_RESUMABLE", 409)
        c.execute(
            "UPDATE agent.runs SET pause_requested=false, record_version=record_version+1, updated_at=now()"
            " WHERE tenant_id=%s AND id=%s",
            (s.tenant_id, run["id"]),
        )
        ev = append_event(
            c,
            tenant_id=s.tenant_id,
            aggregate_type="run",
            aggregate_id=run["id"],
            kind="run.state_changed",
            payload={"from": run["status_code"], "to": run["status_code"], "cause": "user_resume"},
            actor=s.actor,
            owner_user_id=s.user_id,
            task_id=task_id,
            run_id=run["id"],
        )
        enqueue_run_job(
            c,
            tenant_id=s.tenant_id,
            task_id=task_id,
            run_id=run["id"],
            kind="run.resume",
            goal_revision=task["goal_revision"],
            dedupe_key=f"run:{run['id']}:resume:{run['record_version']}",
            event_id=ev,
        )
        return {"taskId": task_id, "state": run["status_code"]}


def decide_approval(
    db: Database, s: Scope, approval_id: str, decision: str, request_sha256: str, expected_record_version: int
) -> dict[str, str]:
    """Owner decision bound to the frozen request hash and record version; the decision and the resume job are
    committed atomically (outbox). Self-approval by a model is impossible: only an authenticated owner reaches this."""
    if decision not in ("APPROVED", "REJECTED"):
        raise CommandError("BAD_DECISION", 422)
    with db.tx(tenant=s.tenant_id) as c:
        a = c.execute(
            "SELECT a.*, a.expires_at < now() AS expired, t.goal_revision AS task_goal FROM agent.approval_records a"
            " JOIN agent.tasks t ON t.tenant_id=a.tenant_id AND t.id=a.task_id"
            " WHERE a.tenant_id=%s AND a.id=%s AND a.owner_user_id=%s FOR UPDATE OF a",
            (s.tenant_id, approval_id, s.user_id),
        ).fetchone()
        if a is None:
            raise CommandError("NOT_FOUND", 404)
        if a["decision_code"] != "PENDING":
            raise CommandError("NOT_PENDING", 409)
        if a["record_version"] != expected_record_version:
            raise CommandError("VERSION_CONFLICT", 409)
        if a["request_sha256"] != request_sha256:
            raise CommandError("REQUEST_HASH_MISMATCH", 409)
        if a["expired"]:
            c.execute(
                "UPDATE agent.approval_records SET decision_code='EXPIRED', record_version=record_version+1"
                " WHERE tenant_id=%s AND id=%s",
                (s.tenant_id, approval_id),
            )
            raise CommandError("EXPIRED", 409)
        if a["goal_revision"] != a["task_goal"]:
            raise CommandError("STALE_GOAL", 409)
        c.execute(
            "UPDATE agent.approval_records SET decision_code=%s, decided_by_json=%s, decided_at=now(),"
            " record_version=record_version+1 WHERE tenant_id=%s AND id=%s AND record_version=%s",
            (decision, Jsonb(s.actor), s.tenant_id, approval_id, expected_record_version),
        )
        ev = append_event(
            c,
            tenant_id=s.tenant_id,
            aggregate_type="approval",
            aggregate_id=approval_id,
            kind="approval.decided",
            payload={"decision": decision},
            actor=s.actor,
            owner_user_id=s.user_id,
            task_id=a["task_id"],
            run_id=a["run_id"],
        )
        enqueue_run_job(
            c,
            tenant_id=s.tenant_id,
            task_id=a["task_id"],
            run_id=a["run_id"],
            kind="run.resume",
            goal_revision=a["goal_revision"],
            dedupe_key=f"run:{a['run_id']}:approval:{approval_id}",
            event_id=ev,
        )
        return {"approvalId": approval_id, "decision": decision}
