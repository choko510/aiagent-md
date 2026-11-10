"""Run control plane (CHAT_01/02, 01-03 state machine): claim / heartbeat / state / Checkpoint HEAD.

Every state change is ONE conditional UPDATE (or a short transaction holding row locks in the fixed order
task -> run). A zero-row CAS is never treated as success. Invariants kept from STEP 2 (PR #80):
  * a stale worker (old lease_epoch) cannot change state, append to HEAD or move framework_ref;
  * Cancel committed first wins: a finishing worker never turns CANCELLED/CANCELLING into SUCCEEDED;
  * work on an old Goal revision is refused (assistant entries / terminal success need the current goal);
  * HEAD is append-only with CAS on head_revision; entries are hash-chained (tamper evidence);
  * framework_ref is discarded only if it is still the pointer this claim used;
  * tenant/owner boundaries: every statement is tenant-predicated;
  * restore incarnation: claims minted before a restore are refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from aiagent_domain import TERMINAL_RUN_STATES, sha256_hex, uuid7
from aiagent_domain.generated import RunClaimV1
from psycopg.types.json import Jsonb

from .db import Database, Row
from .events import append_event

TERMINAL = tuple(sorted(TERMINAL_RUN_STATES))
CLAIMABLE = ("CREATED", "CLAIMED", "RUNNING", "WAITING_APPROVAL", "WAITING_INPUT", "PAUSED", "CANCELLING")
SERVICE_ACTOR = {"type": "service", "id": "runtime"}


class ControlError(Exception):
    code = "CONTROL"


class FencedOut(ControlError):
    code = "FENCED"


class StaleGoal(ControlError):
    code = "STALE_GOAL"


class RunTerminal(ControlError):
    code = "TERMINAL"


class Cancelled(RunTerminal):
    code = "CANCELLED"


class RestoreFenced(ControlError):
    code = "RESTORE_FENCED"


class NotClaimable(ControlError):
    code = "NOT_CLAIMABLE"


class TenantMismatch(ControlError):
    code = "TENANT"


class ScopeRevoked(ControlError):
    code = "SCOPE_REVOKED"


class Paused(ControlError):
    code = "PAUSED"


def scope_active(c: psycopg.Connection[Row], tenant_id: str, owner_id: str, workspace_id: str) -> bool:
    """Current authority, not the membership snapshot at task creation."""
    return (
        c.execute(
            "SELECT 1 FROM agent.tenants t"
            " JOIN agent.tenant_memberships m ON m.tenant_id=t.id AND m.user_id=%s"
            " JOIN agent.users u ON u.id=m.user_id"
            " JOIN agent.workspaces w ON w.tenant_id=t.id AND w.owner_user_id=m.user_id AND w.id=%s"
            " WHERE t.id=%s AND t.disabled_at IS NULL AND u.disabled_at IS NULL"
            " AND m.disabled_at IS NULL AND w.disabled_at IS NULL",
            (owner_id, workspace_id, tenant_id),
        ).fetchone()
        is not None
    )


@dataclass(frozen=True)
class Claim:
    tenant_id: str
    owner_user_id: str
    task_id: str
    run_id: str
    lease_epoch: int
    goal_revision: int
    worker_ref: str
    restore_incarnation: int

    def to_wire(self) -> dict[str, Any]:
        return RunClaimV1.model_validate(
            {
                "schemaVersion": "1.0",
                "tenantId": self.tenant_id,
                "ownerUserId": self.owner_user_id,
                "taskId": self.task_id,
                "runId": self.run_id,
                "leaseEpoch": str(self.lease_epoch),
                "goalRevision": str(self.goal_revision),
                "workerRef": self.worker_ref,
                "restoreIncarnation": str(self.restore_incarnation),
            }
        ).model_dump(mode="json")

    @classmethod
    def from_wire(cls, data: Any) -> Claim:
        m = RunClaimV1.model_validate(data)
        return cls(
            m.tenant_id,
            m.owner_user_id,
            m.task_id,
            m.run_id,
            m.lease_epoch,
            m.goal_revision,
            m.worker_ref,
            m.restore_incarnation,
        )


# ------------------------------------------------------------------------------------------------ claims
def claim_run(
    db: Database,
    tenant_id: str,
    run_id: str,
    worker: str,
    *,
    lease_s: float = 30.0,
    expected_incarnation: int | None = None,
) -> Claim:
    """Fenced claim: lease_epoch += 1 under row locks (task, then run). Same run id and attempt on reclaim."""
    with db.tx() as c:
        inc = c.execute("SELECT restore_incarnation, dispatch_paused FROM agent.schema_control").fetchone()
        assert inc is not None
        if expected_incarnation is not None and expected_incarnation != inc["restore_incarnation"]:
            raise RestoreFenced(run_id)
        if inc["dispatch_paused"]:
            raise NotClaimable("dispatch paused (schema/queue upgrade)")
        run = c.execute("SELECT task_id FROM agent.runs WHERE tenant_id=%s AND id=%s", (tenant_id, run_id)).fetchone()
        if run is None:
            raise TenantMismatch(run_id)
        task = c.execute(
            "SELECT id, owner_user_id, workspace_id, goal_revision, active_run_id, status_code FROM agent.tasks"
            " WHERE tenant_id=%s AND id=%s FOR UPDATE",
            (tenant_id, run["task_id"]),
        ).fetchone()
        r = c.execute(
            "SELECT status_code, lease_epoch, lease_owner_ref, lease_expires_at < now() AS expired,"
            " lease_expires_at IS NULL AS free, cancel_requested, pause_requested, restore_incarnation"
            " FROM agent.runs WHERE tenant_id=%s AND id=%s FOR UPDATE",
            (tenant_id, run_id),
        ).fetchone()
        assert task is not None and r is not None
        if not scope_active(c, tenant_id, str(task["owner_user_id"]), str(task["workspace_id"])):
            raise ScopeRevoked(run_id)
        if r["status_code"] in TERMINAL:
            raise (Cancelled if r["status_code"] == "CANCELLED" else RunTerminal)(r["status_code"])
        if task["active_run_id"] != run_id:
            raise NotClaimable("run is not the task's active run")
        if r["status_code"] not in CLAIMABLE:
            raise NotClaimable(r["status_code"])
        # A committed Cancel must be settled even when Pause remains requested.
        # In particular, a dead worker must never leave CANCELLING orphaned.
        if r["cancel_requested"] or r["status_code"] == "CANCELLING":
            raise NotClaimable("cancelling")
        if r["pause_requested"]:
            raise NotClaimable("paused")
        if r["restore_incarnation"] != inc["restore_incarnation"]:
            raise RestoreFenced(run_id)
        if not (r["free"] or r["expired"]):
            raise FencedOut(f"run {run_id} is leased by another worker")
        new_status = "CANCELLING" if r["status_code"] == "CANCELLING" or r["cancel_requested"] else "CLAIMED"
        row = c.execute(
            "UPDATE agent.runs SET lease_epoch = lease_epoch + 1, lease_owner_ref=%s,"
            " lease_expires_at = now() + make_interval(secs => %s), heartbeat_at = now(), status_code=%s,"
            " started_at = coalesce(started_at, now()), record_version = record_version + 1, updated_at = now()"
            " WHERE tenant_id=%s AND id=%s AND lease_epoch=%s RETURNING lease_epoch",
            (worker, lease_s, new_status, tenant_id, run_id, r["lease_epoch"]),
        ).fetchone()
        if row is None:
            raise FencedOut(run_id)
        if new_status == "CLAIMED" and task["status_code"] not in ("RUNNING", "CANCELLING"):
            c.execute(
                "UPDATE agent.tasks SET status_code='RUNNING', record_version=record_version+1, updated_at=now()"
                " WHERE tenant_id=%s AND id=%s",
                (tenant_id, task["id"]),
            )
        append_event(
            c,
            tenant_id=tenant_id,
            aggregate_type="run",
            aggregate_id=run_id,
            kind="run.claimed",
            payload={"leaseEpoch": str(row["lease_epoch"]), "previousState": r["status_code"]},
            actor=SERVICE_ACTOR,
            owner_user_id=task["owner_user_id"],
            task_id=task["id"],
            run_id=run_id,
        )
        return Claim(
            tenant_id,
            task["owner_user_id"],
            task["id"],
            run_id,
            row["lease_epoch"],
            task["goal_revision"],
            worker,
            inc["restore_incarnation"],
        )


def mark_running(db: Database, claim: Claim) -> None:
    """CLAIMED -> RUNNING after framework state was loaded and ownership re-checked."""
    with db.conn() as c:
        cur = c.execute(
            "UPDATE agent.runs SET status_code='RUNNING', updated_at=now() WHERE tenant_id=%s AND id=%s"
            " AND lease_epoch=%s AND status_code IN ('CLAIMED','RUNNING') AND NOT cancel_requested AND NOT pause_requested",
            (claim.tenant_id, claim.run_id, claim.lease_epoch),
        )
        if cur.rowcount != 1:
            _raise_for(c, claim)


def heartbeat(db: Database, claim: Claim, *, lease_s: float = 30.0) -> bool:
    """Extend the lease only while this claim is current. False = fenced out (stop work at the next boundary)."""
    with db.conn() as c:
        cur = c.execute(
            "UPDATE agent.runs SET lease_expires_at = now() + make_interval(secs => %s), heartbeat_at = now()"
            " WHERE tenant_id=%s AND id=%s AND lease_epoch=%s AND lease_owner_ref=%s AND status_code <> ALL(%s)",
            (lease_s, claim.tenant_id, claim.run_id, claim.lease_epoch, claim.worker_ref, list(TERMINAL)),
        )
        return cur.rowcount == 1


def release(db: Database, claim: Claim) -> bool:
    """Give the lease up early (e.g. after STALE_GOAL) so the resume job can claim without waiting for expiry."""
    with db.conn() as c:
        cur = c.execute(
            "UPDATE agent.runs SET lease_expires_at = now() - interval '1 millisecond'"
            " WHERE tenant_id=%s AND id=%s AND lease_epoch=%s",
            (claim.tenant_id, claim.run_id, claim.lease_epoch),
        )
        return cur.rowcount == 1


def _raise_for(c: psycopg.Connection[Row], claim: Claim) -> None:
    row = c.execute(
        "SELECT r.status_code, r.lease_epoch, r.cancel_requested, r.pause_requested, t.goal_revision FROM agent.runs r"
        " JOIN agent.tasks t ON t.tenant_id=r.tenant_id AND t.id=r.task_id WHERE r.tenant_id=%s AND r.id=%s",
        (claim.tenant_id, claim.run_id),
    ).fetchone()
    if row is None:
        raise TenantMismatch(claim.run_id)
    if row["status_code"] == "CANCELLED" or row["cancel_requested"] or row["status_code"] == "CANCELLING":
        raise Cancelled(claim.run_id)
    if row["status_code"] in TERMINAL:
        raise RunTerminal(row["status_code"])
    if row["lease_epoch"] != claim.lease_epoch:
        raise FencedOut(claim.run_id)
    if row["goal_revision"] != claim.goal_revision:
        raise StaleGoal(claim.run_id)
    if row["pause_requested"] or row["status_code"] == "PAUSED":
        raise Paused(claim.run_id)
    raise NotClaimable(row["status_code"])


def check_current(
    c: psycopg.Connection[Row], claim: Claim, *, for_update: bool = False, allow_cancelling: bool = False
) -> Row:
    """Gateway admission core: tenant, fence, goal, cancel, terminal and restore incarnation."""
    row = c.execute(
        "SELECT r.status_code, r.lease_epoch, r.cancel_requested, r.pause_requested, r.restore_incarnation,"
        " r.head_revision, r.allowed_models, r.token_budget, r.tokens_reserved, r.tokens_used, r.max_model_calls,"
        " r.model_calls, t.goal_revision, t.owner_user_id, t.workspace_id, s.restore_incarnation AS current_incarnation"
        " FROM agent.runs r JOIN agent.tasks t ON t.tenant_id=r.tenant_id AND t.id=r.task_id"
        " CROSS JOIN agent.schema_control s"
        " WHERE r.tenant_id=%s AND r.id=%s AND r.task_id=%s" + (" FOR UPDATE OF r" if for_update else ""),
        (claim.tenant_id, claim.run_id, claim.task_id),
    ).fetchone()
    if row is None or row["owner_user_id"] != claim.owner_user_id:
        raise TenantMismatch(claim.run_id)
    if not scope_active(c, claim.tenant_id, claim.owner_user_id, str(row["workspace_id"])):
        raise ScopeRevoked(claim.run_id)
    if row["pause_requested"] or row["status_code"] == "PAUSED":
        raise Paused(claim.run_id)
    if (
        claim.restore_incarnation != row["current_incarnation"]
        or row["restore_incarnation"] != row["current_incarnation"]
    ):
        raise RestoreFenced(claim.run_id)
    if row["status_code"] == "CANCELLED" or (
        not allow_cancelling and (row["cancel_requested"] or row["status_code"] == "CANCELLING")
    ):
        raise Cancelled(claim.run_id)
    if row["status_code"] in TERMINAL:
        raise RunTerminal(row["status_code"])
    if row["lease_epoch"] != claim.lease_epoch:
        raise FencedOut(claim.run_id)
    if row["goal_revision"] != claim.goal_revision:
        raise StaleGoal(claim.run_id)
    return row


def _has_unresolved_effects(c: psycopg.Connection[Row], tenant_id: str, run_id: str) -> bool:
    """Canonical unresolved Tool Intent predicate for both segment and re-delivery exits."""
    return (
        c.execute(
            "SELECT 1 FROM agent.tool_intents WHERE tenant_id=%s AND run_id=%s"
            " AND status_code IN ('SENDING','UNKNOWN') LIMIT 1",
            (tenant_id, run_id),
        ).fetchone()
        is not None
    )


def settle_stopped(db: Database, tenant_id: str, run_id: str, *, revoked: bool) -> str:
    """Settle a paused/revoked run when no claim could be minted.

    A transient queue pointer is not authority. Revocation is terminal for new
    work, except for effects that need reconciliation. The task and run state,
    audit event, and lease fencing are committed atomically.
    """
    with db.tx() as c:
        pointer = c.execute(
            "SELECT task_id FROM agent.runs WHERE tenant_id=%s AND id=%s",
            (tenant_id, run_id),
        ).fetchone()
        if pointer is None:
            return "STALE_NOT_FOUND"
        task = c.execute(
            "SELECT id, owner_user_id, workspace_id, status_code FROM agent.tasks"
            " WHERE tenant_id=%s AND id=%s FOR UPDATE",
            (tenant_id, pointer["task_id"]),
        ).fetchone()
        run = c.execute(
            "SELECT status_code, pause_requested, cancel_requested,"
            " coalesce(lease_expires_at > now(), false) AS leased FROM agent.runs"
            " WHERE tenant_id=%s AND id=%s FOR UPDATE",
            (tenant_id, run_id),
        ).fetchone()
        if task is None or run is None:
            return "STALE_NOT_FOUND"
        if run["status_code"] in TERMINAL:
            return str(run["status_code"])
        cancelling = bool(run["cancel_requested"] or run["status_code"] == "CANCELLING")
        revoked_now = not scope_active(c, tenant_id, str(task["owner_user_id"]), str(task["workspace_id"]))
        if revoked and not revoked_now and not cancelling:
            return "SCOPE_RESTORED"
        if not cancelling and not revoked_now and not run["pause_requested"]:
            return "PAUSE_CLEARED"
        pending = _has_unresolved_effects(c, tenant_id, run_id)
        if cancelling or revoked_now:
            target = "WAITING_RECONCILIATION" if pending else "CANCELLED"
        else:
            target = "WAITING_RECONCILIATION" if pending or run["status_code"] == "WAITING_RECONCILIATION" else "PAUSED"
        error = "SCOPE_REVOKED" if revoked_now else None
        if run["status_code"] == target and not run["leased"] and (not revoked_now or run["cancel_requested"]):
            return target
        c.execute(
            "UPDATE agent.runs SET status_code=%s, error_code=%s,"
            " cancel_requested=cancel_requested OR %s, lease_epoch=lease_epoch+1,"
            " lease_expires_at=now()-interval '1 millisecond',"
            " ended_at=CASE WHEN %s THEN now() ELSE ended_at END,"
            " record_version=record_version+1, updated_at=now()"
            " WHERE tenant_id=%s AND id=%s",
            (target, error, revoked_now, target in TERMINAL, tenant_id, run_id),
        )
        if task["status_code"] not in TERMINAL:
            c.execute(
                "UPDATE agent.tasks SET status_code=%s, record_version=record_version+1,"
                " updated_at=now() WHERE tenant_id=%s AND id=%s",
                (target, tenant_id, task["id"]),
            )
        # Approval rows remain exclusively writable by the API/Tool Gateway.
        # Disabled scope and fenced lease reject further admission regardless
        # of any formerly approved or pending decision.
        append_event(
            c,
            tenant_id=tenant_id,
            aggregate_type="run",
            aggregate_id=run_id,
            kind="run.state_changed",
            payload={
                "from": run["status_code"],
                "to": target,
                "cause": error or ("cancel_at_admission" if cancelling else "pause_at_admission"),
            },
            actor=SERVICE_ACTOR,
            owner_user_id=task["owner_user_id"],
            task_id=task["id"],
            run_id=run_id,
        )
        return target


# ------------------------------------------------------------------------------------------------ HEAD
def append_head(
    c: psycopg.Connection[Row],
    claim: Claim,
    kind: str,
    payload: dict[str, Any],
    *,
    writer: str,
    stream: str = "main",
    request_key: str | None = None,
    tool_call_id: str | None = None,
    require_current_goal: bool | None = None,
) -> int:
    """CAS-append one immutable entry inside the caller's transaction. Idempotent on (stream, request_key) for
    assistant entries and on tool_call_id for tool entries. Fails closed on stale fence / goal / cancel."""
    if require_current_goal is None:
        require_current_goal = kind == "assistant"
    run = c.execute(
        "SELECT r.lease_epoch, r.head_revision, r.status_code, r.cancel_requested, t.goal_revision"
        " FROM agent.runs r JOIN agent.tasks t ON t.tenant_id=r.tenant_id AND t.id=r.task_id"
        " WHERE r.tenant_id=%s AND r.id=%s AND r.task_id=%s FOR UPDATE OF r",
        (claim.tenant_id, claim.run_id, claim.task_id),
    ).fetchone()
    if run is None:
        raise TenantMismatch(claim.run_id)
    if run["status_code"] in TERMINAL:
        raise RunTerminal(run["status_code"])
    if run["lease_epoch"] != claim.lease_epoch:
        raise FencedOut(claim.run_id)
    if require_current_goal and run["goal_revision"] != claim.goal_revision:
        raise StaleGoal(claim.run_id)
    existing = _find_entry(c, claim.tenant_id, claim.run_id, kind, stream, request_key, tool_call_id)
    if existing is not None:
        return int(existing["checkpoint_revision"])
    prev = c.execute(
        "SELECT snapshot_sha256 FROM agent.run_checkpoints WHERE tenant_id=%s AND run_id=%s AND checkpoint_revision=%s",
        (claim.tenant_id, claim.run_id, run["head_revision"]),
    ).fetchone()
    rev = run["head_revision"] + 1
    snap = sha256_hex(
        {
            "prev": prev["snapshot_sha256"] if prev else None,
            "rev": rev,
            "kind": kind,
            "stream": stream,
            "payload": payload,
        }
    )
    cid = uuid7()
    c.execute(
        "INSERT INTO agent.run_checkpoints(tenant_id, id, task_id, run_id, checkpoint_revision, stream, entry_kind,"
        " request_key, tool_call_id, writer_ref, writer_lease_epoch, applied_goal_revision, checkpoint_json,"
        " snapshot_sha256) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            claim.tenant_id,
            cid,
            claim.task_id,
            claim.run_id,
            rev,
            stream,
            kind,
            request_key,
            tool_call_id,
            writer,
            claim.lease_epoch,
            min(claim.goal_revision, run["goal_revision"]),
            Jsonb(payload),
            snap,
        ),
    )
    cur = c.execute(
        "UPDATE agent.runs SET head_revision=%s, current_checkpoint_id=%s, updated_at=now()"
        " WHERE tenant_id=%s AND id=%s AND head_revision=%s",
        (rev, cid, claim.tenant_id, claim.run_id, run["head_revision"]),
    )
    if cur.rowcount != 1:
        raise FencedOut("HEAD CAS lost")
    return int(rev)


def _find_entry(
    c: psycopg.Connection[Row],
    tenant_id: str,
    run_id: str,
    kind: str,
    stream: str,
    request_key: str | None,
    tool_call_id: str | None,
) -> Row | None:
    if kind == "assistant" and request_key is not None:
        return c.execute(
            "SELECT checkpoint_revision, checkpoint_json FROM agent.run_checkpoints WHERE tenant_id=%s AND run_id=%s"
            " AND entry_kind='assistant' AND stream=%s AND request_key=%s",
            (tenant_id, run_id, stream, request_key),
        ).fetchone()
    if kind == "tool" and tool_call_id is not None:
        return c.execute(
            "SELECT checkpoint_revision, checkpoint_json FROM agent.run_checkpoints WHERE tenant_id=%s AND run_id=%s"
            " AND entry_kind='tool' AND tool_call_id=%s",
            (tenant_id, run_id, tool_call_id),
        ).fetchone()
    return None


def head_entries(c: psycopg.Connection[Row], tenant_id: str, run_id: str, stream: str | None = None) -> list[Row]:
    q = (
        "SELECT checkpoint_revision, stream, entry_kind, request_key, tool_call_id, checkpoint_json, snapshot_sha256,"
        " applied_goal_revision FROM agent.run_checkpoints WHERE tenant_id=%s AND run_id=%s"
    )
    args: tuple[Any, ...] = (tenant_id, run_id)
    if stream is not None:
        q += " AND stream=%s"
        args = (*args, stream)
    return c.execute(q + " ORDER BY checkpoint_revision", args).fetchall()


def transcript(c: psycopg.Connection[Row], tenant_id: str, run_id: str, stream: str = "main") -> list[dict[str, Any]]:
    return [
        dict(e["checkpoint_json"])
        for e in head_entries(c, tenant_id, run_id, stream)
        if e["entry_kind"] in ("user", "assistant", "tool")
    ]


def verify_head_chain(c: psycopg.Connection[Row], tenant_id: str, run_id: str) -> bool:
    """Recompute the hash chain. False = HEAD was modified outside the append path (tamper evidence)."""
    prev = None
    for e in head_entries(c, tenant_id, run_id):
        snap = sha256_hex(
            {
                "prev": prev,
                "rev": e["checkpoint_revision"],
                "kind": e["entry_kind"],
                "stream": e["stream"],
                "payload": e["checkpoint_json"],
            }
        )
        if snap != e["snapshot_sha256"]:
            return False
        prev = snap
    return True


def find_assistant(
    c: psycopg.Connection[Row], tenant_id: str, run_id: str, stream: str, request_key: str
) -> dict[str, Any] | None:
    row = _find_entry(c, tenant_id, run_id, "assistant", stream, request_key, None)
    return dict(row["checkpoint_json"]) if row else None


def find_tool(c: psycopg.Connection[Row], tenant_id: str, run_id: str, tool_call_id: str) -> dict[str, Any] | None:
    row = _find_entry(c, tenant_id, run_id, "tool", "main", None, tool_call_id)
    return dict(row["checkpoint_json"]) if row else None


def find_proposal(
    c: psycopg.Connection[Row], tenant_id: str, run_id: str, tool_call_id: str
) -> tuple[dict[str, Any], str] | None:
    """The committed assistant proposal for a tool call id (LOOP-004) and the stream it was proposed on."""
    row = c.execute(
        "SELECT tc, stream FROM agent.run_checkpoints, jsonb_array_elements(checkpoint_json->'tool_calls') tc"
        " WHERE tenant_id=%s AND run_id=%s AND entry_kind='assistant' AND tc->>'id' = %s",
        (tenant_id, run_id, tool_call_id),
    ).fetchone()
    return (dict(row["tc"]), row["stream"]) if row else None


# ------------------------------------------------------------------------------------------------ framework_ref
def framework_ref(db: Database, tenant_id: str, run_id: str) -> str | None:
    row = db.one("SELECT framework_ref FROM agent.runs WHERE tenant_id=%s AND id=%s", (tenant_id, run_id))
    return row["framework_ref"] if row else None


def set_framework_ref(db: Database, claim: Claim, ref: str) -> None:
    with db.conn() as c:
        cur = c.execute(
            "UPDATE agent.runs SET framework_ref=%s WHERE tenant_id=%s AND id=%s AND lease_epoch=%s",
            (ref, claim.tenant_id, claim.run_id, claim.lease_epoch),
        )
        if cur.rowcount != 1:
            raise FencedOut(claim.run_id)


def discard_framework_ref(db: Database, claim: Claim, ref: str | None) -> bool:
    """Drop the derived-cache pointer only if it is still the one this claim used and the claim still owns the run."""
    if ref is None:
        return False
    with db.conn() as c:
        cur = c.execute(
            "UPDATE agent.runs SET framework_ref=NULL WHERE tenant_id=%s AND id=%s AND framework_ref=%s AND lease_epoch=%s",
            (claim.tenant_id, claim.run_id, ref, claim.lease_epoch),
        )
        return cur.rowcount == 1


# ------------------------------------------------------------------------------------------------ outcomes
_TASK_FOR_RUN = {
    "SUCCEEDED": "SUCCEEDED",
    "PARTIAL": "PARTIAL",
    "FAILED": "FAILED",
    "CANCELLED": "CANCELLED",
    "EXPIRED": "EXPIRED",
    "BUDGET_EXHAUSTED": "BUDGET_EXHAUSTED",
    "WAITING_APPROVAL": "WAITING_INPUT",
    "WAITING_INPUT": "WAITING_INPUT",
    "PAUSED": "PAUSED",
    "WAITING_RECONCILIATION": "WAITING_RECONCILIATION",
}


def finish_segment(
    db: Database, claim: Claim, outcome: str, *, result_text: str | None = None, error_code: str | None = None
) -> str:
    """Record the end of a run segment as ONE transaction (run + task + events). Returns the state written.

    * SUCCEEDED requires: current fence, current goal, run RUNNING, no cancel/pause request.
    * A cancel committed first wins: the run becomes CANCELLED (or WAITING_RECONCILIATION when an external effect is
      SENDING/UNKNOWN), never SUCCEEDED.
    * Non-terminal outcomes (WAITING_APPROVAL, PAUSED, WAITING_RECONCILIATION) release the lease.
    """
    with db.tx() as c:
        task = c.execute(
            "SELECT id, owner_user_id, workspace_id, goal_revision, status_code FROM agent.tasks WHERE tenant_id=%s AND id=%s FOR UPDATE",
            (claim.tenant_id, claim.task_id),
        ).fetchone()
        run = c.execute(
            "SELECT status_code, lease_epoch, cancel_requested, pause_requested FROM agent.runs"
            " WHERE tenant_id=%s AND id=%s AND task_id=%s FOR UPDATE",
            (claim.tenant_id, claim.run_id, claim.task_id),
        ).fetchone()
        if task is None or run is None:
            raise TenantMismatch(claim.run_id)
        if run["status_code"] in TERMINAL:
            raise (Cancelled if run["status_code"] == "CANCELLED" else RunTerminal)(run["status_code"])
        if run["lease_epoch"] != claim.lease_epoch:
            raise FencedOut(claim.run_id)
        target = outcome
        revoked = not scope_active(c, claim.tenant_id, str(task["owner_user_id"]), str(task["workspace_id"]))
        if revoked or run["cancel_requested"] or run["status_code"] == "CANCELLING":
            target = (
                "WAITING_RECONCILIATION" if _has_unresolved_effects(c, claim.tenant_id, claim.run_id) else "CANCELLED"
            )
            if revoked:
                error_code = "SCOPE_REVOKED"
        elif run["pause_requested"]:
            target = (
                "WAITING_RECONCILIATION"
                if outcome == "WAITING_RECONCILIATION"
                or run["status_code"] == "WAITING_RECONCILIATION"
                or _has_unresolved_effects(c, claim.tenant_id, claim.run_id)
                else "PAUSED"
            )
        elif outcome in ("SUCCEEDED", "PARTIAL") and task["goal_revision"] != claim.goal_revision:
            raise StaleGoal(claim.run_id)
        terminal = target in TERMINAL
        c.execute(
            "UPDATE agent.runs SET status_code=%s, result_text=coalesce(%s, result_text), error_code=%s,"
            " cancel_requested = cancel_requested OR %s,"
            " lease_expires_at = CASE WHEN %s THEN NULL ELSE now() - interval '1 millisecond' END,"
            " ended_at = CASE WHEN %s THEN now() ELSE ended_at END, record_version=record_version+1, updated_at=now()"
            " WHERE tenant_id=%s AND id=%s AND lease_epoch=%s",
            (
                target,
                result_text if target in ("SUCCEEDED", "PARTIAL") else None,
                error_code,
                revoked,
                terminal,
                terminal,
                claim.tenant_id,
                claim.run_id,
                claim.lease_epoch,
            ),
        )
        task_state = _TASK_FOR_RUN.get(target, "RUNNING")
        if task["status_code"] not in TERMINAL and task["status_code"] != task_state:
            c.execute(
                "UPDATE agent.tasks SET status_code=%s, record_version=record_version+1, updated_at=now()"
                " WHERE tenant_id=%s AND id=%s",
                (task_state, claim.tenant_id, claim.task_id),
            )
        append_event(
            c,
            tenant_id=claim.tenant_id,
            aggregate_type="run",
            aggregate_id=claim.run_id,
            kind="run.state_changed",
            payload={
                "from": run["status_code"],
                "to": target,
                "leaseEpoch": str(claim.lease_epoch),
                **({"errorCode": error_code} if error_code else {}),
            },
            actor=SERVICE_ACTOR,
            owner_user_id=task["owner_user_id"],
            task_id=claim.task_id,
            run_id=claim.run_id,
        )
        if target in ("SUCCEEDED", "PARTIAL"):
            append_event(
                c,
                tenant_id=claim.tenant_id,
                aggregate_type="task",
                aggregate_id=claim.task_id,
                kind="result.published",
                payload={"runId": claim.run_id, "state": target},
                actor=SERVICE_ACTOR,
                owner_user_id=task["owner_user_id"],
                task_id=claim.task_id,
                run_id=claim.run_id,
            )
        return target
