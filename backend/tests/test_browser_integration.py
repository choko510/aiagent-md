"""PostgreSQL + ToolGateway + Playwright Chromium end-to-end browser integration tests."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace

import pytest
from aiagent_browser import Authority, BrowserError, BrowserTarget, FixtureResponse, PlaywrightBrowserWorker
from aiagent_browser.pg_fence import PostgresFencePort
from aiagent_core import admin, control
from aiagent_core.commands import Scope, decide_approval, submit_task
from aiagent_core.db import Database
from aiagent_domain import uuid7
from aiagent_gateways.browser_executor import BrowserFixtureExecutor
from aiagent_gateways.tool_gateway import ToolGateway
from conftest import PgEnv

pytestmark = pytest.mark.pg


def _run(coro):  # noqa: ANN001, ANN202
    return asyncio.run(coro)


def _propose(dbs: dict[str, Database], claim: control.Claim, call_id: str, name: str, args: dict) -> None:
    with dbs["model_gateway"].tx() as c:
        control.append_head(
            c,
            claim,
            "assistant",
            {"kind": "assistant", "content": "", "tool_calls": [{"id": call_id, "name": name, "args": args}]},
            writer="test",
            request_key=os.urandom(32).hex(),
        )


def test_browser_tool_gateway_pg_fence_approval_and_chromium(
    pg: PgEnv, dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    created = submit_task(dbs["api"], scope, "inspect offline fixture", profile="browser")
    claim = control.claim_run(dbs["runtime"], scope.tenant_id, created["runId"], "browser-worker-1")

    worker = PlaywrightBrowserWorker()
    executor = BrowserFixtureExecutor(
        dbs["tool_gateway"],
        fixtures={
            "fixture://alpha.test/checkout": FixtureResponse(
                200,
                "<details><summary data-testid='details'>Summary</summary><p>Order TOKEN:secret99</p></details>"
                "<label for='note'>Note</label><input id='note' type='text' value=''>"
                "<form action='#ok' method='get'><button data-testid='buy' type='submit'>Confirm</button></form>",
            ),
            "fixture://alpha.test/secret": FixtureResponse(
                200,
                "<h1>Vault</h1><input type='password' value='PASS-SECRET'>",
            ),
        },
        allowed_hosts=frozenset({"alpha.test"}),
        worker=worker,
    )
    gw = ToolGateway(dbs["tool_gateway"], {"browser_fixture": executor})

    try:
        secret_args = {"url": "fixture://alpha.test/secret"}
        _propose(dbs, claim, "call_sec", "browser_navigate", secret_args)
        sec_out = _run(gw.submit(claim, "call_sec", "browser_navigate", secret_args))
        assert sec_out["status"] == "DONE", sec_out
        assert "PASS-SECRET" not in str(sec_out)
        sid = sec_out["result"]["sessionId"]

        nav_args = {"url": "fixture://alpha.test/checkout", "sessionId": sid}
        _propose(dbs, claim, "call_nav", "browser_navigate", nav_args)
        nav_out = _run(gw.submit(claim, "call_nav", "browser_navigate", nav_args))
        assert nav_out["status"] == "DONE", nav_out
        assert "secret99" not in str(nav_out)

        row = dbs["owner"].one(
            "SELECT state, lease_epoch, mode FROM agent.browser_sessions WHERE tenant_id=%s AND id=%s",
            (scope.tenant_id, sid),
        )
        assert row is not None and row["state"] == "ACTIVE" and row["lease_epoch"] == claim.lease_epoch

        click_args = {"sessionId": sid, "kind": "test_id", "selector": "details"}
        _propose(dbs, claim, "call_clk", "browser_click", click_args)
        clk_out = _run(gw.submit(claim, "call_clk", "browser_click", click_args))
        assert clk_out["status"] == "CONFIRMED", clk_out
        assert clk_out["result"]["status"] == "EXECUTED"
        assert "Order" in clk_out["result"]["after"]

        sub_args = {
            "sessionId": sid,
            "kind": "test_id",
            "selector": "buy",
            "inputs": [{"kind": "label", "selector": "Note", "value": "leave at door"}],
        }
        _propose(dbs, claim, "call_sub", "browser_submit", sub_args)
        pending = _run(gw.submit(claim, "call_sub", "browser_submit", sub_args))
        assert pending["status"] == "PENDING_APPROVAL", pending
        approval_id = pending["approvalId"]

        apr = dbs["owner"].one(
            "SELECT tool_intent_id, request_sha256, record_version FROM agent.approval_records"
            " WHERE tenant_id=%s AND id=%s",
            (scope.tenant_id, approval_id),
        )
        assert apr is not None
        decide_approval(dbs["api"], scope, approval_id, "APPROVED", apr["request_sha256"], apr["record_version"])

        sub_out = _run(gw.submit(claim, "call_sub", "browser_submit", sub_args))
        assert sub_out["status"] == "CONFIRMED", sub_out
        assert sub_out["result"]["receipt"].startswith("browser:")

        ledger = dbs["owner"].one(
            "SELECT outcome_code, external_receipt_ref FROM agent.side_effect_records"
            " WHERE tenant_id=%s AND tool_intent_id=%s",
            (scope.tenant_id, apr["tool_intent_id"]),
        )
        assert ledger is not None and ledger["outcome_code"] == "CONFIRMED"
        assert ledger["external_receipt_ref"] == sub_out["result"]["receipt"]
    finally:
        executor.close()


def test_pg_fence_blocks_stale_epoch_and_cross_tenant(
    pg: PgEnv, dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    created = submit_task(dbs["api"], scope, "fence test", profile="browser")
    claim = control.claim_run(dbs["runtime"], scope.tenant_id, created["runId"], "w1")

    fence = PostgresFencePort(dbs["tool_gateway"])
    sid = uuid7()
    auth1 = Authority(
        claim.tenant_id,
        claim.owner_user_id,
        claim.task_id,
        claim.run_id,
        str(claim.goal_revision),
        str(claim.lease_epoch),
    )
    _run(fence.register(sid, auth1))
    _run(fence.check(sid, auth1))

    # Cross-tenant check denied
    other = admin.provision_tenant(pg.owner)
    with pytest.raises(BrowserError, match="STALE_LEASE"):
        _run(fence.check(sid, replace(auth1, tenant_id=other["tenantId"])))

    # Advance epoch -> old epoch rejected
    auth2 = replace(auth1, lease_epoch=str(claim.lease_epoch + 1))
    _run(fence.advance(sid, auth1, auth2, "AGENT"))
    with pytest.raises(BrowserError, match="STALE_LEASE"):
        _run(fence.check(sid, auth1))
    _run(fence.check(sid, auth2))
    _run(fence.revoke(sid, auth2))
    with pytest.raises(BrowserError, match="STALE_LEASE"):
        _run(fence.check(sid, auth2))


def test_browser_submit_timeout_records_unknown_ledger(
    pg: PgEnv, dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    class TimeoutWorker:
        async def start(self, session_id: str) -> None:
            pass

        async def render(self, session_id: str, html: str) -> None:
            pass

        async def observe(self, session_id: str) -> str:
            return "Checkout"

        async def click(self, session_id: str, target: BrowserTarget, *, timeout_ms: int = 5000) -> str:
            return "OK"

        async def submit(self, session_id: str, target: BrowserTarget, inputs=(), *, timeout_ms: int = 5000) -> str:
            raise BrowserError("ACTION_TIMEOUT")

        async def close(self, session_id: str) -> None:
            pass

    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    created = submit_task(dbs["api"], scope, "timeout unknown", profile="browser")
    claim = control.claim_run(dbs["runtime"], scope.tenant_id, created["runId"], "w-timeout")
    executor = BrowserFixtureExecutor(
        dbs["tool_gateway"],
        fixtures={"fixture://alpha.test/pay": FixtureResponse(200, "<button data-testid='pay'>Pay</button>")},
        allowed_hosts=frozenset({"alpha.test"}),
        worker=TimeoutWorker(),
    )
    gw = ToolGateway(dbs["tool_gateway"], {"browser_fixture": executor})
    try:
        nav_args = {"url": "fixture://alpha.test/pay"}
        _propose(dbs, claim, "call_nav2", "browser_navigate", nav_args)
        nav_out = _run(gw.submit(claim, "call_nav2", "browser_navigate", nav_args))
        sid = nav_out["result"]["sessionId"]

        sub_args = {"sessionId": sid, "kind": "test_id", "selector": "pay"}
        _propose(dbs, claim, "call_sub2", "browser_submit", sub_args)
        pending = _run(gw.submit(claim, "call_sub2", "browser_submit", sub_args))
        apr = dbs["owner"].one(
            "SELECT tool_intent_id, request_sha256, record_version FROM agent.approval_records"
            " WHERE tenant_id=%s AND id=%s",
            (scope.tenant_id, pending["approvalId"]),
        )
        assert apr is not None
        decide_approval(
            dbs["api"], scope, pending["approvalId"], "APPROVED", apr["request_sha256"], apr["record_version"]
        )

        out = _run(gw.submit(claim, "call_sub2", "browser_submit", sub_args))
        assert out["status"] == "UNKNOWN"
        ledger = dbs["owner"].one(
            "SELECT outcome_code FROM agent.side_effect_records WHERE tenant_id=%s AND tool_intent_id=%s",
            (scope.tenant_id, apr["tool_intent_id"]),
        )
        assert ledger is not None and ledger["outcome_code"] == "UNKNOWN"
    finally:
        executor.close()
