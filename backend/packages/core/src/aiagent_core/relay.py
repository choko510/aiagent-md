"""Transactional Outbox relay (10-02 §7.1). claim (short TX, relay fence) -> publish (QueuePort, separate TX)
-> mark dispatched only if the relay fence is unchanged. Duplicate publish is harmless (queue dedupe key = outbox
identity); a lost publish ACK leaves the row reclaimable. Rows minted under an older restore incarnation are never
published (their authority predates the restore)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .db import Database
from .queue import enqueue


@dataclass(frozen=True)
class OutboxClaim:
    tenant_id: str
    id: str
    topic: str
    dedupe_key: str
    relay_fence: int
    payload: dict[str, Any]
    incarnation: int


def _fault(point: str) -> None:
    if point in os.environ.get("AIAGENT_FAULT", "").split(","):
        os._exit(137)


class OutboxRelay:
    def __init__(self, db: Database, owner: str, *, lease_s: float = 30.0, batch: int = 100) -> None:
        self.db, self.owner, self.lease_s, self.batch = db, owner, lease_s, batch

    def claim_batch(self) -> list[OutboxClaim]:
        with self.db.tx() as c:
            rows = c.execute(
                "WITH cand AS (SELECT tenant_id, id FROM agent.outbox_messages WHERE dispatched_at IS NULL"
                " AND next_attempt_at <= now() AND (relay_lease_until IS NULL OR relay_lease_until <= now())"
                " ORDER BY next_attempt_at, id LIMIT %s FOR UPDATE SKIP LOCKED)"
                " UPDATE agent.outbox_messages o SET relay_lease_epoch = o.relay_lease_epoch + 1, relay_owner_ref=%s,"
                " relay_lease_until = now() + make_interval(secs => %s), dispatch_attempts = o.dispatch_attempts + 1"
                " FROM cand WHERE o.tenant_id=cand.tenant_id AND o.id=cand.id"
                " RETURNING o.tenant_id, o.id, o.topic, o.dedupe_key, o.relay_lease_epoch, o.safe_payload_json,"
                " o.restore_incarnation",
                (self.batch, self.owner, self.lease_s),
            ).fetchall()
        return [
            OutboxClaim(
                str(r["tenant_id"]),
                str(r["id"]),
                r["topic"],
                r["dedupe_key"],
                r["relay_lease_epoch"],
                dict(r["safe_payload_json"]),
                r["restore_incarnation"],
            )
            for r in rows
        ]

    def publish(self, m: OutboxClaim) -> None:
        if m.topic != "run":  # notification / sse topics are delivered by their own workers (not in STEP 3 scope)
            return
        with self.db.tx() as c:
            enqueue(c, "run", m.tenant_id, f"outbox:{m.id}", m.payload)

    def mark(self, m: OutboxClaim, *, error: str | None = None) -> bool:
        with self.db.conn() as c:
            if error is None:
                cur = c.execute(
                    "UPDATE agent.outbox_messages SET dispatched_at=now(), relay_lease_until=NULL, last_error_code=NULL"
                    " WHERE tenant_id=%s AND id=%s AND relay_lease_epoch=%s AND relay_owner_ref=%s AND dispatched_at IS NULL",
                    (m.tenant_id, m.id, m.relay_fence, self.owner),
                )
            else:
                cur = c.execute(
                    "UPDATE agent.outbox_messages SET relay_lease_until=NULL, last_error_code=%s,"
                    " next_attempt_at = now() + make_interval(secs => least(300, 2 ^ least(dispatch_attempts, 8)))"
                    " WHERE tenant_id=%s AND id=%s AND relay_lease_epoch=%s AND relay_owner_ref=%s AND dispatched_at IS NULL",
                    (error, m.tenant_id, m.id, m.relay_fence, self.owner),
                )
            return cur.rowcount == 1

    def run_once(self) -> int:
        inc_row = self.db.one("SELECT restore_incarnation FROM agent.schema_control")
        assert inc_row is not None
        n = 0
        for m in self.claim_batch():
            _fault("relay_after_claim")
            if m.incarnation != inc_row["restore_incarnation"]:
                self.mark(m, error="RESTORE_STALE")  # kept for audit, never dispatched
                continue
            try:
                self.publish(m)
            except Exception:
                self.mark(m, error="PUBLISH_FAILED")
                continue
            _fault("relay_after_publish")
            if self.mark(m):
                n += 1
        return n
