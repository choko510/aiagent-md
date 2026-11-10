"""Scheduler (CHAT_07) on top of the Outbox/QueuePort - no second queue.

* Clock = the database clock (`now()`), never the local worker clock, so skewed workers agree on due slots.
* A schedule row is processed under `FOR UPDATE SKIP LOCKED`; every fired slot is an `agent.schedule_occurrences`
  row (UNIQUE schedule+slot) created in the SAME transaction as its Task/Run/Event/Outbox -> one Task per slot even
  with several scheduler processes or after a crash/restart (JQ-T07).
* Cron arithmetic only uses APScheduler's CronTrigger (time calculation helper, ADR-039); APScheduler's own job store
  / executor are not used.
* Catch-up after downtime: `skip` (fire only if the slot is within the grace window), `latest` (fire the newest missed
  slot), `all_bounded` (fire up to max_catchup newest missed slots).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiagent_domain import uuid7
from apscheduler.triggers.cron import CronTrigger

from .commands import create_task_tx
from .control import scope_active
from .db import Database

GRACE = timedelta(seconds=60)


def _trigger(expr: str, tz: str) -> CronTrigger:
    return CronTrigger.from_crontab(expr, timezone=ZoneInfo(tz))


def next_after(expr: str, tz: str, after: datetime) -> datetime | None:
    t = _trigger(expr, tz)
    nxt: datetime | None = t.get_next_fire_time(None, after + timedelta(microseconds=1))
    return nxt


def due_slots(
    expr: str, tz: str, last_slot: datetime | None, start: datetime, now: datetime, *, limit: int = 1000
) -> list[datetime]:
    """Find the newest due slots without scanning (and silently dropping) old cron occurrences.

    Binary-search the monotone next-fire function. Cost is O(limit * log(time range))
    even after years of downtime and high-frequency schedules.
    """
    trigger = _trigger(expr, tz)
    lower = last_slot or start
    upper = now
    newest_first: list[datetime] = []
    while len(newest_first) < limit and lower < upper:
        first = trigger.get_next_fire_time(None, lower + timedelta(microseconds=1))
        if first is None or first > upper:
            break
        span_us = (upper - lower) // timedelta(microseconds=1)
        lo, hi = 0, span_us
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            nxt = trigger.get_next_fire_time(None, lower + timedelta(microseconds=mid + 1))
            if nxt is not None and nxt <= upper:
                lo = mid
            else:
                hi = mid
        latest = trigger.get_next_fire_time(None, lower + timedelta(microseconds=lo + 1))
        if latest is None or latest > upper:
            break
        newest_first.append(latest)
        upper = latest - timedelta(microseconds=1)
    return list(reversed(newest_first))


def choose(slots: list[datetime], policy: str, max_catchup: int, now: datetime) -> list[datetime]:
    if not slots:
        return []
    if policy == "skip":
        return [slots[-1]] if now - slots[-1] <= GRACE else []
    if policy == "latest":
        return [slots[-1]]
    return slots[-max_catchup:]


@dataclass(frozen=True)
class Fired:
    schedule_id: str
    slot_at: datetime
    task_id: str


def create_schedule(
    db: Database,
    *,
    tenant_id: str,
    owner_user_id: str,
    workspace_id: str,
    goal: str,
    cron: str | None = None,
    run_at: datetime | None = None,
    timezone: str = "UTC",
    catchup: str = "latest",
    max_catchup: int = 3,
) -> str:
    ZoneInfo(timezone)  # validates the IANA name
    sid = uuid7()
    with db.tx(tenant=tenant_id) as c:
        if not scope_active(c, tenant_id, owner_user_id, workspace_id):
            raise ValueError("SCOPE_REVOKED")
        now = c.execute("SELECT now() AS n").fetchone()["n"]  # type: ignore[index]
        nxt = run_at if cron is None else next_after(cron, timezone, now)
        c.execute(
            "INSERT INTO agent.schedules(tenant_id, id, owner_user_id, workspace_id, kind, cron_expr, timezone_id, run_at,"
            " goal_text, catchup_policy, max_catchup, next_fire_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                tenant_id,
                sid,
                owner_user_id,
                workspace_id,
                "cron" if cron else "one_shot",
                cron,
                timezone,
                run_at,
                goal,
                catchup,
                max_catchup,
                nxt,
            ),
        )
    return sid


class Scheduler:
    def __init__(self, db: Database, *, batch: int = 50) -> None:
        self.db, self.batch = db, batch

    def tick(self) -> list[Fired]:
        fired: list[Fired] = []
        with self.db.tx() as c:
            now = c.execute("SELECT now() AS n").fetchone()["n"]  # type: ignore[index]
            rows = c.execute(
                "SELECT * FROM agent.schedules WHERE enabled AND next_fire_at <= now() ORDER BY next_fire_at"
                " LIMIT %s FOR UPDATE SKIP LOCKED",
                (self.batch,),
            ).fetchall()
            for s in rows:
                # A schedule is NOT durable permission to act after membership/tenant
                # or workspace revocation. Disable it without minting another Task.
                if not scope_active(c, str(s["tenant_id"]), str(s["owner_user_id"]), str(s["workspace_id"])):
                    c.execute(
                        "UPDATE agent.schedules SET enabled=false, next_fire_at=NULL, record_version=record_version+1,"
                        " updated_at=now() WHERE tenant_id=%s AND id=%s",
                        (s["tenant_id"], s["id"]),
                    )
                    continue
                due: list[datetime] = []
                if s["kind"] == "one_shot":
                    slots = [s["run_at"]] if s["last_slot_at"] is None else []
                    nxt = None
                else:
                    due = due_slots(
                        s["cron_expr"],
                        s["timezone_id"],
                        s["last_slot_at"],
                        s["created_at"],
                        now,
                        limit=s["max_catchup"] if s["catchup_policy"] == "all_bounded" else 1,
                    )
                    slots = choose(due, s["catchup_policy"], s["max_catchup"], now)
                    nxt = next_after(s["cron_expr"], s["timezone_id"], now)
                for slot in slots:
                    occ = uuid7()
                    exists = c.execute(
                        "SELECT 1 FROM agent.schedule_occurrences WHERE tenant_id=%s AND schedule_id=%s AND slot_at=%s",
                        (s["tenant_id"], s["id"], slot),
                    ).fetchone()
                    if exists:
                        continue
                    out = create_task_tx(
                        c,
                        tenant_id=str(s["tenant_id"]),
                        owner_user_id=str(s["owner_user_id"]),
                        workspace_id=str(s["workspace_id"]),
                        goal=s["goal_text"],
                        profile=s["profile_code"],
                        actor={"type": "service", "id": "scheduler"},
                        schedule_id=str(s["id"]),
                        trigger_key=f"{slot.isoformat()}#r{s['definition_revision']}",
                        occurrence_id=occ,
                    )
                    c.execute(
                        "INSERT INTO agent.schedule_occurrences(tenant_id, id, schedule_id, definition_revision, slot_at,"
                        " task_id, late_by_ms) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (
                            s["tenant_id"],
                            occ,
                            s["id"],
                            s["definition_revision"],
                            slot,
                            out["taskId"],
                            int((now - slot).total_seconds() * 1000),
                        ),
                    )
                    fired.append(Fired(str(s["id"]), slot, out["taskId"]))
                last = max([*slots, s["last_slot_at"]] if s["last_slot_at"] else slots, default=None)
                if s["kind"] == "cron":
                    # Always record the newest evaluated slot, including under skip policy.
                    last = due[-1] if due else s["last_slot_at"]
                c.execute(
                    "UPDATE agent.schedules SET next_fire_at=%s, last_slot_at=%s, record_version=record_version+1,"
                    " updated_at=now() WHERE tenant_id=%s AND id=%s",
                    (nxt, last, s["tenant_id"], s["id"]),
                )
        return fired

    def disable(self, tenant_id: str, schedule_id: str) -> bool:
        with self.db.tx(tenant=tenant_id) as c:
            cur = c.execute(
                "UPDATE agent.schedules SET enabled=false, next_fire_at=NULL, definition_revision=definition_revision+1,"
                " record_version=record_version+1, updated_at=now() WHERE tenant_id=%s AND id=%s",
                (tenant_id, schedule_id),
            )
            return cur.rowcount == 1
