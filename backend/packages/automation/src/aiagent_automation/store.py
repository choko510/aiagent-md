"""SQLite test fixture: durable restart and atomic-CAS/outbox simulation, NOT production DDL."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from .campaign import Campaign, Rejected, _CommitRejection

T = TypeVar("T")


class SqliteHarnessStore:
    """BEGIN IMMEDIATE simulates one aggregate lock across controller/worker processes."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with self._connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS campaigns (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def _connection(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def create(self, campaign: Campaign) -> None:
        with self._connection() as db:
            try:
                db.execute("INSERT INTO campaigns VALUES (?, ?)",
                           (campaign.plan.campaign_id, json.dumps(campaign.wire())))
            except sqlite3.IntegrityError as exc:
                raise Rejected("CAMPAIGN_EXISTS") from exc

    def read(self, campaign_id: str) -> Campaign:
        with self._connection() as db:
            row = db.execute("SELECT payload FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if row is None:
                raise Rejected("CAMPAIGN_NOT_FOUND")
            return Campaign.from_wire(json.loads(row[0]))

    def mutate(self, campaign_id: str, fn: Callable[[Campaign], T]) -> T:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if row is None:
                raise Rejected("CAMPAIGN_NOT_FOUND")
            campaign = Campaign.from_wire(json.loads(row[0]))
            # _CommitRejection represents an expected fail-closed transition that must persist.
            rejection: _CommitRejection | None = None
            try:
                result = fn(campaign)
            except _CommitRejection as exc:
                rejection = exc
                result = None  # type: ignore[assignment]
            db.execute("UPDATE campaigns SET payload=? WHERE id=?",
                       (json.dumps(campaign.wire()), campaign_id))
            db.commit()
            if rejection is not None:
                raise rejection
            return result  # type: ignore[return-value]
