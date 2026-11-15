from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiagent_automation.campaign import FakeClock
from aiagent_automation.watch import DailyWatch, SqliteWatchHarness, WatchController, local_slot, next_daily


class MockQueue:
    def __init__(self, *, accept: bool = True):
        self.dedupe_keys = set()
        self.calls = []
        self.accept = accept

    def request_watch(self, **kw) -> bool:
        self.calls.append(kw)
        if not self.accept:
            return False
        self.dedupe_keys.add(kw["dedupe_key"])
        return True


def watch(zone, hour=2, minute=30, max_occurrences=3):
    return DailyWatch("w1", "t1", 1, zone, hour, minute, "2026-01-01T00:00:00Z", max_occurrences)


def test_dst_gap_skip():
    from datetime import date

    w = watch("America/New_York")
    assert local_slot(date(2026, 3, 8), w) is None
    after = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)
    assert next_daily(w, after).date().isoformat() == "2026-03-09"


def test_dst_fold_first_only():
    from datetime import date

    w = watch("America/New_York", hour=1, minute=30)
    first = local_slot(date(2026, 11, 1), w)
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert next_daily(w, first) > first + timedelta(hours=1)


def test_tokyo_timezone_and_max_slots(tmp_path: Path):
    clock = FakeClock(datetime(2026, 11, 11, 0, 30, tzinfo=UTC))
    queue = MockQueue()
    store = SqliteWatchHarness(tmp_path / "watch.sqlite")
    ctrl = WatchController(store, clock, queue)
    w = watch("Asia/Tokyo", hour=9, minute=0, max_occurrences=1)
    assert ctrl.tick(w)
    assert not ctrl.tick(w)
    assert ctrl.relay() == 1
    assert ctrl.relay() == 0
    new_ctrl = WatchController(SqliteWatchHarness(store.path), clock, queue)
    assert not new_ctrl.tick(w)
    assert len(queue.dedupe_keys) == 1


def test_no_access_to_external_service(tmp_path: Path):
    clock = FakeClock(datetime(2026, 11, 11, tzinfo=UTC))
    queue = MockQueue()
    ctrl = WatchController(SqliteWatchHarness(tmp_path / "watch.sqlite"), clock, queue)
    assert not ctrl.tick(watch("Asia/Tokyo", hour=10))
    assert ctrl.relay() == 0


def test_relay_keeps_pending_when_queue_rejects_and_isolates_tenants(tmp_path: Path):
    clock = FakeClock(datetime(2026, 11, 11, 0, 30, tzinfo=UTC))
    store = SqliteWatchHarness(tmp_path / "watch.sqlite")
    queue = MockQueue(accept=False)
    ctrl = WatchController(store, clock, queue)

    w_t1 = DailyWatch("shared-watch", "t1", 1, "Asia/Tokyo", 9, 0, "2026-01-01T00:00:00Z", 1)
    w_t2 = DailyWatch("shared-watch", "t2", 1, "Asia/Tokyo", 9, 0, "2026-01-01T00:00:00Z", 1)
    assert ctrl.tick(w_t1)
    assert ctrl.tick(w_t2)
    assert store.count("t1", "shared-watch") == 1
    assert store.count("t2", "shared-watch") == 1

    # Queue rejects -> entries remain PENDING and can be retried later when accepted.
    assert ctrl.relay() == 0
    assert len(store.pending()) == 2

    queue.accept = True
    assert ctrl.relay() == 2
    assert store.pending() == []
    assert len(queue.dedupe_keys) == 2


def test_claim_bounded_enforces_max_occurrences_atomically(tmp_path: Path):
    store = SqliteWatchHarness(tmp_path / "watch.sqlite")
    w = watch("UTC", hour=1, minute=0, max_occurrences=1)
    slot1 = datetime(2026, 11, 11, 1, 0, tzinfo=UTC)
    slot2 = datetime(2026, 11, 12, 1, 0, tzinfo=UTC)
    assert store.claim_bounded(w, slot1)
    assert not store.claim_bounded(w, slot2)
    assert store.count("t1", "w1") == 1
