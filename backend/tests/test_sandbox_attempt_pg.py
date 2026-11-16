"""Real PostgreSQL/Gateway and fake Cell: Tool Attempt only, not execution attestation."""

from __future__ import annotations

import asyncio
import os

import aiagent_core.rpc
import pytest
from aiagent_core import commands, control
from aiagent_core.commands import Scope
from aiagent_gateways.executors import SandboxCellExecutor
from aiagent_gateways.tool_gateway import ToolGateway
from aiagent_sandbox.grant import verify

pytestmark = pytest.mark.pg


@pytest.mark.parametrize("mismatch", [False, True])
def test_real_db_gateway_binds_attempt_to_fake_cell(dbs, tenant, monkeypatch, mismatch):
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    ids = commands.submit_task(dbs["api"], scope, "fixture", profile="sandbox")
    claim = control.claim_run(dbs["runtime"], scope.tenant_id, ids["runId"], "fake-cell-worker")
    args = {"argv": ["test-command"], "cwdRelative": "."}
    with dbs["model_gateway"].tx() as c:
        control.append_head(
            c,
            claim,
            "assistant",
            {
                "kind": "assistant",
                "content": "",
                "tool_calls": [{"id": "fixture-call", "name": "sandbox_exec", "args": args}],
            },
            writer="test",
            request_key=os.urandom(32).hex(),
        )

    captured = []

    class FakeCell:
        def __init__(self, *args, **kwargs):
            pass

        async def call(self, method, body):
            grant = verify(b"g" * 32, body["grant"], ["test-command"], ".")
            captured.append(grant)
            return {
                "status": "EXITED",
                "attemptId": "different" if mismatch else grant.attempt_id,
                "exitCode": 0,
                "timedOut": False,
                "truncated": False,
                "stdout": "",
                "stderr": "",
            }

    monkeypatch.setattr(aiagent_core.rpc, "RpcClient", FakeCell)
    executor = SandboxCellExecutor("unix:/unused", b"r" * 32, b"g" * 32)
    gateway = ToolGateway(dbs["tool_gateway"], {"sandbox_cell": executor})
    result = asyncio.run(gateway.submit(claim, "fixture-call", "sandbox_exec", args))
    assert len(captured) == 1
    grant = captured[0]
    assert grant.attempt_binding_version == 1
    assert (
        grant.tenant_id == claim.tenant_id and grant.run_id == claim.run_id and grant.lease_epoch == claim.lease_epoch
    )
    row = dbs["owner"].one(
        "SELECT a.id,a.tool_intent_id,a.run_lease_epoch,a.status_code FROM agent.tool_attempts a "
        "JOIN agent.tool_intents i ON i.tenant_id=a.tenant_id AND i.id=a.tool_intent_id "
        "WHERE i.tenant_id=%s AND i.run_id=%s AND i.invocation_key=%s",
        (claim.tenant_id, claim.run_id, "fixture-call"),
    )
    assert row is not None and str(row["id"]) == grant.attempt_id and str(row["tool_intent_id"]) == grant.intent_id
    assert row["run_lease_epoch"] == claim.lease_epoch
    assert result["status"] == ("UNKNOWN" if mismatch else "CONFIRMED")
    assert row["status_code"] == ("UNKNOWN" if mismatch else "CONFIRMED")


@pytest.mark.parametrize("cancelled", [False, True])
def test_real_authority_denies_stale_or_cancelled_before_fake_cell(dbs, tenant, monkeypatch, cancelled):
    from dataclasses import replace

    from aiagent_domain.rpcwire import RpcError

    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    ids = commands.submit_task(dbs["api"], scope, "fixture", profile="sandbox")
    claim = control.claim_run(dbs["runtime"], scope.tenant_id, ids["runId"], "fake-cell-worker")
    args = {"argv": ["test-command"], "cwdRelative": "."}
    with dbs["model_gateway"].tx() as c:
        control.append_head(
            c,
            claim,
            "assistant",
            {
                "kind": "assistant",
                "content": "",
                "tool_calls": [{"id": "stale-call", "name": "sandbox_exec", "args": args}],
            },
            writer="test",
            request_key=os.urandom(32).hex(),
        )
    calls = []

    class FakeCell:
        def __init__(self, *args, **kwargs):
            calls.append("started")

    monkeypatch.setattr(aiagent_core.rpc, "RpcClient", FakeCell)
    gateway = ToolGateway(
        dbs["tool_gateway"], {"sandbox_cell": SandboxCellExecutor("unix:/unused", b"r" * 32, b"g" * 32)}
    )
    if cancelled:
        dbs["owner"].one(
            "UPDATE agent.runs SET cancel_requested=true WHERE tenant_id=%s AND id=%s RETURNING id",
            (claim.tenant_id, claim.run_id),
        )
        result = asyncio.run(gateway.submit(claim, "stale-call", "sandbox_exec", args))
        assert result["status"] == "CANCELLED"
    else:
        with pytest.raises(RpcError, match="FENCED"):
            asyncio.run(
                gateway.submit(replace(claim, lease_epoch=claim.lease_epoch + 1), "stale-call", "sandbox_exec", args)
            )
    assert calls == []


@pytest.mark.parametrize("case", ["tenant", "recipe"])
def test_real_authority_rejects_other_tenant_or_unproposed_argv(dbs, tenant, monkeypatch, case):
    from dataclasses import replace
    from uuid import uuid4

    from aiagent_domain.rpcwire import RpcError

    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    ids = commands.submit_task(dbs["api"], scope, "fixture", profile="sandbox")
    claim = control.claim_run(dbs["runtime"], scope.tenant_id, ids["runId"], "fake-cell-worker")
    args = {"argv": ["test-command"], "cwdRelative": "."}
    with dbs["model_gateway"].tx() as c:
        control.append_head(
            c,
            claim,
            "assistant",
            {
                "kind": "assistant",
                "content": "",
                "tool_calls": [{"id": "binding-call", "name": "sandbox_exec", "args": args}],
            },
            writer="test",
            request_key=os.urandom(32).hex(),
        )
    calls = []

    class FakeCell:
        def __init__(self, *args, **kwargs):
            calls.append("started")

    monkeypatch.setattr(aiagent_core.rpc, "RpcClient", FakeCell)
    gateway = ToolGateway(
        dbs["tool_gateway"], {"sandbox_cell": SandboxCellExecutor("unix:/unused", b"r" * 32, b"g" * 32)}
    )
    request_claim = replace(claim, tenant_id=str(uuid4())) if case == "tenant" else claim
    request_args = {"argv": ["altered-command"], "cwdRelative": "."} if case == "recipe" else args
    with pytest.raises(RpcError):
        asyncio.run(gateway.submit(request_claim, "binding-call", "sandbox_exec", request_args))
    assert calls == []
