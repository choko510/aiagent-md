"""PostgreSQL stores and QueuePort dispatch adapters for AI-04 Campaign & Watch Automation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

import psycopg
from aiagent_core.commands import create_task_tx
from aiagent_core.db import Database
from psycopg.types.json import Jsonb

from .campaign import Campaign, QueueDispatch, Rejected, Store
from .watch import DailyWatch

T = TypeVar("T")


class PostgresCampaignStore(Store):
    """Serializable row-locked Campaign aggregate store in agent.automation_campaigns."""

    def __init__(self, db: Database, *, tenant_id: str, owner_user_id: str, workspace_id: str) -> None:
        self._db = db
        self._tenant_id = tenant_id
        self._owner_user_id = owner_user_id
        self._workspace_id = workspace_id

    def create(self, campaign: Campaign) -> None:
        if campaign.plan.tenant_id != self._tenant_id:
            raise Rejected("TENANT_MISMATCH")
        with self._db.tx(tenant=self._tenant_id) as c:
            try:
                c.execute(
                    "INSERT INTO agent.automation_campaigns(tenant_id, id, owner_user_id, workspace_id,"
                    " state, control_version, goal_revision, campaign_json)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        self._tenant_id,
                        campaign.plan.campaign_id,
                        self._owner_user_id,
                        self._workspace_id,
                        str(campaign.state),
                        campaign.control_version,
                        int(campaign.plan.goal_revision),
                        Jsonb(campaign.wire()),
                    ),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise Rejected("CAMPAIGN_EXISTS") from exc

    def read(self, campaign_id: str) -> Campaign:
        with self._db.tx(tenant=self._tenant_id) as c:
            row = c.execute(
                "SELECT campaign_json FROM agent.automation_campaigns"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s",
                (self._tenant_id, campaign_id, self._owner_user_id),
            ).fetchone()
        if row is None:
            raise Rejected("CAMPAIGN_NOT_FOUND")
        return Campaign.from_wire(dict(row["campaign_json"]))

    def mutate(self, campaign_id: str, fn: Callable[[Campaign], T]) -> T:
        with self._db.tx(tenant=self._tenant_id) as c:
            row = c.execute(
                "SELECT campaign_json FROM agent.automation_campaigns"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s FOR UPDATE",
                (self._tenant_id, campaign_id, self._owner_user_id),
            ).fetchone()
            if row is None:
                raise Rejected("CAMPAIGN_NOT_FOUND")
            campaign = Campaign.from_wire(dict(row["campaign_json"]))
            try:
                result = fn(campaign)
            finally:
                c.execute(
                    "UPDATE agent.automation_campaigns"
                    " SET state=%s, control_version=%s, goal_revision=%s, campaign_json=%s, updated_at=now()"
                    " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s",
                    (
                        str(campaign.state),
                        campaign.control_version,
                        int(campaign.plan.goal_revision),
                        Jsonb(campaign.wire()),
                        self._tenant_id,
                        campaign_id,
                        self._owner_user_id,
                    ),
                )
            return result


class PostgresQueueDispatch(QueueDispatch):
    """Atomic QueuePort / create_task_tx + outbox run.start admission bound to campaign control_version."""

    def __init__(
        self,
        db: Database,
        *,
        owner_user_id: str,
        workspace_id: str,
        profile: str = "default",
    ) -> None:
        self._db = db
        self._owner_user_id = owner_user_id
        self._workspace_id = workspace_id
        self._profile = profile
        self.created_runs: list[dict[str, str]] = []

    def request_run(
        self,
        *,
        tenant_id: str,
        campaign_id: str,
        cycle_id: str,
        dedupe_key: str,
        control_version: int,
    ) -> bool:
        with self._db.tx(tenant=tenant_id) as c:
            row = c.execute(
                "SELECT state, control_version, campaign_json FROM agent.automation_campaigns"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s FOR UPDATE",
                (tenant_id, campaign_id, self._owner_user_id),
            ).fetchone()
            if row is None or row["state"] != "ACTIVE" or int(row["control_version"]) != control_version:
                return False
            camp = Campaign.from_wire(dict(row["campaign_json"]))
            cycle = next((cy for cy in camp.cycles if cy.cycle_id == cycle_id), None)
            if cycle is None:
                return False
            existing = c.execute(
                "SELECT id FROM agent.tasks WHERE tenant_id=%s AND trigger_key=%s",
                (tenant_id, dedupe_key),
            ).fetchone()
            if existing is not None:
                return True
            child_goal = camp.plan.child_task_goal(cycle.cycle_id, cycle.logical_work_item_id)
            created = create_task_tx(
                c,
                tenant_id=tenant_id,
                owner_user_id=self._owner_user_id,
                workspace_id=self._workspace_id,
                goal=child_goal,
                profile=self._profile,
                actor={"type": "service", "id": "automation"},
                trigger_key=dedupe_key,
                token_budget=camp.plan.tokens_per_cycle,
                max_tool_calls=camp.plan.tools_per_cycle,
            )
            self.created_runs.append(created)
            return True


class PostgresWatchStore:
    """Atomic bounded occurrence watch repository in PostgreSQL (no sqlite TOCTOU)."""

    def __init__(self, db: Database, *, owner_user_id: str, workspace_id: str) -> None:
        self._db = db
        self._owner_user_id = owner_user_id
        self._workspace_id = workspace_id

    def register(self, watch: DailyWatch) -> None:
        with self._db.tx(tenant=watch.tenant_id) as c:
            row = c.execute(
                "INSERT INTO agent.automation_watches(tenant_id, watch_id, owner_user_id, workspace_id,"
                " revision, max_occurrences, timezone_id, start_utc)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s::timestamptz)"
                " ON CONFLICT (tenant_id, watch_id) DO UPDATE"
                " SET revision=EXCLUDED.revision, max_occurrences=EXCLUDED.max_occurrences,"
                " timezone_id=EXCLUDED.timezone_id, start_utc=EXCLUDED.start_utc"
                " WHERE agent.automation_watches.owner_user_id = EXCLUDED.owner_user_id"
                " AND agent.automation_watches.workspace_id = EXCLUDED.workspace_id"
                " AND EXCLUDED.revision >= agent.automation_watches.revision"
                " RETURNING watch_id",
                (
                    watch.tenant_id,
                    watch.watch_id,
                    self._owner_user_id,
                    self._workspace_id,
                    watch.revision,
                    watch.max_occurrences,
                    watch.timezone_id,
                    watch.start_utc,
                ),
            ).fetchone()
            if row is None:
                raise Rejected("WATCH_OWNER_OR_REVISION_CONFLICT")

    def claim_bounded_tx(self, c: psycopg.Connection[Any], watch: DailyWatch, slot: datetime) -> bool:
        """Atomically claim a bounded watch slot within an existing tenant-scoped DB transaction."""
        dedupe = f"watch:{watch.tenant_id}:{watch.watch_id}:r{watch.revision}:{slot.isoformat()}"
        parent = c.execute(
            "SELECT owner_user_id, revision, max_occurrences, active FROM agent.automation_watches"
            " WHERE tenant_id=%s AND watch_id=%s FOR UPDATE",
            (watch.tenant_id, watch.watch_id),
        ).fetchone()
        if (
            parent is None
            or not parent["active"]
            or str(parent["owner_user_id"]) != self._owner_user_id
            or int(parent["revision"]) != watch.revision
        ):
            return False
        existing = c.execute(
            "SELECT count(*) AS n FROM agent.automation_watch_slots WHERE tenant_id=%s AND watch_id=%s",
            (watch.tenant_id, watch.watch_id),
        ).fetchone()
        assert existing is not None
        if int(existing["n"]) >= int(parent["max_occurrences"]):
            return False
        cur = c.execute(
            "INSERT INTO agent.automation_watch_slots(tenant_id, watch_id, slot_iso, dedupe_key, status)"
            " VALUES (%s,%s,%s,%s,'PENDING') ON CONFLICT DO NOTHING",
            (watch.tenant_id, watch.watch_id, slot.isoformat(), dedupe),
        )
        return cur.rowcount == 1

    def claim_bounded(self, watch: DailyWatch, slot: datetime) -> bool:
        with self._db.tx(tenant=watch.tenant_id) as c:
            return self.claim_bounded_tx(c, watch, slot)

    def pending(self, tenant_id: str) -> list[tuple[str, str, str, str]]:
        with self._db.tx(tenant=tenant_id) as c:
            rows = c.execute(
                "SELECT tenant_id, watch_id, slot_iso, dedupe_key FROM agent.automation_watch_slots"
                " WHERE tenant_id=%s AND status='PENDING' ORDER BY created_at",
                (tenant_id,),
            ).fetchall()
        return [(str(r["tenant_id"]), str(r["watch_id"]), str(r["slot_iso"]), str(r["dedupe_key"])) for r in rows]

    def ack(self, tenant_id: str, dedupe: str) -> None:
        with self._db.tx(tenant=tenant_id) as c:
            c.execute(
                "UPDATE agent.automation_watch_slots SET status='SENT'"
                " WHERE tenant_id=%s AND dedupe_key=%s AND status='PENDING'",
                (tenant_id, dedupe),
            )
