"""STEP 3-D minimum E2E over the formal path: API -> Task/Run -> Outbox -> QueuePort -> Runtime Worker (process) ->
Model Gateway (process, RPC) -> Tool Intent -> Approval -> resume job -> Tool Gateway (process) final admission ->
Sentinel permit -> Trusted Egress -> receiver -> Effect Ledger -> HEAD -> SUCCEEDED -> API result."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiagent_api.app import create_app
from aiagent_core import admin, control
from aiagent_core.db import Database
from conftest import PgEnv
from stack import Stack

pytestmark = pytest.mark.pg


@pytest.fixture(scope="module")
def stack(pg: PgEnv, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    s = Stack(pg.dsn, tmp_path_factory.mktemp("stack")).start_all()
    yield s
    s.close()


@pytest.fixture()
def api(pg: PgEnv) -> Iterator[TestClient]:
    db = Database(pg.dsn["api"], max_size=4)
    with TestClient(create_app(db)) as c:
        yield c
    db.close()


def auth(t: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {t['token']}"}


def submit(api: TestClient, t: dict[str, str], goal: str, **kw: str) -> str:
    r = api.post("/v1/tasks", json={"schemaVersion": "1.0", "goal": goal, **kw}, headers=auth(t))
    assert r.status_code == 201, r.text
    return str(r.json()["taskId"])


def state(api: TestClient, t: dict[str, str], task_id: str) -> dict:
    r = api.get(f"/v1/tasks/{task_id}", headers=auth(t))
    assert r.status_code == 200, r.text
    return r.json()


def approve(api: TestClient, t: dict[str, str], task_id: str, decision: str = "APPROVED") -> None:
    for a in state(api, t, task_id)["approvals"]:
        if a["decision"] == "PENDING":
            r = api.post(f"/v1/approvals/{a['id']}/decision", headers=auth(t), json={
                "schemaVersion": "1.0", "decision": decision, "requestSha256": a["requestSha256"],
                "expectedRecordVersion": a["recordVersion"]})
            assert r.status_code == 200, r.text


def sends_for(stack: Stack, n0: int) -> list[dict]:
    return stack.sends()[n0:]


def test_e2e_approval_resume_tool_complete(stack: Stack, api: TestClient, pg: PgEnv) -> None:
    t = admin.provision_tenant(pg.owner)
    n0 = len(stack.sends())
    task = submit(api, t, "Find the widget price and notify alice.")
    r = stack.worker()
    assert r.returncode == 0, r.stderr[-3000:]
    st = state(api, t, task)
    assert st["state"] == "WAITING_INPUT" and st["run"]["state"] == "WAITING_APPROVAL", (st, r.stderr[-2000:])
    assert [a["tool"] for a in st["approvals"]] == ["send_message"]
    assert sends_for(stack, n0) == []  # nothing sent before approval
    approve(api, t, task)
    r = stack.worker()
    assert r.returncode == 0, r.stderr[-3000:]
    st = state(api, t, task)
    assert st["state"] == "SUCCEEDED" and st["run"]["result"] == "done:CONFIRMED", (st, r.stderr[-3000:])
    sent = sends_for(stack, n0)
    assert len(sent) == 1 and sent[0]["body"] == {"to": "alice", "body": "price=42"}
    assert sent[0]["authorized"] is True  # recipient-bound credential injected by the Tool Gateway only
    with Database(pg.owner).conn() as c:
        assert control.verify_head_chain(c, t["tenantId"], st["run"]["id"])
        eff = c.execute("SELECT outcome_code, external_receipt_ref FROM agent.side_effect_records e JOIN agent.tool_intents i"
                        " ON i.tenant_id=e.tenant_id AND i.id=e.tool_intent_id WHERE i.run_id=%s", (st["run"]["id"],)).fetchall()
        assert [e["outcome_code"] for e in eff] == ["CONFIRMED"] and eff[0]["external_receipt_ref"].startswith("rcpt_")
        rc = c.execute("SELECT decision, reason_code FROM agent.egress_receipts WHERE tenant_id=%s", (t["tenantId"],)).fetchall()
        assert [x["decision"] for x in rc] == ["ALLOWED"]
        # the provider credential never left the Model Gateway: not in HEAD, framework checkpoints, events, ledger
        for q in ("SELECT checkpoint_json::text AS x FROM agent.run_checkpoints",
                  "SELECT payload_json::text AS x FROM agent.domain_events",
                  "SELECT coalesce(result_json::text,'') AS x FROM agent.tool_intents",
                  "SELECT encode(blob, 'escape') AS x FROM lg_checkpoint.checkpoint_blobs",
                  "SELECT checkpoint::text AS x FROM lg_checkpoint.checkpoints"):
            assert not any(stack.provider_secret in (row["x"] or "") for row in c.execute(q).fetchall()), q
    assert stack.provider_secret not in r.stderr + stack.logs()
    assert stack.receiver_token not in r.stderr + stack.logs()
