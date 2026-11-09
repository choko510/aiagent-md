"""PR #80 review follow-up: PYRT-T13..T17.

T13 model snapshot identity + admission on replayed answers (P1-01)
T14 Steer / Cancel / Revoke racing the final tool admission (P1-03)
T15 several runs and tenants concurrently in one worker process (P1-04)
T16 QueuePort: attempt fence, ACK loss, re-enqueue after approval, multi-worker contention (P1-05)
T17 cancellation during model / subagent / waiting (P2)
"""

from __future__ import annotations

import time

import pytest

from aiagent_conformance.control import Cancelled, StaleGoal
from aiagent_conformance.fixtures import USER_TEXT
from aiagent_conformance.gateways import ModelGateway, PolicyDenied, SealedModelRequest, sealed_model_call

from .conftest import Harness


# ---------------------------------------------------------------- T13 (P1-01)
def test_t13_cached_answer_needs_same_snapshot_and_fresh_admission(h: Harness) -> None:
    rid = h.new_run()
    ctl, gw = h.ctl, ModelGateway(h.ctl)
    msgs = [{"kind": "user", "content": USER_TEXT["notify"]}]
    c1 = ctl.claim(rid, "t13")
    a1 = sealed_model_call(ctl, gw, SealedModelRequest(c1, "main", msgs, ["search", "send_message"], "notify"))
    again = sealed_model_call(ctl, gw, SealedModelRequest(c1, "main", msgs, ["search", "send_message"], "notify"))
    assert again == a1  # same snapshot: replayed from HEAD
    assert [r["cached"] for r in h.model_calls(rid, include_cached=True)] == [False, True]

    # A different tool surface is a different snapshot: no reuse.
    sealed_model_call(ctl, gw, SealedModelRequest(c1, "main", msgs, ["search"], "notify"))
    assert len(h.model_calls(rid)) == 2

    # Goal changes; the *same input* is re-sent.
    ctl.steer(rid)
    c2 = ctl.claim(rid, "t13")
    a2 = sealed_model_call(ctl, gw, SealedModelRequest(c2, "main", msgs, ["search", "send_message"], "notify"))
    assert a2["tool_calls"][0]["id"] != a1["tool_calls"][0]["id"], "answer from the old Goal revision was reused"
    assert h.model_calls(rid)[-1]["goal_revision"] == 2

    # A worker still holding the old-goal snapshot hits the cache but is refused at admission.
    with pytest.raises(Exception) as ei:
        sealed_model_call(ctl, gw, SealedModelRequest(c1, "main", msgs, ["search", "send_message"], "notify"))
    assert type(ei.value).__name__ == "FencedOut"
    last = h.model_calls(rid, include_cached=True)[-1]
    assert last["cached"] and not last["admitted"]

    # Model policy and cancel are re-checked on a cache hit too.
    ctl.db.execute("UPDATE aiagent.runs SET allowed_models = '{}' WHERE run_id = %s", (rid,))
    with pytest.raises(PolicyDenied):
        sealed_model_call(ctl, gw, SealedModelRequest(c2, "main", msgs, ["search", "send_message"], "notify"))
    ctl.db.execute("UPDATE aiagent.runs SET allowed_models = '{fake-gateway}' WHERE run_id = %s", (rid,))
    ctl.cancel(rid)
    with pytest.raises(Cancelled):
        sealed_model_call(ctl, gw, SealedModelRequest(c2, "main", msgs, ["search", "send_message"], "notify"))


def test_t13b_old_goal_claim_on_cached_answer_is_stale(h: Harness) -> None:
    rid = h.new_run()
    ctl, gw = h.ctl, ModelGateway(h.ctl)
    msgs = [{"kind": "user", "content": USER_TEXT["notify"]}]
    c1 = ctl.claim(rid, "t13b")
    sealed_model_call(ctl, gw, SealedModelRequest(c1, "main", msgs, ["search"], "notify"))
    ctl.steer(rid)  # same fence, newer goal
    with pytest.raises(StaleGoal):
        sealed_model_call(ctl, gw, SealedModelRequest(c1, "main", msgs, ["search"], "notify"))
    assert h.model_calls(rid, include_cached=True)[-1]["reason"] == "STALE_GOAL"


# ---------------------------------------------------------------- T14 (P1-03)
@pytest.mark.parametrize("action,expected", [("steer", "STALE_GOAL"), ("cancel", "CANCELLED"), ("revoke", "REVOKED")])
def test_t14_concurrent_change_before_final_admission_blocks_send(h: Harness, action: str, expected: str) -> None:
    rid = h.new_run()
    assert h.worker(rid)["status"] == "AWAITING_APPROVAL"
    h.approve_all(rid)
    out = h.worker(rid, race=f"gateway_before_final_admission:{action}")
    assert h.sends(rid) == [], f"old operation was sent after a concurrent {action}"
    assert [r["status"] for r in h.ctl.ledger(rid) if r["tool"] == "send_message"] == [expected]
    assert out["status"] in {"steer": ("STALEGOAL",), "cancel": ("CANCELLED",), "revoke": ("FINAL",)}[action], out


def test_t14b_change_after_final_admission_does_not_unsend(h: Harness) -> None:
    """The SENDING commit is the linearization point: a Steer committed after it cannot stop that send,
    and the ledger records it as sent (no silent retry, no silent drop)."""
    rid = h.new_run()
    assert h.worker(rid)["status"] == "AWAITING_APPROVAL"
    h.approve_all(rid)
    h.worker(rid, race="gateway_after_final_admission:steer")
    assert len(h.sends(rid)) == 1
    assert [r["status"] for r in h.ctl.ledger(rid) if r["tool"] == "send_message"] == ["CONFIRMED"]


# ---------------------------------------------------------------- T15 (P1-04)
def test_t15_concurrent_runs_and_tenants_in_one_process(h: Harness) -> None:
    r1 = h.new_run("notify", tenant="t1")
    r2 = h.new_run("parallel_writes", tenant="t2")
    r3 = h.new_run("notify", tenant="t2")
    runs = [r1, r2, r3]
    out = h.worker(",".join(runs), "notify,parallel_writes,notify")
    assert {r: o["status"] for r, o in out["runs"].items()} == dict.fromkeys(runs, "AWAITING_APPROVAL"), out
    for rid in runs:
        tenant = h.ctl.run(rid)["tenant_id"]
        head = h.ctl.head(rid)
        assert all(e["run_id"] == rid for e in head)
        assert {c["tenant_id"] for c in h.model_calls(rid, include_cached=True)} == {tenant}
        assert {row["tenant_id"] for row in h.ctl.ledger(rid)} == {tenant}
    first = {rid: [e for e in h.ctl.head(rid) if e["kind"] == "assistant"][0]["payload"]["tool_calls"] for rid in runs}
    assert [tc["name"] for tc in first[r1]] == ["search"] and [tc["name"] for tc in first[r3]] == ["search"]
    assert sorted(tc["args"]["to"] for tc in first[r2]) == ["alice", "bob"]
    for rid in runs:
        h.approve_all(rid)
    out = h.worker(",".join(runs), "notify,parallel_writes,notify")
    assert out["runs"][r1]["text"] == "done:CONFIRMED" and out["runs"][r3]["text"] == "done:CONFIRMED", out
    assert out["runs"][r2]["text"] == "done:parallel", out
    assert len(h.sends(r1)) == 1 and len(h.sends(r3)) == 1 and len(h.sends(r2)) == 2


# ---------------------------------------------------------------- T16 (P1-05)
def test_t16a_attempt_fence_rejects_stale_ack(h: Harness) -> None:
    rid = h.new_run()
    assert h.ctl.enqueue(rid, f"{rid}:start")
    j1 = h.ctl.dequeue("w1", lease_s=0.3, run_id=rid)
    time.sleep(0.5)  # lease expires, job is redelivered to another worker
    j2 = h.ctl.dequeue("w2", lease_s=30, run_id=rid)
    assert j2["job_id"] == j1["job_id"] and j2["attempts"] == 2 and j2["attempt_id"] != j1["attempt_id"]
    assert h.ctl.ack(j1) is False, "stale attempt completed the job"
    assert h.ctl.ack(j2) is True
    assert h.ctl.dequeue("w3", run_id=rid) is None


def test_t16b_ack_lost_then_redelivered_and_resumed_after_approval(h: Harness) -> None:
    rid = h.new_run()
    h.ctl.enqueue(rid, f"{rid}:start")
    outs = h.queue_workers(1, fault="before_job_ack")
    assert outs[0]["status"] == "KILLED"
    outs = h.queue_workers(1)
    jobs = [j for o in outs for j in o["jobs"] if j["run"] == rid]
    assert [(j["attempts"], j["status"], j["acked"]) for j in jobs] == [(2, "AWAITING_APPROVAL", True)], outs
    assert len(h.ctl.approvals(rid)) == 1
    assert len(h.model_calls(rid)) == 2, "redelivery re-ran the model instead of resuming"
    apr = h.ctl.approvals(rid)[0]["approval_id"]
    assert h.ctl.decide_and_enqueue(apr, "APPROVED") is True
    assert h.ctl.decide_and_enqueue(apr, "APPROVED") is False  # duplicate decision: no second resume job
    outs = h.queue_workers(1)
    jobs = [j for o in outs for j in o["jobs"] if j["run"] == rid]
    assert [(j["kind"], j["status"]) for j in jobs] == [("resume", "FINAL")], outs
    assert len(h.sends(rid)) == 1
    assert h.ctl.count("jobs", "run_id=%s AND status <> 'DONE' AND kind='resume'", (rid,)) == 0


def test_t16c_multi_worker_contention(h: Harness) -> None:
    runs = [h.new_run() for _ in range(8)]
    for rid in runs:
        h.ctl.enqueue(rid, f"{rid}:start")
    outs = h.queue_workers(3)
    jobs = [j for o in outs for j in o["jobs"] if j["run"] in runs]
    assert sorted(j["run"] for j in jobs) == sorted(runs), "a job was lost or processed twice"
    assert all(j["attempts"] == 1 and j["status"] == "AWAITING_APPROVAL" for j in jobs), jobs
    for rid in runs:
        assert len(h.ctl.approvals(rid)) == 1
        h.ctl.decide_and_enqueue(h.ctl.approvals(rid)[0]["approval_id"], "APPROVED")
    outs = h.queue_workers(3)
    jobs = [j for o in outs for j in o["jobs"] if j["run"] in runs]
    assert sorted(j["run"] for j in jobs) == sorted(runs)
    assert all(j["status"] == "FINAL" for j in jobs), jobs
    assert [len(h.sends(rid)) for rid in runs] == [1] * len(runs)


# ---------------------------------------------------------------- T17 (P2 cancellation)
def test_t17a_cancel_while_waiting_approval(h: Harness) -> None:
    rid = h.new_run()
    assert h.worker(rid)["status"] == "AWAITING_APPROVAL"
    h.approve_all(rid)
    h.ctl.cancel(rid)
    assert h.worker(rid)["status"] == "CANCELLED"
    assert h.sends(rid) == []


def test_t17b_cancel_during_model_call(h: Harness) -> None:
    rid = h.new_run()
    out = h.worker(rid, race="model_before_admission@main:cancel")
    assert out["status"] == "CANCELLED", out
    calls = h.model_calls(rid)
    assert [c["reason"] for c in calls] == ["CANCELLED"] and h.ctl.ledger(rid) == []


def test_t17c_cancel_inside_subagent(h: Harness) -> None:
    rid = h.new_run("delegate")
    out = h.worker(rid, "delegate", race="model_before_admission@subagent:researcher:cancel")
    assert out["status"] == "CANCELLED", out
    assert h.ctl.ledger(rid) == [], "the subagent still executed a tool after cancel"
    assert not any(c["admitted"] for c in h.model_calls(rid) if c["stream"] == "subagent:researcher")


# ---------------------------------------------------------------- PR #80 review round 2
def test_t17d_cancel_committed_just_before_completion_is_not_overwritten(h: Harness) -> None:
    """Worker finishes (FINAL), the user cancels, then the worker records completion: CANCELLED must win."""
    rid = h.new_run()
    assert h.worker(rid)["status"] == "AWAITING_APPROVAL"
    h.approve_all(rid)
    out = h.worker(rid, race="before_record_status:cancel")
    assert out["status"] == "FINAL" and out["recorded"] is False, out
    assert h.ctl.run(rid)["status"] == "CANCELLED"
    assert len(h.sends(rid)) == 1  # admitted before the cancel: recorded as sent, not silently dropped
    again = h.worker(rid)  # a later worker can neither reopen nor overwrite the terminal state
    assert again["status"] == "CANCELLED" and h.ctl.run(rid)["status"] == "CANCELLED"


def test_t17e_terminal_states_are_final(h: Harness) -> None:
    from aiagent_conformance.control import FencedOut, RunTerminal

    rid = h.new_run()
    c = h.ctl.claim(rid, "t17e")
    h.ctl.steer(rid)
    with pytest.raises(StaleGoal):  # completion computed for an old Goal revision is not recorded
        h.ctl.set_status(c, "SUCCEEDED")
    c = h.ctl.claim(rid, "t17e")
    h.ctl.set_status(c, "SUCCEEDED")
    assert h.ctl.cancel(rid) is False and h.ctl.run(rid)["status"] == "SUCCEEDED"  # cancel after success: no-op
    with pytest.raises(RunTerminal):
        h.ctl.set_status(c, "WAITING_APPROVAL")
    with pytest.raises(RunTerminal):
        h.ctl.claim(rid, "t17e-other", force=True)
    rid2 = h.new_run()
    c2 = h.ctl.claim(rid2, "t17e")
    assert h.ctl.cancel(rid2) is True
    with pytest.raises(Cancelled):
        h.ctl.set_status(c2, "SUCCEEDED")
    old = h.ctl.claim(h.new_run(), "w-old")
    h.ctl.claim(old.run_id, "w-new", force=True)
    with pytest.raises(FencedOut):
        h.ctl.set_status(old, "SUCCEEDED")


def test_t18_stale_worker_cannot_erase_newer_checkpoint_pointer(h: Harness) -> None:
    """The old worker hits corrupted framework state and is about to drop framework_ref while a newer worker
    has just re-claimed the run and written its own pointer: the newer pointer must survive."""
    from aiagent_conformance.control import RACE_NEW_REF

    from .test_conformance import _adapter

    rid = h.new_run()
    assert h.worker(rid, fault="after_tool_ack:search")["status"] == "KILLED"
    assert _adapter(h).corrupt_static(h.ctl, rid, "garbage") > 0
    fence_before = h.ctl.run(rid)["fence"]
    out = h.worker(rid, race="before_discard_framework_ref:reclaim")
    run = h.ctl.run(rid)
    assert run["framework_ref"] == RACE_NEW_REF, "stale worker erased the newer worker's checkpoint pointer"
    assert run["fence"] == fence_before + 2  # old worker's claim, then the newer worker's claim
    assert out["status"] in ("FENCEDOUT", "REBUILD"), out
    stale_writes = [e for e in h.ctl.head(rid) if e["fence"] == fence_before + 1]
    assert stale_writes == [], "the fenced-out worker still wrote to HEAD"


def test_t16d_ack_after_lease_expiry_without_redelivery_is_accepted(h: Harness) -> None:
    """Spec: lease expiry only makes a job eligible for redelivery; until someone re-claims it the attempt_id is
    unchanged and its ACK is accepted. After a re-claim the old ACK is rejected (T16a)."""
    rid = h.new_run()
    h.ctl.enqueue(rid, f"{rid}:start")
    job = h.ctl.dequeue("w1", lease_s=0.2, run_id=rid)
    time.sleep(0.4)
    assert h.ctl.ack(job) is True
    assert h.ctl.dequeue("w2", run_id=rid) is None, "a completed job was redelivered"
