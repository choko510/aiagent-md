"""FakeClock-friendly bounded daily Watch scheduling with DST gap skip and first-fold policy.

This is *not* a new broker. AI-07 maps the durable slot outbox to the existing
scheduler Task/Run path and handles its PostgreSQL occurrence ledger.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .campaign import Clock, Rejected, parse_utc


@dataclass(frozen=True)
class DailyWatch:
    watch_id: str
    tenant_id: str
    revision: int
    timezone_id: str
    local_hour: int
    local_minute: int
    start_utc: str
    max_occurrences: int

    def __post_init__(self) -> None:
        if not self.watch_id or not self.tenant_id or self.revision < 1 or self.max_occurrences < 1:
            raise Rejected("BAD_WATCH_ID_OR_LIMIT")
        if not 0 <= self.local_hour <= 23 or not 0 <= self.local_minute <= 59:
            raise Rejected("BAD_LOCAL_TIME")
        try:
            ZoneInfo(self.timezone_id)
        except ZoneInfoNotFoundError as exc:
            raise Rejected("UNKNOWN_TIMEZONE") from exc
        parse_utc(self.start_utc)


def local_slot(day: date, watch: DailyWatch) -> datetime | None:
    """Roundtrip detects nonexistent wall times. fold=0 chooses first repeated hour."""
    zone = ZoneInfo(watch.timezone_id)
    candidate = datetime.combine(day, time(watch.local_hour, watch.local_minute), zone).replace(fold=0)
    utc = candidate.astimezone(UTC)
    restored = utc.astimezone(zone)
    if (restored.date(), restored.hour, restored.minute, restored.fold) != (
        day,
        watch.local_hour,
        watch.local_minute,
        0,
    ):
        return None
    return utc


def next_daily(watch: DailyWatch, after_utc: datetime) -> datetime | None:
    if after_utc.tzinfo is None:
        raise Rejected("NAIVE_CLOCK")
    zone = ZoneInfo(watch.timezone_id)
    first_day = max(parse_utc(watch.start_utc).astimezone(zone).date(), after_utc.astimezone(zone).date())
    for offset in range(370):
        slot = local_slot(first_day + timedelta(days=offset), watch)
        if slot is not None and slot > after_utc and slot >= parse_utc(watch.start_utc):
            return slot
    return None


class WatchQueue(Protocol):
    def request_watch(self, *, tenant_id: str, watch_id: str, slot_utc: str, dedupe_key: str) -> None: ...


class SqliteWatchHarness:
    """Test-only transactional occurrence/outbox, no changes to production PostgreSQL."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS watch_slots (watch_id TEXT, slot TEXT, tenant_id TEXT,"
                " dedupe TEXT UNIQUE, status TEXT, PRIMARY KEY (watch_id,slot))"
            )

    def claim(self, watch: DailyWatch, slot: datetime) -> bool:
        dedupe = f"watch:{watch.tenant_id}:{watch.watch_id}:r{watch.revision}:{slot.isoformat()}"
        with sqlite3.connect(self.path, timeout=10) as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO watch_slots VALUES (?,?,?,?, 'PENDING')",
                (watch.watch_id, slot.isoformat(), watch.tenant_id, dedupe),
            )
            return cur.rowcount == 1

    def pending(self) -> list[tuple[str, str, str, str]]:
        with sqlite3.connect(self.path) as db:
            return db.execute(
                "SELECT tenant_id,watch_id,slot,dedupe FROM watch_slots WHERE status='PENDING'"
            ).fetchall()

    def ack(self, dedupe: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE watch_slots SET status='SENT' WHERE dedupe=?", (dedupe,))

    def count(self, watch_id: str) -> int:
        with sqlite3.connect(self.path) as db:
            return int(db.execute("SELECT count(*) FROM watch_slots WHERE watch_id=?", (watch_id,)).fetchone()[0])


class WatchController:
    def __init__(self, store: SqliteWatchHarness, clock: Clock, queue: WatchQueue) -> None:
        self.store, self.clock, self.queue = store, clock, queue

    def tick(self, watch: DailyWatch) -> bool:
        """Bounded latest slot, no infinite catch-up or provider access. Occurrence admission is atomic."""
        if self.store.count(watch.watch_id) >= watch.max_occurrences:
            return False
        now = self.clock.now()
        if now.tzinfo is None:
            raise Rejected("NAIVE_CLOCK")
        # Only the current local day; missed days are skipped, never replayed unboundedly.
        day = now.astimezone(ZoneInfo(watch.timezone_id)).date()
        slot = local_slot(day, watch)
        if slot is None or not parse_utc(watch.start_utc) <= slot <= now:
            return False
        return self.store.claim(watch, slot)

    def relay(self) -> int:
        sent = 0
        for tenant, watch, slot, dedupe in self.store.pending():
            self.queue.request_watch(tenant_id=tenant, watch_id=watch, slot_utc=slot, dedupe_key=dedupe)
            self.store.ack(dedupe)
            sent += 1
        return sent
