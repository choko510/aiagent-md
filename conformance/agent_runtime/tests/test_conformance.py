"""AgentRuntimePort conformance (PYRT-T01..T14). Same FakeModel / gateways / PostgreSQL / fixtures for A and B.

Every test drives real worker subprocesses; "kill" is os._exit(137) at a named fault point.
"""

from __future__ import annotations

import re

import pytest

from aiagent_conformance.control import Claim, FencedOut
from aiagent_conformance.fixtures import LONG_HISTORY
from aiagent_conformance.gateways import FAKE_PROVIDER_SECRET

from .conftest import Harness


def _approve_and_finish(h: Harness, rid: str, policy: str = "notify", **kw) -> dict:
    out = h.worker(rid, policy)
    assert out["status"] == "AWAITING_APPROVAL", out
    h.approve_all(rid)
    return h.worker(rid, policy, **kw)


# ---------------------------------------------------------------- baseline
def test_t00_happy_path_with_approval(h: Harness) -> None:
    rid = h.new_run()
    out = _approve_and_finish(h, rid)
    assert out["status"] == "FINAL" and out["text"] == "done:CONFIRMED", out
    assert len(h.sends(rid)) == 1
    assert [c["admitted"] for c in h.model_calls(rid)] == [True, True, True]


# ---------------------------------------------------------------- PYRT-T01 worker kill / restart
def test_t01_kill_after_model_response_then_restart(h: Harness) -> None:
    rid = h.new_run()
    assert h.worker(rid, fault="after_model_response")["status"] == "KILLED"
    out = h.worker(rid)
    assert out["status"] == "AWAITING_APPROVAL", out
    h.approve_all(rid)
    assert h.worker(rid)["text"] == "done:CONFIRMED"
    assert len(h.sends(rid)) == 1
    # the answer lost before the HEAD commit is paid for again (measured, not hidden)
    assert len(h.model_calls(rid)) == 4


def test_t01b_kill_after_read_tool_then_restart(h: Harness) -> None:
    rid = h.new_run()
    assert h.worker(rid, fault="after_tool_ack:search")["status"] == "KILLED"
    out = h.worker(rid)
    assert out["status"] == "AWAITING_APPROVAL", out
    search = [row for row in h.ctl.ledger(rid) if row["tool"] == "search"]
    assert len(search) == 1 and search[0]["attempts"] == 2  # replayed with the same intent key
    assert len(h.model_calls(rid)) == 2  # no re-inference: proposal replayed from HEAD / framework


# ---------------------------------------------------------------- PYRT-T06 ACK loss right after the external send
def test_t06_ack_lost_after_external_send(h: Harness) -> None:
    rid = h.new_run()
    assert h.worker(rid)["status"] == "AWAITING_APPROVAL"
    h.approve_all(rid)
    assert h.worker(rid, fault="after_tool_ack:send_message")["status"] == "KILLED"
    assert len(h.sends(rid)) == 1
    out = h.worker(rid)
    assert out["status"] == "FINAL" and out["text"] == "done:CONFIRMED", out
    assert len(h.sends(rid)) == 1, "external operation duplicated on replay"


def test_t07_gateway_crash_between_send_and_confirm_is_unknown(h: Harness) -> None:
    rid = h.new_run()
    assert h.worker(rid)["status"] == "AWAITING_APPROVAL"
    h.approve_all(rid)
    assert h.worker(rid, fault="gateway_after_send")["status"] == "KILLED"
    for _ in range(2):  # never blind-retried, however often the run is resumed
        out = h.worker(rid)
        assert out["status"] == "UNKNOWN_EFFECT", out
    assert len(h.sends(rid)) == 1
    assert [row["status"] for row in h.ctl.ledger(rid) if row["tool"] == "send_message"] == ["UNKNOWN"]
    assert h.ctl.run(rid)["status"] == "NEEDS_RECONCILE"


def test_t12_negative_control_framework_durability_alone_duplicates(h: Harness) -> None:
    """Without the Effect Ledger, framework durability does NOT give exactly-once external effects."""
    rid = h.new_run()
    assert h.worker(rid, args=["--naive"], fault="after_tool_ack:send_message")["status"] == "KILLED"
    assert h.worker(rid, args=["--naive"])["status"] == "FINAL"
    assert h.naive_sends(rid) == 2


# ---------------------------------------------------------------- PYRT-T02 approval wait / reject / expiry / forged resume
def test_t02a_reject(h: Harness) -> None:
    rid = h.new_run()
    assert h.worker(rid)["status"] == "AWAITING_APPROVAL"
    h.approve_all(rid, "REJECTED")
    out = h.worker(rid)
    assert out["text"] == "done:DENIED", out
    assert h.sends(rid) == []


def test_t02b_expired_approval(h: Harness) -> None:
    rid = h.new_run()
    assert h.worker(rid)["status"] == "AWAITING_APPROVAL"
    ids = h.approve_all(rid)
    for i in ids:
        h.ctl.expire(i)
    out = h.worker(rid)
    assert out["text"] == "done:EXPIRED", out
    assert h.sends(rid) == []


def test_t02c_resume_without_decision_does_not_execute(h: Harness) -> None:
    """The framework is resumed as if approved (A: DeferredToolResults(True), B: Command(resume=...)):
    the framework's approval state is not authority."""
    rid = h.new_run()
    assert h.worker(rid)["status"] == "AWAITING_APPROVAL"
    out = h.worker(rid)
    assert out["status"] == "AWAITING_APPROVAL", out
    assert h.sends(rid) == []


# ---------------------------------------------------------------- PYRT-T03 Goal revision
def test_t03a_goal_change_while_waiting_rejects_old_intent(h: Harness) -> None:
    rid = h.new_run()
    assert h.worker(rid)["status"] == "AWAITING_APPROVAL"
    h.approve_all(rid)
    h.ctl.steer(rid)
    out = h.worker(rid)
    assert out["text"] == "done:STALE_GOAL", out
    assert h.sends(rid) == []


def test_t03b_stale_worker_cannot_dispatch_model(h: Harness) -> None:
    rid = h.new_run()
    out = h.worker(rid, args=["--stale-claim"])
    assert out["status"] == "STALEGOAL", out
    calls = h.model_calls(rid)
    assert calls and not any(c["admitted"] for c in calls)
    assert h.ctl.ledger(rid) == []


def test_t03c_fenced_worker_cannot_write_head(h: Harness) -> None:
    rid = h.new_run()
    old = h.ctl.claim(rid, "w-old", lease_s=0.01)
    h.ctl.claim(rid, "w-new", force=True)
    with pytest.raises(FencedOut):
        h.ctl.append_head(old, "assistant", {"kind": "assistant", "content": "x", "tool_calls": []}, request_key="k")
    assert isinstance(old, Claim)


# ---------------------------------------------------------------- PYRT-T04 framework checkpoint corruption
@pytest.mark.parametrize("mode", ["garbage", "swap"])
def test_t04_corrupted_framework_state_is_rebuilt_from_head(h: Harness, mode: str) -> None:
    rid = h.new_run()
    assert h.worker(rid, fault="after_tool_ack:search")["status"] == "KILLED"
    adapter_cls = _adapter(h)
    n = adapter_cls.corrupt_static(h.ctl, rid, mode)
    assert n > 0, "nothing was corrupted"
    out = h.worker(rid)
    assert out["status"] == "AWAITING_APPROVAL", out
    assert out["rebuilds"], "corruption was not detected"
    h.approve_all(rid)
    assert h.worker(rid)["text"] == "done:CONFIRMED"
    sends = h.sends(rid)
    assert len(sends) == 1 and sends[0]["payload"]["to"] == "alice"
    assert all(row["result"] is None or "wodget" not in str(row["result"]) for row in h.ctl.ledger(rid))


@pytest.mark.parametrize("mode", ["text", "toolresult"])
def test_t04b_altered_checkpoint_content_is_not_trusted(h: Harness, mode: str) -> None:
    """P1-02: assistant text / tool result / final answer in the framework state are compared with HEAD in
    order and content before reuse; the final answer always comes from HEAD."""
    if h.cand == "a" and mode == "toolresult":
        pytest.skip("DBOS persists only model-request steps; function-tool results are not stored, so not corruptible")
    rid = h.new_run()
    assert h.worker(rid)["status"] == "AWAITING_APPROVAL"
    h.approve_all(rid)
    assert h.worker(rid, fault="before_segment_return")["status"] == "KILLED"
    n = _adapter(h).corrupt_static(h.ctl, rid, mode)
    assert n > 0, "nothing was corrupted"
    out = h.worker(rid)
    assert out["status"] == "FINAL" and out["text"] == "done:CONFIRMED", out
    assert out["rebuilds"], "altered framework content was not detected"
    assert len(h.sends(rid)) == 1


def _adapter(h: Harness):
    if h.cand == "a":
        from candidates.corruption import CorruptA

        return CorruptA
    from candidates.corruption import CorruptB

    return CorruptB


# ---------------------------------------------------------------- PYRT-T08 model gateway adapter / tool surface / secrets
def test_t08_gateway_only_tool_surface_and_no_credentials(h: Harness) -> None:
    rid = h.new_run()
    _approve_and_finish(h, rid)
    seen = set().union(*(set(c["tools"] or []) for c in h.model_calls(rid)))
    allowed = {"search", "send_message"} | ({"read_file"} if h.cand == "b" else set())
    assert seen <= allowed, f"framework exposed extra tools: {seen - allowed}"
    assert not seen & {"execute", "write_file", "edit_file", "ls", "glob", "grep", "shell", "run_code"}
    dump = _framework_dump(h)
    assert FAKE_PROVIDER_SECRET not in dump
    assert not re.search(r"sk-[A-Za-z0-9-]{8,}", dump)


def _framework_dump(h: Harness) -> str:
    schemas = ["dbos"] if h.cand == "a" else ["public"]
    out = []
    for s in schemas:
        for t in h.ctl.db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s", (s,)).fetchall():
            for row in h.ctl.db.execute(f'SELECT * FROM "{s}"."{t["table_name"]}"').fetchall():
                out.append(" ".join(v.decode("latin1") if isinstance(v, (bytes, memoryview)) and not isinstance(v, memoryview) else str(v) for v in row.values()))
    return "\n".join(out)


# ---------------------------------------------------------------- PYRT-T09 subagent + context compaction
def test_t09b_framework_internal_tool_results_are_in_head(h: Harness) -> None:
    """Delegation results (`task` / `delegate_task`) are recorded in HEAD, so the full transcript check passes."""
    rid = h.new_run("delegate")
    out = h.worker(rid, "delegate")
    assert out["status"] == "FINAL" and out["text"] == "done:delegated" and out["rebuilds"] == [], out
    tools = [e["payload"]["tool_name"] for e in h.ctl.head(rid, "main") if e["kind"] == "tool"]
    assert tools in (["task"], ["delegate_task"]), tools


def test_t09_subagent_and_compaction(h: Harness) -> None:
    rid = h.new_run("delegate", history=LONG_HISTORY)
    out = h.worker(rid, "delegate", args=["--compaction"])
    assert out["status"] == "FINAL" and out["text"] == "done:delegated", out
    if h.cand == "b":
        # DeepAgents summarization is non-mutating: the checkpoint still equals the raw HEAD transcript.
        assert out["rebuilds"] == [], out
    else:
        # Finding: pydantic-ai-harness SummarizingCompaction rewrites the run's message history, so the
        # framework transcript cannot be verified against raw HEAD; the adapter falls back to HEAD.
        assert out["rebuilds"] and "differs from HEAD" in out["rebuilds"][0], out
    calls = h.model_calls(rid)
    streams = {c["stream"] for c in calls}
    assert "subagent:researcher" in streams, streams
    assert "summarize" in streams, f"compaction did not fire: {streams}"
    assert all(c["admitted"] for c in calls)
    child = [c for c in calls if c["stream"] == "subagent:researcher"]
    assert all(set(c["tools"] or []) <= {"search", "read_file"} for c in child), child
    led = h.ctl.ledger(rid)
    assert [row["tool"] for row in led] == ["search"] and led[0]["root_run_id"] == rid  # child tool went through the gateway
    main = h.ctl.transcript(rid)
    assert sum(1 for e in main if "[history" in str(e.get("content"))) == len(LONG_HISTORY), "HEAD lost raw history"
    assert not any("SUMMARY of" in str(e.get("content")) for e in main), "summary leaked into the canonical transcript"


# ---------------------------------------------------------------- PYRT-T10 queue / workflow connection
def test_t10_queue_dedup_and_redelivery_after_kill(h: Harness) -> None:
    rid = h.new_run()
    assert h.worker(rid, args=["--via-queue"], fault="after_tool_ack:search")["status"] == "KILLED"
    out = h.worker(rid, args=["--via-queue"])
    assert out["status"] == "AWAITING_APPROVAL", out
    search = [row for row in h.ctl.ledger(rid) if row["tool"] == "search"]
    assert len(search) == 1
    if h.cand == "b":
        assert out["queue"]["attempts"] == 2 and out["queue"]["acked"] is True, out
    h.approve_all(rid)
    assert h.worker(rid)["text"] == "done:CONFIRMED"
    assert len(h.sends(rid)) == 1


def test_t10b_duplicate_enqueue_rejected(h: Harness) -> None:
    rid = h.new_run()
    out = h.worker(rid, args=["--via-queue"])
    assert out["status"] == "AWAITING_APPROVAL", out
    assert out["queue"]["deduplicated"] == 1, out


# ---------------------------------------------------------------- PYRT-T11 parallel write tool calls
@pytest.mark.parametrize("mode", ["framework_default", "sequential"])
def test_t11_parallel_writes_serialized_by_gateway(h: Harness, mode: str) -> None:
    rid = h.new_run("parallel_writes")
    args = ["--parallel-mode", "parallel_ordered_events" if mode == "framework_default" else "sequential"]
    out = h.worker(rid, "parallel_writes", args=args)
    assert out["status"] == "AWAITING_APPROVAL", out
    h.approve_all(rid)
    out = h.worker(rid, "parallel_writes", args=args)
    assert out["text"] == "done:parallel", out
    assert sorted(s["payload"]["to"] for s in h.sends(rid)) == ["alice", "bob"]
    inflight = h.ctl.db.execute("SELECT max_inflight_writes FROM aiagent.tool_concurrency WHERE run_id=%s", (rid,)).fetchone()
    if mode == "sequential":
        assert inflight["max_inflight_writes"] == 1
    print(f"[measure] {h.cand} {mode}: max framework-side concurrent write calls = {inflight['max_inflight_writes']}")
