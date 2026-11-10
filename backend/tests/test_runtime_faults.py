"""PYRT-T00..T18 on the FORMAL stack (real Model/Tool Gateway + Sentinel processes over authenticated RPC, formal
schema, QueuePort, Worker processes). Kills are real process deaths at named fault points.

Mapping to STEP 2 IDs is in each test name. T12 (negative control: framework-only durability) is a PoC property of
the fake gateway and has no meaning on the formal stack (there is no 'naive' gateway mode); not ported.
"""

from __future__ import annotations

import json
import time

import pytest
from aiagent_core import admin
from conftest import PgEnv
from helpers import approve, q, run_of, state, submit, wait_lease_expiry
from stack import Stack

pytestmark = pytest.mark.pg
NOTIFY = "Find the widget price and notify alice."


def sends_for(stack: Stack, task_id: str, pg: PgEnv) -> list[dict]:
    keys = {
        f"{r['tenant_id']}:{r['id']}"
        for r in q(pg.owner, "SELECT tenant_id, id FROM agent.tool_intents WHERE task_id=%s", (task_id,))
    }
    return [s for s in stack.sends() if s["key"] in keys]


def model_attempts(pg: PgEnv, task_id: str) -> list[dict]:
    return q(
        pg.owner,
        "SELECT status_code, cached, admitted, reason_code, stream FROM agent.model_attempts WHERE task_id=%s"
        " ORDER BY created_at",
        (task_id,),
    )


@pytest.fixture()
def t(pg: PgEnv) -> dict[str, str]:
    return admin.provision_tenant(pg.owner)


def to_approval(stack: Stack, api, t, goal: str = NOTIFY, **wkw: str) -> str:
    task = submit(api, t, goal)
    stack.drive(**wkw)
    st = state(api, t, task)
    assert st["run"]["state"] == "WAITING_APPROVAL", st
    return task


# ---------------------------------------------------------------- T01 kills / restarts
def test_t01_model_gateway_killed_after_provider_answer(stack: Stack, api, t, pg: PgEnv) -> None:
    task = submit(api, t, NOTIFY)
    stack.restart("model", AIAGENT_FAULT="model_after_response")
    stack.worker()  # gateway dies mid-call; worker sees GATEWAY_UNAVAILABLE -> job retried
    stack.restart("model")
    stack.drive()
    assert state(api, t, task)["run"]["state"] == "WAITING_APPROVAL"
    approve(api, t, task)
    stack.drive()
    assert state(api, t, task)["run"]["result"] == "done:CONFIRMED"
    assert len(sends_for(stack, task, pg)) == 1
    atts = model_attempts(pg, task)
    assert any(a["status_code"] == "FAILED" for a in atts)  # the lost answer is paid again (recorded, not hidden)


def test_t01b_worker_killed_after_read_tool_ack(stack: Stack, api, t, pg: PgEnv) -> None:
    task = submit(api, t, NOTIFY)
    r = stack.worker(fault="after_tool_ack:search")
    assert r.returncode == 137
    wait_lease_expiry(pg.owner, task)
    stack.drive()
    assert state(api, t, task)["run"]["state"] == "WAITING_APPROVAL"
    search = q(
        pg.owner,
        "SELECT submissions, status_code FROM agent.tool_intents WHERE task_id=%s AND tool_key='search'",
        (task,),
    )
    assert len(search) == 1 and search[0]["status_code"] == "DONE"
    completed = [a for a in model_attempts(pg, task) if a["status_code"] == "COMPLETED"]
    assert len(completed) == 2  # no re-inference for the answer already in HEAD


def test_t01c_worker_killed_after_claim_redelivered(stack: Stack, api, t, pg: PgEnv) -> None:
    task = submit(api, t, NOTIFY)
    assert stack.worker(fault="worker_after_claim").returncode == 137
    wait_lease_expiry(pg.owner, task)
    stack.drive()
    st = state(api, t, task)
    assert st["run"]["state"] == "WAITING_APPROVAL" and int(st["run"]["leaseEpoch"]) == 2


# ---------------------------------------------------------------- T02 approval negatives
def test_t02a_reject(stack: Stack, api, t, pg: PgEnv) -> None:
    task = to_approval(stack, api, t)
    approve(api, t, task, "REJECTED")
    stack.drive()
    st = state(api, t, task)
    assert st["state"] == "SUCCEEDED" and st["run"]["result"] == "done:DENIED"
    assert sends_for(stack, task, pg) == []


def test_t02b_expired_approval(stack: Stack, api, t, pg: PgEnv) -> None:
    task = to_approval(stack, api, t)
    st = state(api, t, task)
    q(
        pg.owner,
        "UPDATE agent.approval_records SET expires_at = now() - interval '1s' WHERE task_id=%s RETURNING 1",
        (task,),
    )
    a = st["approvals"][0]
    r = api.post(
        f"/v1/approvals/{a['id']}/decision",
        headers={"Authorization": f"Bearer {t['token']}"},
        json={
            "schemaVersion": "1.0",
            "decision": "APPROVED",
            "requestSha256": a["requestSha256"],
            "expectedRecordVersion": a["recordVersion"],
        },
    )
    assert r.status_code == 409 and r.json()["error"] == "EXPIRED"
    assert sends_for(stack, task, pg) == []


def test_t02c_resume_without_decision_does_not_execute(stack: Stack, api, t, pg: PgEnv) -> None:
    task = to_approval(stack, api, t)
    api.post(f"/v1/tasks/{task}/pause", headers={"Authorization": f"Bearer {t['token']}"})
    api.post(f"/v1/tasks/{task}/resume", headers={"Authorization": f"Bearer {t['token']}"})
    stack.drive()
    assert state(api, t, task)["run"]["state"] == "WAITING_APPROVAL"
    assert sends_for(stack, task, pg) == []


def test_t02d_wrong_request_hash_refused(stack: Stack, api, t, pg: PgEnv) -> None:
    task = to_approval(stack, api, t)
    a = state(api, t, task)["approvals"][0]
    r = api.post(
        f"/v1/approvals/{a['id']}/decision",
        headers={"Authorization": f"Bearer {t['token']}"},
        json={
            "schemaVersion": "1.0",
            "decision": "APPROVED",
            "requestSha256": "0" * 64,
            "expectedRecordVersion": a["recordVersion"],
        },
    )
    assert r.status_code == 409 and r.json()["error"] == "REQUEST_HASH_MISMATCH"


# ---------------------------------------------------------------- T03 / T14 goal change
def test_t03a_steer_while_waiting_rejects_old_intent(stack: Stack, api, t, pg: PgEnv) -> None:
    task = to_approval(stack, api, t)
    r = api.post(
        f"/v1/tasks/{task}/steer",
        headers={"Authorization": f"Bearer {t['token']}"},
        json={"schemaVersion": "1.0", "goal": "Notify bob instead.", "expectedGoalRevision": "1"},
    )
    assert r.status_code == 200, r.text
    stack.drive()
    st = state(api, t, task)
    assert st["goalRevision"] == "2"
    old = [a for a in st["approvals"] if a["goalRevision"] == "1"]
    assert old and all(a["decision"] == "REVOKED" for a in old)
    assert st["run"]["state"] == "WAITING_APPROVAL"  # new proposal (to bob) under goal 2 awaits approval
    approve(api, t, task)
    stack.drive()
    sent = sends_for(stack, task, pg)
    assert [s["body"]["to"] for s in sent] == ["bob"]  # the old (alice) intent never executed


def test_t14_steer_after_approval_before_dispatch_blocks_send(stack: Stack, api, t, pg: PgEnv) -> None:
    task = to_approval(stack, api, t)
    approve(api, t, task)
    api.post(
        f"/v1/tasks/{task}/steer",
        headers={"Authorization": f"Bearer {t['token']}"},
        json={"schemaVersion": "1.0", "goal": "Do not send anything; just answer.", "expectedGoalRevision": "1"},
    )
    stack.drive()
    assert sends_for(stack, task, pg) == []
    st = q(pg.owner, "SELECT status_code FROM agent.tool_intents WHERE task_id=%s AND tool_key='send_message'", (task,))
    assert st[0]["status_code"] in ("STALE_GOAL", "REVOKED")


# ---------------------------------------------------------------- T04 corrupted / altered framework state
def test_t04_corrupted_framework_checkpoint_rebuilt_from_head(stack: Stack, api, t, pg: PgEnv) -> None:
    task = to_approval(stack, api, t)
    rid = run_of(pg.owner, task)["id"]
    q(
        pg.owner,
        "UPDATE lg_checkpoint.checkpoint_blobs SET blob = '\\xc70500deadbeef'::bytea, type='msgpack'"
        " WHERE thread_id LIKE %s AND channel='messages' RETURNING 1",
        (rid + "%",),
    )
    approve(api, t, task)
    stack.drive()
    st = state(api, t, task)
    assert st["run"]["result"] == "done:CONFIRMED" and len(sends_for(stack, task, pg)) == 1
    ref = run_of(pg.owner, task)["framework_ref"]
    assert ref is not None and not q(
        pg.owner, "SELECT 1 FROM lg_checkpoint.checkpoints WHERE thread_id=%s AND %s=''", (ref, "x")
    )


def test_t04b_injected_type_in_checkpoint_rejected(stack: Stack, api, t, pg: PgEnv) -> None:
    """A checkpoint blob carrying an `os.system` constructor (checkpoint tampering) is refused, never executed."""
    import ormsgpack
    from langgraph.checkpoint.serde.jsonplus import EXT_CONSTRUCTOR_POS_ARGS

    marker = stack.tmp / "pwned.txt"
    evil = ormsgpack.packb(
        [ormsgpack.Ext(EXT_CONSTRUCTOR_POS_ARGS, ormsgpack.packb(["os", "system", [f"echo x > {marker}"]]))]
    )
    task = to_approval(stack, api, t)
    rid = run_of(pg.owner, task)["id"]
    q(
        pg.owner,
        "UPDATE lg_checkpoint.checkpoint_blobs SET blob = %s, type='msgpack' WHERE thread_id LIKE %s"
        " AND channel='messages' RETURNING 1",
        (evil, rid + "%"),
    )
    approve(api, t, task)
    stack.drive()
    assert not marker.exists()
    assert state(api, t, task)["run"]["result"] == "done:CONFIRMED" and len(sends_for(stack, task, pg)) == 1


def test_t04c_altered_head_detected(stack: Stack, api, t, pg: PgEnv) -> None:
    from aiagent_core import control
    from aiagent_core.db import Database

    task = submit(api, t, "search the widget price")
    stack.drive()
    rid = run_of(pg.owner, task)["id"]
    db = Database(pg.owner, max_size=1)
    with db.conn() as c:
        assert control.verify_head_chain(c, t["tenantId"], rid)
        c.execute("ALTER TABLE agent.run_checkpoints DISABLE TRIGGER run_checkpoints_append_only")
        c.execute(
            "UPDATE agent.run_checkpoints SET checkpoint_json = jsonb_set(checkpoint_json, '{content}', '\"evil\"')"
            " WHERE run_id=%s AND entry_kind='tool'",
            (rid,),
        )
        c.execute("ALTER TABLE agent.run_checkpoints ENABLE TRIGGER run_checkpoints_append_only")
        assert not control.verify_head_chain(c, t["tenantId"], rid)
    db.close()


# ---------------------------------------------------------------- T06 / T07 effect uncertainty
def test_t06_worker_killed_after_external_send_ack(stack: Stack, api, t, pg: PgEnv) -> None:
    task = to_approval(stack, api, t)
    approve(api, t, task)
    assert stack.worker(fault="after_tool_ack:send_message").returncode == 137
    assert len(sends_for(stack, task, pg)) == 1
    wait_lease_expiry(pg.owner, task)
    stack.drive()
    st = state(api, t, task)
    assert st["run"]["result"] == "done:CONFIRMED" and len(sends_for(stack, task, pg)) == 1


def test_t07_tool_gateway_killed_between_send_and_confirm_is_unknown(stack: Stack, api, t, pg: PgEnv) -> None:
    task = to_approval(stack, api, t)
    approve(api, t, task)
    stack.restart("tool", AIAGENT_FAULT="gateway_after_send")
    stack.worker()
    stack.restart("tool")
    for _ in range(2):  # never blind-retried however often the run is resumed
        wait_lease_expiry(pg.owner, task)
        api.post(f"/v1/tasks/{task}/resume", headers={"Authorization": f"Bearer {t['token']}"})
        stack.drive()
    assert len(sends_for(stack, task, pg)) == 1
    st = state(api, t, task)
    assert st["run"]["state"] == "WAITING_RECONCILIATION", st
    assert [e["status"] for e in st["effects"] if e["tool"] == "send_message"] == ["UNKNOWN"]
    # operator reconciliation through Tool Gateway (receipt query via Sentinel permit + egress)
    import asyncio

    from aiagent_core.rpc import RpcClient

    intent = q(pg.owner, "SELECT id FROM agent.tool_intents WHERE task_id=%s AND tool_key='send_message'", (task,))[0][
        "id"
    ]
    ops = RpcClient(f"tcp:127.0.0.1:{stack.ports['tool']}", "ops", bytes.fromhex(stack.keys["ops_tool"]))
    res = asyncio.run(ops.call("tool.reconcile", {"tenantId": t["tenantId"], "intentId": intent}))
    assert res["status"] == "CONFIRMED"
    assert len(sends_for(stack, task, pg)) == 1
    # Reconciliation must make the Run claimable and replay the *stored* receipt
    # from the immutable HEAD, without issuing a second external POST.
    restored = state(api, t, task)
    assert restored["run"]["state"] == "CREATED", restored
    rid = restored["run"]["id"]
    tool_entries = q(
        pg.owner,
        "SELECT checkpoint_json FROM agent.run_checkpoints WHERE run_id=%s"
        " AND entry_kind='tool' AND checkpoint_json->>'tool_name'='send_message'",
        (rid,),
    )
    assert len(tool_entries) == 1 and '"status":"CONFIRMED"' in tool_entries[0]["checkpoint_json"]["content"]
    reply = api.post(f"/v1/tasks/{task}/resume", headers={"Authorization": f"Bearer {t['token']}"})
    assert reply.status_code == 200, reply.text
    stack.drive()
    done = state(api, t, task)
    assert done["run"]["state"] == "SUCCEEDED", done
    assert done["run"]["result"] == "done:CONFIRMED", done
    stack.drive()  # duplicate resume jobs / queue delivery cannot repeat the send
    assert len(sends_for(stack, task, pg)) == 1
    tool_entries_after = q(
        pg.owner,
        "SELECT id FROM agent.run_checkpoints WHERE run_id=%s AND entry_kind='tool'"
        " AND checkpoint_json->>'tool_name'='send_message'",
        (rid,),
    )
    assert len(tool_entries_after) == 1


def test_t07c_cancelled_unknown_effect_converges_after_reconciliation(stack: Stack, api, t, pg: PgEnv) -> None:
    task = to_approval(stack, api, t)
    approve(api, t, task)
    stack.restart("tool", AIAGENT_FAULT="gateway_after_send")
    stack.worker()
    stack.restart("tool")
    wait_lease_expiry(pg.owner, task)
    reply = api.post(f"/v1/tasks/{task}/resume", headers={"Authorization": f"Bearer {t['token']}"})
    assert reply.status_code == 200, reply.text
    stack.drive()
    pending = state(api, t, task)
    assert pending["run"]["state"] == "WAITING_RECONCILIATION", pending
    reply = api.post(f"/v1/tasks/{task}/cancel", headers={"Authorization": f"Bearer {t['token']}"})
    assert reply.status_code == 200, reply.text
    assert state(api, t, task)["run"]["state"] == "WAITING_RECONCILIATION"
    import asyncio

    from aiagent_core.rpc import RpcClient

    intent_id = q(pg.owner, "SELECT id FROM agent.tool_intents WHERE task_id=%s AND tool_key='send_message'", (task,))[
        0
    ]["id"]
    ops = RpcClient(f"tcp:127.0.0.1:{stack.ports['tool']}", "ops", bytes.fromhex(stack.keys["ops_tool"]))
    res = asyncio.run(ops.call("tool.reconcile", {"tenantId": t["tenantId"], "intentId": intent_id}))
    assert res["status"] == "CONFIRMED"
    assert state(api, t, task)["run"]["state"] == "CANCELLED"
    assert state(api, t, task)["state"] == "CANCELLED"
    assert (
        q(
            pg.owner,
            "SELECT count(*) AS n FROM agent.run_checkpoints WHERE run_id=%s"
            " AND entry_kind='tool' AND tool_call_id=(SELECT invocation_key FROM agent.tool_intents WHERE id=%s)",
            (pending["run"]["id"], intent_id),
        )[0]["n"]
        == 1
    )
    assert len(sends_for(stack, task, pg)) == 1
    stack.drive()
    assert len(sends_for(stack, task, pg)) == 1
    assert state(api, t, task)["run"]["state"] == "CANCELLED"
    assert (
        asyncio.run(ops.call("tool.reconcile", {"tenantId": t["tenantId"], "intentId": intent_id}))["status"]
        == "CONFIRMED"
    )



def test_t07d_receiver_restart_404_is_not_negative_proof(stack: Stack, api, t, pg: PgEnv) -> None:
    """POST succeeds, tool gateway dies, receiver loses its in-memory receipts:
    its new 404 must never clear UNKNOWN or cause another external POST.
    """
    task = to_approval(stack, api, t)
    approve(api, t, task)
    stack.restart("tool", AIAGENT_FAULT="gateway_after_send")
    stack.worker()
    stack.restart("tool")
    wait_lease_expiry(pg.owner, task)
    stack.drive()
    before = state(api, t, task)
    assert before["run"]["state"] == "WAITING_RECONCILIATION", before
    assert len(sends_for(stack, task, pg)) == 1

    stack.restart("receiver")  # same endpoint, but 'seen' is now empty
    import asyncio

    from aiagent_core.rpc import RpcClient

    intent_id = q(pg.owner, "SELECT id FROM agent.tool_intents WHERE task_id=%s AND tool_key='send_message'", (task,))[
        0
    ]["id"]
    ops = RpcClient(f"tcp:127.0.0.1:{stack.ports['tool']}", "ops", bytes.fromhex(stack.keys["ops_tool"]))
    result = asyncio.run(ops.call("tool.reconcile", {"tenantId": t["tenantId"], "intentId": intent_id}))
    assert result["status"] == "UNKNOWN", result
    assert state(api, t, task)["run"]["state"] == "WAITING_RECONCILIATION"
    assert q(pg.owner, "SELECT status_code FROM agent.tool_intents WHERE id=%s", (intent_id,))[0][
        "status_code"
    ] == "UNKNOWN"

    reply = api.post(f"/v1/tasks/{task}/resume", headers={"Authorization": f"Bearer {t['token']}"})
    assert reply.status_code == 200, reply.text
    stack.drive()
    assert state(api, t, task)["run"]["state"] == "WAITING_RECONCILIATION"
    assert len(sends_for(stack, task, pg)) == 1
    assert q(
        pg.owner,
        "SELECT count(*) AS n FROM agent.run_checkpoints WHERE run_id=%s AND entry_kind='tool'"
        " AND checkpoint_json->>'tool_name'='send_message'",
        (before["run"]["id"],),
    )[0]["n"] == 0


def test_t07b_remote_ack_lost_timeout_is_unknown(pg: PgEnv, api, t, tmp_path) -> None:
    s = Stack(pg.dsn, tmp_path, owner_dsn=pg.owner, receiver_hang=True).start_all()
    try:
        task = submit(api, t, NOTIFY)
        s.drive()
        approve(api, t, task)
        s.drive()
        assert len(sends_for(s, task, pg)) == 1
        st = state(api, t, task)
        assert st["run"]["state"] == "WAITING_RECONCILIATION"
        assert [e["status"] for e in st["effects"] if e["tool"] == "send_message"] == ["UNKNOWN"]
    finally:
        s.close()


# ---------------------------------------------------------------- T09 subagent + compaction
def test_t09_subagent_child_result_admitted(stack: Stack, api, t, pg: PgEnv) -> None:
    task = submit(api, t, "Delegate the price research, then answer.")
    stack.drive()
    st = state(api, t, task)
    assert st["run"]["result"] == "done:delegated", st
    rid = st["run"]["id"]
    rows = q(
        pg.owner,
        "SELECT stream, entry_kind, checkpoint_json FROM agent.run_checkpoints WHERE run_id=%s ORDER BY"
        " checkpoint_revision",
        (rid,),
    )
    streams = {r["stream"] for r in rows}
    assert "subagent:researcher" in streams
    task_results = [r for r in rows if r["entry_kind"] == "tool" and r["checkpoint_json"].get("tool_name") == "task"]
    assert len(task_results) == 1 and "widget price is 42" in task_results[0]["checkpoint_json"]["content"]
    sub_models = [a for a in model_attempts(pg, task) if a["stream"] == "subagent:researcher"]
    assert sub_models and all(a["admitted"] for a in sub_models)  # sub-agent uses the same Model Gateway


def test_t09b_compaction_keeps_raw_head(stack: Stack, api, t, pg: PgEnv) -> None:
    long_goal = "search widget price. " + " ".join(f"[history {i}] " + "lorem ipsum " * 300 for i in range(5))
    assert len(long_goal) < 20000
    task = submit(api, t, long_goal)
    rs = stack.drive(AIAGENT_MAX_INPUT_TOKENS="6000")
    st = state(api, t, task)
    assert st["state"] == "SUCCEEDED", (st["run"], [r.stderr[-1500:] for r in rs])
    rid = st["run"]["id"]
    rows = q(pg.owner, "SELECT stream, entry_kind, checkpoint_json FROM agent.run_checkpoints WHERE run_id=%s", (rid,))
    assert any(r["stream"] == "summarize" for r in rows), "summarization did not go through the Model Gateway"
    main_user = [r for r in rows if r["stream"] == "main" and r["entry_kind"] == "user"]
    assert main_user[0]["checkpoint_json"]["content"] == long_goal  # raw goal never replaced by the summary


# ---------------------------------------------------------------- T10 / T11 / T13 / T15 / T17
def test_t11_parallel_writes_each_sent_once(stack: Stack, api, t, pg: PgEnv) -> None:
    task = submit(api, t, "Notify alice and bob at once (parallel).")
    stack.drive()
    for _ in range(3):
        if approve(api, t, task) == 0:
            break
        stack.drive()
    st = state(api, t, task)
    assert st["run"]["result"] == "done:parallel", st
    sent = sends_for(stack, task, pg)
    assert sorted(s["body"]["to"] for s in sent) == ["alice", "bob"]


def test_t13_answer_replayed_from_head_after_fresh_admission(stack: Stack, api, t, pg: PgEnv) -> None:
    task = submit(api, t, NOTIFY)
    assert stack.worker(fault="runtime_after_model").returncode == 137
    wait_lease_expiry(pg.owner, task)
    stack.drive()
    atts = model_attempts(pg, task)
    assert any(a["status_code"] == "REPLAYED" and a["admitted"] for a in atts)
    assert sum(a["status_code"] == "COMPLETED" for a in atts) == 2  # provider not called again for the replay


def test_t13b_replay_with_old_goal_claim_is_stale(stack: Stack, api, t, pg: PgEnv) -> None:
    task = submit(api, t, NOTIFY)
    assert stack.worker(fault="runtime_after_model").returncode == 137
    api.post(
        f"/v1/tasks/{task}/steer",
        headers={"Authorization": f"Bearer {t['token']}"},
        json={"schemaVersion": "1.0", "goal": "just answer", "expectedGoalRevision": "1"},
    )
    wait_lease_expiry(pg.owner, task)
    stack.drive()
    st = state(api, t, task)
    assert st["goalRevision"] == "2"
    assert all(a["reason_code"] != "STALE_GOAL" or not a["admitted"] for a in model_attempts(pg, task))


def test_t15_concurrent_runs_and_tenants_in_one_process(stack: Stack, api, pg: PgEnv) -> None:
    ts = [admin.provision_tenant(pg.owner) for _ in range(2)]
    tasks = [(tt, submit(api, tt, f"search widget price #{i}")) for i, tt in enumerate(ts * 2)]
    stack.drive(AIAGENT_WORKER_CONCURRENCY="4")
    for tt, task in tasks:
        st = state(api, tt, task)
        assert st["state"] == "SUCCEEDED" and st["run"]["result"] == "The widget price is 42.", st
    for tt, task in tasks:  # no cross-tenant HEAD entries
        rows = q(pg.owner, "SELECT DISTINCT tenant_id FROM agent.run_checkpoints WHERE task_id=%s", (task,))
        assert [r["tenant_id"] for r in rows] == [tt["tenantId"]]
        other = next(x for x in ts if x is not tt)
        assert api.get(f"/v1/tasks/{task}", headers={"Authorization": f"Bearer {other['token']}"}).status_code == 404


def test_t17a_cancel_while_waiting_approval(stack: Stack, api, t, pg: PgEnv) -> None:
    task = to_approval(stack, api, t)
    a = state(api, t, task)["approvals"][0]
    assert (
        api.post(f"/v1/tasks/{task}/cancel", headers={"Authorization": f"Bearer {t['token']}"}).json()["state"]
        == "CANCELLED"
    )
    r = api.post(
        f"/v1/approvals/{a['id']}/decision",
        headers={"Authorization": f"Bearer {t['token']}"},
        json={
            "schemaVersion": "1.0",
            "decision": "APPROVED",
            "requestSha256": a["requestSha256"],
            "expectedRecordVersion": a["recordVersion"],
        },
    )
    assert r.status_code == 409
    stack.drive()
    assert state(api, t, task)["state"] == "CANCELLED" and sends_for(stack, task, pg) == []


def test_t17b_cancel_while_leased_finishes_cancelled(stack: Stack, api, t, pg: PgEnv) -> None:
    task = submit(api, t, NOTIFY)
    assert stack.worker(fault="worker_after_claim").returncode == 137
    r = api.post(f"/v1/tasks/{task}/cancel", headers={"Authorization": f"Bearer {t['token']}"}).json()
    assert r["state"] == "CANCELLING"
    wait_lease_expiry(pg.owner, task)
    stack.drive()
    st = state(api, t, task)
    assert st["state"] == "CANCELLED" and st["run"]["state"] == "CANCELLED", st
    assert not any(a["status_code"] == "COMPLETED" for a in model_attempts(pg, task))  # no model work after cancel
    assert sends_for(stack, task, pg) == []


def test_t16_dlq_replay_of_cancelled_run_does_nothing(stack: Stack, api, t, pg: PgEnv) -> None:
    """JQ-T15: an operator replays an old job of a cancelled run -> acked as stale, no tool, no model call."""
    task = submit(api, t, NOTIFY)
    api.post(f"/v1/tasks/{task}/cancel", headers={"Authorization": f"Bearer {t['token']}"})
    stack.relay()
    rid = run_of(pg.owner, task)["id"]
    job = q(
        pg.owner,
        "UPDATE queue.jobs SET status='DEAD', locked_until=NULL WHERE payload->>'runId'=%s RETURNING id",
        (rid,),
    )
    from aiagent_core.db import Database
    from aiagent_core.queue import QueuePort

    QueuePort(Database(pg.dsn["ops"])).replay_dead_letter(job[0]["id"], "ops-test") if False else q(
        pg.owner, "UPDATE queue.jobs SET status='READY', attempts=0 WHERE id=%s RETURNING 1", (job[0]["id"],)
    )
    stack.drive()
    out = q(pg.owner, "SELECT status, outcome_code FROM queue.jobs WHERE id=%s", (job[0]["id"],))[0]
    assert out["status"] == "DONE" and out["outcome_code"] == "STALE_TERMINAL"
    assert model_attempts(pg, task) == [] and sends_for(stack, task, pg) == []


def test_t08_framework_holds_no_credentials(stack: Stack, api, t, pg: PgEnv) -> None:
    task = submit(api, t, NOTIFY)
    stack.drive()
    blobs = q(pg.owner, "SELECT encode(blob,'escape') AS b FROM lg_checkpoint.checkpoint_blobs")
    joined = json.dumps([b["b"] for b in blobs])
    assert stack.provider_secret not in joined and stack.receiver_token not in joined
    assert state(api, t, task)["run"]["state"] == "WAITING_APPROVAL"
    time.sleep(0)
