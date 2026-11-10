"""Reconciliation is one atomic ledger/HEAD/run/task/outbox state transition.

No real executor sends: the fake can only query existing receipts. PostgreSQL
role grants and the production ToolGateway implementation are exercised.
"""

from __future__ import annotations

import httpx
import pytest
from aiagent_core import commands, control
from aiagent_core.commands import Scope
from aiagent_core.db import Database
from aiagent_domain import sha256_hex, uuid7
from aiagent_domain.rpcwire import RpcError
from aiagent_domain.tools import TOOLS
from aiagent_gateways.executors import HttpMessageExecutor
from aiagent_gateways.tool_gateway import ToolGateway

pytestmark = pytest.mark.pg
ARGS = {"to": "alice", "body": "hello"}


class ReceiptOnly:
    def __init__(self, sent: bool = True) -> None:
        self.sent = sent
        self.queries = 0

    def destination(self) -> None:
        return None

    async def execute(self, effect_key: str, args: dict, permit: str | None) -> dict:
        raise AssertionError("reconciliation must not execute an external write")

    async def query(self, effect_key: str, permit: str | None = None) -> dict:
        self.queries += 1
        return {"sent": self.sent, "receipt": f"receipt-{self.queries}"} if self.sent else {"sent": False}


def _scope(t: dict[str, str]) -> Scope:
    return Scope(t["tenantId"], t["userId"], t["workspaceId"])


def _run(dbs: dict[str, Database], run_id: str) -> dict:
    row = dbs["owner"].one("SELECT * FROM agent.runs WHERE id=%s", (run_id,))
    assert row is not None
    return row


def _task(dbs: dict[str, Database], task_id: str) -> dict:
    row = dbs["owner"].one("SELECT * FROM agent.tasks WHERE id=%s", (task_id,))
    assert row is not None
    return row


def _jobs(dbs: dict[str, Database], run_id: str) -> int:
    row = dbs["owner"].one(
        "SELECT count(*) AS n FROM agent.outbox_messages WHERE dedupe_key LIKE %s",
        (f"run:{run_id}:reconciled:%",),
    )
    return int(row["n"])


def _prepare(
    dbs: dict[str, Database], t: dict[str, str], count: int = 1, bad_hash: bool = False
) -> tuple[dict[str, str], list[str]]:
    ids = commands.submit_task(dbs["api"], _scope(t), "reconcile verified tool result")
    claim = control.claim_run(dbs["runtime"], t["tenantId"], ids["runId"], "old-worker")
    intents = []
    for i in range(count):
        call_id = f"call_reconcile_{uuid7()}"
        intent_id = uuid7()
        with dbs["model_gateway"].tx() as c:
            control.append_head(
                c,
                claim,
                "assistant",
                {
                    "kind": "assistant",
                    "content": "",
                    "tool_calls": [{"id": call_id, "name": "send_message", "args": ARGS}],
                },
                writer="model_gateway",
                stream="main",
                request_key=sha256_hex({"call": call_id}),
            )
        with dbs["tool_gateway"].tx() as c:
            c.execute(
                "INSERT INTO agent.tool_intents (tenant_id,id,task_id,run_id,tool_key,invocation_key,"
                " request_sha256,safe_arguments_json,risk_class,goal_revision,status_code,stream)"
                " VALUES (%s,%s,%s,%s,'send_message',%s,%s,%s::jsonb,'external_irreversible',1,'UNKNOWN','main')",
                (
                    t["tenantId"],
                    intent_id,
                    ids["taskId"],
                    ids["runId"],
                    call_id,
                    "a" * 64 if bad_hash else sha256_hex({"tool": "send_message", "args": ARGS}),
                    '{"to":"alice","body":"hello"}',
                ),
            )
            c.execute(
                "INSERT INTO agent.side_effect_records(tenant_id,id,tool_intent_id,effect_key,"
                " request_sha256,outcome_code,attempt_fence)"
                " VALUES (%s,%s,%s,%s,%s,'UNKNOWN',1)",
                (
                    t["tenantId"],
                    uuid7(),
                    intent_id,
                    f"{t['tenantId']}:{intent_id}",
                    "a" * 64 if bad_hash else sha256_hex({"tool": "send_message", "args": ARGS}),
                ),
            )
        intents.append(intent_id)
    assert control.finish_segment(dbs["runtime"], claim, "WAITING_RECONCILIATION") == "WAITING_RECONCILIATION"
    return ids, intents


def _gateway(dbs: dict[str, Database], receipt: ReceiptOnly) -> ToolGateway:
    return ToolGateway(dbs["tool_gateway"], {TOOLS["send_message"].executor: receipt})


@pytest.mark.asyncio
async def test_only_final_reconciled_intent_releases_run_and_enqueues_once(
    dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    ids, intents = _prepare(dbs, tenant, count=2)
    receipt = ReceiptOnly()
    gw = _gateway(dbs, receipt)
    first = await gw.reconcile(tenant["tenantId"], intents[0])
    assert first["status"] == "CONFIRMED"
    assert _run(dbs, ids["runId"])["status_code"] == "WAITING_RECONCILIATION"
    assert _jobs(dbs, ids["runId"]) == 0
    second = await gw.reconcile(tenant["tenantId"], intents[1])
    assert second["status"] == "CONFIRMED"
    assert _run(dbs, ids["runId"])["status_code"] == "CREATED"
    assert _task(dbs, ids["taskId"])["status_code"] == "RUNNING"
    assert _jobs(dbs, ids["runId"]) == 1
    assert (
        len(
            dbs["owner"].all(
                "SELECT 1 FROM agent.run_checkpoints WHERE run_id=%s AND entry_kind='tool'",
                (ids["runId"],),
            )
        )
        == 2
    )
    assert (await gw.reconcile(tenant["tenantId"], intents[0]))["status"] == "CONFIRMED"
    assert _jobs(dbs, ids["runId"]) == 1
    assert receipt.queries == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "paused,cancelled,expected",
    [
        (True, False, "PAUSED"),
        (False, True, "CANCELLED"),
    ],
)
async def test_pause_or_cancel_stays_nonrunnable_after_reconciliation(
    dbs: dict[str, Database], tenant: dict[str, str], paused: bool, cancelled: bool, expected: str
) -> None:
    ids, intents = _prepare(dbs, tenant)
    if paused:
        commands.pause(dbs["api"], _scope(tenant), ids["taskId"])
    if cancelled:
        commands.cancel(dbs["api"], _scope(tenant), ids["taskId"])
    receipt = ReceiptOnly()
    assert (await _gateway(dbs, receipt).reconcile(tenant["tenantId"], intents[0]))["status"] == "CONFIRMED"
    assert _run(dbs, ids["runId"])["status_code"] == expected
    assert _task(dbs, ids["taskId"])["status_code"] == expected
    assert _jobs(dbs, ids["runId"]) == 0
    assert receipt.queries == 1
    if paused:
        commands.resume(dbs["api"], _scope(tenant), ids["taskId"])
        claim = control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "resumed-worker")
        assert control.finish_segment(dbs["runtime"], claim, "SUCCEEDED") == "SUCCEEDED"
    else:
        with pytest.raises(commands.CommandError, match="NOT_RESUMABLE"):
            commands.resume(dbs["api"], _scope(tenant), ids["taskId"])


@pytest.mark.asyncio
async def test_untrusted_receipt_cannot_restore_mismatched_proposal(
    dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    ids, intents = _prepare(dbs, tenant, bad_hash=True)
    receipt = ReceiptOnly()
    with pytest.raises(RpcError, match="NOT_IN_HEAD"):
        await _gateway(dbs, receipt).reconcile(tenant["tenantId"], intents[0])
    assert _run(dbs, ids["runId"])["status_code"] == "WAITING_RECONCILIATION"
    assert _jobs(dbs, ids["runId"]) == 0
    assert not dbs["owner"].all(
        "SELECT id FROM agent.run_checkpoints WHERE run_id=%s AND entry_kind='tool'", (ids["runId"],)
    )
    assert (
        dbs["owner"].one("SELECT status_code FROM agent.tool_intents WHERE id=%s", (intents[0],))["status_code"]
        == "UNKNOWN"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,body,expected",
    [
        (404, "{}", None),
        (200, "{}", None),
        (200, '{"receipt":""}', None),
        (200, '{"receipt":"   "}', None),
        (200, '{"receipt":123}', None),
        (200, '{"receipt":"rcpt\\ninvalid"}', None),
        (200, "{not-json", None),
        (200, '{"receipt":"ok_receipt"}', "ok_receipt"),
    ],
)
async def test_http_receipt_query_never_converts_weak_evidence_into_confirmation(
    monkeypatch: pytest.MonkeyPatch, code: int, body: str, expected: str | None
) -> None:
    executor = HttpMessageExecutor("https://receiver.example/messages", proxy=None)
    requests = []

    def response(req: httpx.Request) -> httpx.Response:
        requests.append((req.method, str(req.url)))
        return httpx.Response(code, text=body)

    transport = httpx.MockTransport(response)
    monkeypatch.setattr(
        executor,
        "_client",
        lambda permit: httpx.AsyncClient(transport=transport, trust_env=False),
    )
    result = await executor.query("tenant:intent")
    assert requests == [("GET", "https://receiver.example/receipts/tenant:intent")]
    if expected is None:
        assert result is None
    else:
        assert result == {"sent": True, "receipt": expected}


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [{"sent": False}, {"sent": True}, {"sent": True, "receipt": ""}])
async def test_gateway_weak_negative_or_receipt_cannot_release_run(
    dbs: dict[str, Database], tenant: dict[str, str], answer: dict
) -> None:
    ids, intents = _prepare(dbs, tenant)

    class WeakEvidence(ReceiptOnly):
        async def query(self, effect_key: str, permit: str | None = None) -> dict:
            self.queries += 1
            return answer

    evidence = WeakEvidence()
    result = await _gateway(dbs, evidence).reconcile(tenant["tenantId"], intents[0])
    assert result["status"] == "UNKNOWN"
    assert _run(dbs, ids["runId"])["status_code"] == "WAITING_RECONCILIATION"
    assert (
        dbs["owner"].one("SELECT status_code FROM agent.tool_intents WHERE id=%s", (intents[0],))["status_code"]
        == "UNKNOWN"
    )
    assert _jobs(dbs, ids["runId"]) == 0
