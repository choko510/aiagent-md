from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiagent_automation.campaign import FakeClock
from aiagent_automation.watch import DailyWatch, SqliteWatchHarness, WatchController, local_slot, next_daily


class MockQueue:
    def __init__(self):
        self.dedupe_keys = set()
        self.calls = []

    def request_watch(self, **kw):
        self.calls.append(kw)
        self.dedupe_keys.add(kw['dedupe_key'])


def watch(zone, hour=2, minute=30, max_occurrences=3):
    return DailyWatch('w1', 't1', 1, zone, hour, minute, '2026-01-01T00:00:00Z', max_occurrences)


def test_dst_gap_skip():
    from datetime import date
    w = watch('America/New_York')
    assert local_slot(date(2026, 3, 8), w) is None
    after = datetime(2026, 3, 8, 6, 0, tzinfo=timezone.utc)
    assert next_daily(w, after).date().isoformat() == '2026-03-09'


def test_dst_fold_first_only():
    from datetime import date
    w = watch('America/New_York', hour=1, minute=30)
    first = local_slot(date(2026, 11, 1), w)
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    assert next_daily(w, first) > first + timedelta(hours=1)


def test_tokyo_timezone_and_max_slots(tmp_path: Path):
    clock = FakeClock(datetime(2026, 11, 11, 0, 30, tzinfo=timezone.utc))
    queue = MockQueue()
    store = SqliteWatchHarness(tmp_path / 'watch.sqlite')
    ctrl = WatchController(store, clock, queue)
    w = watch('Asia/Tokyo', hour=9, minute=0, max_occurrences=1)
    assert ctrl.tick(w)
    assert not ctrl.tick(w)
    assert ctrl.relay() == 1
    assert ctrl.relay() == 0
    new_ctrl = WatchController(SqliteWatchHarness(store.path), clock, queue)
    assert not new_ctrl.tick(w)
    assert len(queue.dedupe_keys) == 1


def test_no_access_to_external_service(tmp_path: Path):
    clock = FakeClock(datetime(2026, 11, 11, tzinfo=timezone.utc))
    queue = MockQueue()
    ctrl = WatchController(SqliteWatchHarness(tmp_path / 'watch.sqlite'), clock, queue)
    assert not ctrl.tick(watch('Asia/Tokyo', hour=10))
    assert ctrl.relay() == 0
