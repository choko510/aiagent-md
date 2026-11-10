from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiagent_automation.campaign import (
    CampaignController,
    CampaignPlan,
    CampaignState,
    EffectState,
    FakeClock,
    Phase,
    Rejected,
    ResultState,
    StopPolicy,
)
from aiagent_automation.store import SqliteHarnessStore


class MockVerifier:
    def passes(self, *, campaign_id, cycle_id, evidence_refs, quality_gate_ref):
        return quality_gate_ref == "gate:pass" and bool(evidence_refs)


class MockQueue:
    def __init__(self):
        self.keys = set()
        self.deliveries = []
        self.fail_once = False

    def request_run(self, **kw):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated queue failure")
        self.keys.add(kw["dedupe_key"])
        self.deliveries.append(kw)
        return True


@pytest.fixture
def rig(tmp_path: Path):
    clock = FakeClock(datetime(2026, 11, 11, tzinfo=UTC))
    queue = MockQueue()
    store = SqliteHarnessStore(tmp_path / "campaign.sqlite")
    ctl = CampaignController(store, clock, queue, MockVerifier())
    return ctl, store, clock, queue


def plan(clock, **overrides):
    vals = dict(
        max_cycles=3,
        deadline_utc=(clock.now() + timedelta(hours=3)).isoformat(),
        max_cost_micro_usd=300,
        max_tokens=300,
        max_tool_calls=6,
    )
    vals.update({k: v for k, v in overrides.items() if k in vals or k == "target_count"})
    policy = StopPolicy(**vals)
    return CampaignPlan("c1", "tenant1", "g1", policy, 100, 100, 2)


def start(rig, **kwargs):
    ctl, _store, clock, _queue = rig
    ctl.create(plan(clock, **kwargs), "work1")
    assert ctl.dispatch_pending("c1") == 1
    assert ctl.admit_cycle("c1", "c1:1", 1)
    assert ctl.advance("c1", "c1:1", Phase.PLAN, Phase.EXECUTE, 1)
    return ctl


def verify(ctl, cycle="c1:1", **kwargs):
    ctl.advance("c1", cycle, Phase.EXECUTE, Phase.VERIFY, kwargs.pop("version", 1))
    return ctl.complete_cycle(
        "c1",
        cycle,
        claimed_success=True,
        evidence_refs=["artifact:sha"],
        quality_gate_ref="gate:pass",
        cost_micro_usd=50,
        tokens=60,
        tools=1,
        **kwargs,
    )


def test_bounded_continuation_max_cycles(rig):
    ctl = start(rig)
    assert verify(ctl, next_work_item_id="work2") == ResultState.VERIFIED
    assert ctl.dispatch_pending("c1") == 1
    assert ctl.admit_cycle("c1", "c1:2", 1)
    ctl.advance("c1", "c1:2", Phase.PLAN, Phase.EXECUTE, 1)
    verify(ctl, "c1:2", next_work_item_id="work3")
    ctl.dispatch_pending("c1")
    ctl.admit_cycle("c1", "c1:3", 1)
    ctl.advance("c1", "c1:3", Phase.PLAN, Phase.EXECUTE, 1)
    verify(ctl, "c1:3", next_work_item_id="work4")
    v = ctl.view("c1")
    assert v["state"] == CampaignState.LIMIT_REACHED
    assert v["cycleCount"] == 3
    assert v["completedCount"] == 3
    assert ctl.dispatch_pending("c1") == 0


def test_deadline_durable(rig):
    ctl, _store, clock, _queue = rig
    ctl.create(plan(clock), "work1")
    clock.advance(hours=4)
    with pytest.raises(Rejected, match="DEADLINE_REACHED"):
        ctl.admit_cycle("c1", "c1:1", 1)
    assert ctl.view("c1")["state"] == "LIMIT_REACHED"
    assert ctl.dispatch_pending("c1") == 0


def test_stop_and_stale_delivery(rig):
    ctl, _store, clock, _queue = rig
    ctl.create(plan(clock), "work1")
    assert ctl.control("c1", "STOP", 1) == 2
    assert ctl.view("c1")["state"] == "STOPPED"
    with pytest.raises(Rejected):
        ctl.admit_cycle("c1", "c1:1", 1)
    assert ctl.dispatch_pending("c1") == 0
    with pytest.raises(Rejected):
        ctl.control("c1", "RESUME", 2)


def test_pause_resume_fences_old_queue(rig):
    ctl, _store, clock, queue = rig
    ctl.create(plan(clock), "work1")
    assert ctl.control("c1", "PAUSE", 1) == 2
    assert ctl.dispatch_pending("c1") == 0
    assert ctl.control("c1", "RESUME", 2) == 3
    with pytest.raises(Rejected, match="STALE_CONTROL_VERSION"):
        ctl.admit_cycle("c1", "c1:1", 1)
    assert ctl.dispatch_pending("c1") == 1
    assert queue.deliveries[-1]["control_version"] == 3
    assert ctl.admit_cycle("c1", "c1:1", 3)


def test_auth_revoke(rig):
    ctl, _store, clock, _queue = rig
    ctl.create(plan(clock), "work1")
    ctl.control("c1", "REVOKE", 1)
    assert ctl.dispatch_pending("c1") == 0
    with pytest.raises(Rejected):
        ctl.admit_cycle("c1", "c1:1", 2)


def test_restart_dedupe_and_outbox_recovery(rig):
    ctl, store, clock, queue = rig
    ctl.create(plan(clock), "work1")
    queue.fail_once = True
    with pytest.raises(RuntimeError):
        ctl.dispatch_pending("c1")
    # Simulate a new Python process opening a durable store, not an in-memory reset.
    recovered = CampaignController(SqliteHarnessStore(store.path), clock, queue, MockVerifier())
    assert recovered.dispatch_pending("c1") == 1
    assert recovered.dispatch_pending("c1") == 0
    assert recovered.admit_cycle("c1", "c1:1", 1)
    assert not recovered.admit_cycle("c1", "c1:1", 1)
    assert len(queue.keys) == 1


def test_duplicate_delivery_never_resends_effect(rig):
    ctl = start(rig)
    assert ctl.admit_effect("c1", "c1:1", "intent1", "targetA", 1)
    assert not ctl.admit_effect("c1", "c1:1", "intent1", "targetA", 1)
    with pytest.raises(Rejected, match="INTENT_ID_CONFLICT"):
        ctl.admit_effect("c1", "c1:1", "intent1", "targetB", 1)
    with pytest.raises(Rejected, match="EFFECT_BARRIER"):
        ctl.admit_effect("c1", "c1:1", "intent2", "targetA", 1)
    ctl.record_effect("c1", "intent1", EffectState.CONFIRMED, "ledger:receipt")
    assert not ctl.admit_effect("c1", "c1:1", "intent1", "targetA", 1)


def test_unknown_effect_barrier_and_reconcile(rig):
    ctl = start(rig)
    ctl.admit_effect("c1", "c1:1", "intent1", "targetA", 1)
    ctl.record_effect("c1", "intent1", EffectState.UNKNOWN)
    assert ctl.view("c1")["state"] == "WAITING_RECONCILIATION"
    with pytest.raises(Rejected):
        ctl.advance("c1", "c1:1", Phase.EXECUTE, Phase.VERIFY, 1)
    ctl.record_effect("c1", "intent1", EffectState.NOT_SENT, "inquiry:proved-never-sent")
    assert ctl.view("c1")["state"] == "PAUSED"
    ctl.control("c1", "RESUME", 1)
    assert ctl.advance("c1", "c1:1", Phase.EXECUTE, Phase.VERIFY, 2)


def test_stop_with_unresolved_effect_does_not_promise_stopped(rig):
    ctl = start(rig)
    ctl.admit_effect("c1", "c1:1", "intent1", "targetA", 1)
    ctl.control("c1", "STOP", 1)
    assert ctl.view("c1")["state"] == "STOP_REQUESTED"
    ctl.record_effect("c1", "intent1", EffectState.UNKNOWN)
    assert ctl.view("c1")["state"] == "STOP_REQUESTED"
    ctl.record_effect("c1", "intent1", EffectState.CONFIRMED, "provider:authoritative")
    assert ctl.view("c1")["state"] == "STOPPED"


def test_budget_insufficient_not_overbooked(rig):
    ctl, _store, clock, _queue = rig
    ctl.create(plan(clock, max_cost_micro_usd=100), "work1")
    ctl.dispatch_pending("c1")
    ctl.admit_cycle("c1", "c1:1", 1)
    ctl.advance("c1", "c1:1", Phase.PLAN, Phase.EXECUTE, 1)
    verify(ctl, next_work_item_id="work2")
    assert ctl.view("c1")["state"] == "LIMIT_REACHED"
    assert ctl.view("c1")["cycleCount"] == 1
    assert ctl.view("c1")["spentMicroUsd"] == "50"


def test_unknown_usage_held_no_free_refund(rig):
    ctl = start(rig)
    ctl.advance("c1", "c1:1", Phase.EXECUTE, Phase.VERIFY, 1)
    result = ctl.complete_cycle(
        "c1",
        "c1:1",
        claimed_success=True,
        evidence_refs=["a"],
        quality_gate_ref="gate",
        cost_micro_usd=None,
        tokens=None,
        tools=None,
        next_work_item_id="work2",
    )
    assert result == "UNVERIFIED"
    v = ctl.view("c1")
    assert v["heldMicroUsd"] == "100"
    assert v["cycleCount"] == 1
    assert v["state"] == "WAITING_RECONCILIATION"


def test_model_success_not_verified_without_trusted_proof(rig):
    ctl = start(rig)
    ctl.advance("c1", "c1:1", Phase.EXECUTE, Phase.VERIFY, 1)
    assert (
        ctl.complete_cycle(
            "c1",
            "c1:1",
            claimed_success=True,
            evidence_refs=["model:claimed"],
            quality_gate_ref="model:claimed",
            cost_micro_usd=0,
            tokens=0,
            tools=0,
        )
        == ResultState.UNVERIFIED
    )
    assert ctl.view("c1")["completedCount"] == 0


def test_target_verified_then_no_extra_cycle(rig):
    ctl, _store, clock, _queue = rig
    ctl.create(plan(clock, target_count=1), "work1")
    ctl.dispatch_pending("c1")
    ctl.admit_cycle("c1", "c1:1", 1)
    ctl.advance("c1", "c1:1", Phase.PLAN, Phase.EXECUTE, 1)
    verify(ctl, next_work_item_id="work2")
    assert ctl.view("c1")["state"] == "TARGET_REACHED"
    assert ctl.view("c1")["cycleCount"] == 1


def test_approval_pending_blocks_new_admission(rig):
    ctl, _store, clock, _queue = rig
    ctl.create(plan(clock), "work1")
    ctl.control("c1", "APPROVAL_PENDING", 1)
    assert ctl.dispatch_pending("c1") == 0
    ctl.control("c1", "APPROVAL_GRANTED", 2)
    ctl.control("c1", "RESUME", 3)
    assert ctl.admit_cycle("c1", "c1:1", 4)


def test_limits_must_be_finite():
    with pytest.raises(Rejected):
        StopPolicy(0, "2026-11-11T00:00:00Z", 100, 100, 5)
    with pytest.raises(Rejected):
        StopPolicy(1, "not-date", 100, 100, 5)
    with pytest.raises(Rejected):
        StopPolicy(1, "2026-11-11T00:00:00", 100, 100, 5)


def test_duplicate_control_command_and_cas(rig):
    ctl, _store, clock, _queue = rig
    ctl.create(plan(clock), "work1")
    assert ctl.control("c1", "STOP", 1, command_id="ui-stop-1") == 2
    assert ctl.control("c1", "STOP", 1, command_id="ui-stop-1") == 2
    assert len([x for x in ctl.view("c1")["audit"] if x["reason"] == "STOP"]) == 1
    with pytest.raises(Rejected, match="CONTROL_COMMAND_CONFLICT"):
        ctl.control("c1", "PAUSE", 1, command_id="ui-stop-1")
    with pytest.raises(Rejected, match="STALE_CONTROL_VERSION"):
        ctl.control("c1", "STOP", 1, command_id="ui-stop-2")


def test_outbox_relay_rejects_if_stop_wins_race(rig):
    ctl, store, clock, _queue = rig
    ctl.create(plan(clock), "work1")

    class GuardedQueue(MockQueue):
        def request_run(self, **kw):
            # Simulated competing STOP commits after relay's initial check.
            ctl.control("c1", "STOP", 1)
            campaign = store.read("c1")
            if campaign.control_version != kw["control_version"] or campaign.state != CampaignState.ACTIVE:
                return False
            return super().request_run(**kw)

    guarded = GuardedQueue()
    ctl.queue = guarded
    assert ctl.dispatch_pending("c1") == 0
    assert not guarded.deliveries
    assert ctl.view("c1")["state"] == "STOPPED"


def test_restart_before_effect_ack_never_resends(rig):
    ctl = start(rig)
    ctl.admit_effect("c1", "c1:1", "external-intent", "targetA", 1)
    recovered = CampaignController(SqliteHarnessStore(rig[1].path), rig[2], rig[3], MockVerifier())
    assert not recovered.admit_effect("c1", "c1:1", "external-intent", "targetA", 1)
    with pytest.raises(Rejected, match="EFFECT_BARRIER"):
        recovered.admit_effect("c1", "c1:1", "new-external-intent", "targetA", 1)
    # Do not claim a request was never sent merely because the worker died.
    recovered.record_effect("c1", "external-intent", EffectState.UNKNOWN)
    assert recovered.view("c1")["state"] == "WAITING_RECONCILIATION"


def test_no_next_candidate_parks_without_spin(rig):
    ctl = start(rig)
    verify(ctl)
    assert ctl.view("c1")["state"] == "WAITING_INPUT"
    assert ctl.dispatch_pending("c1") == 0


def test_no_verifier_defaults_fail_closed(tmp_path):
    clock = FakeClock(datetime(2026, 11, 11, tzinfo=UTC))
    ctl = CampaignController(SqliteHarnessStore(tmp_path / "x.sqlite"), clock, MockQueue())
    ctl.create(plan(clock), "work1")
    ctl.admit_cycle("c1", "c1:1", 1)
    ctl.advance("c1", "c1:1", Phase.PLAN, Phase.EXECUTE, 1)
    ctl.advance("c1", "c1:1", Phase.EXECUTE, Phase.VERIFY, 1)
    result = ctl.complete_cycle(
        "c1",
        "c1:1",
        claimed_success=True,
        evidence_refs=["model:not-authoritative"],
        quality_gate_ref="gate:pass",
        cost_micro_usd=0,
        tokens=0,
        tools=0,
    )
    assert result == ResultState.UNVERIFIED
    assert ctl.view("c1")["completedCount"] == 0
