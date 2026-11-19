from __future__ import annotations

import asyncio

import aiagent_core.rpc
import pytest
from aiagent_development.contracts import InvalidEvidence
from aiagent_development.factory import _complete_sandbox_result
from aiagent_gateways.executors import ExecutorFailed, ExecutorUnknown, SandboxCellExecutor


@pytest.mark.parametrize(
    ("exit_code", "timeout", "truncated", "outcome"),
    [
        (0, False, False, "EXITED_ZERO"),
        (1, False, False, "NONZERO_EXIT"),
        (0, False, True, "OUTPUT_LIMIT"),
        (-9, True, False, "TIMED_OUT"),
    ],
)
def test_gateway_propagates_command_outcome(
    monkeypatch: pytest.MonkeyPatch, exit_code: int, timeout: bool, truncated: bool, outcome: str
) -> None:

    class Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def call(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {
                "status": "EXITED",
                "attemptId": "test-attempt",
                "exitCode": exit_code,
                "timedOut": timeout,
                "truncated": truncated,
                "stdout": "",
                "stderr": "",
            }

    monkeypatch.setattr(aiagent_core.rpc, "RpcClient", Client)
    executor = SandboxCellExecutor("unix:/unused", b"r" * 32, b"g" * 32)
    executor.context["key"] = {
        "tenant_id": "t",
        "task_id": "t",
        "run_id": "r",
        "intent_id": "i",
        "lease_epoch": 1,
        "attempt_id": "test-attempt",
    }
    result = asyncio.run(executor.execute("key", {"argv": ["tool"], "cwdRelative": "."}, None))
    assert result["outcome"] == outcome and result["truncated"] is truncated
    assert result["resourceLimit"] == "UNKNOWN"
    if timeout or truncated:
        with pytest.raises(InvalidEvidence):
            _complete_sandbox_result(result)
    else:
        assert _complete_sandbox_result(result) == result


def test_incomplete_or_inconsistent_result_is_not_evidence() -> None:
    for result in (
        {"exitCode": 0, "timedOut": False},
        {"exitCode": 0, "timedOut": False, "truncated": True, "outcome": "EXITED_ZERO"},
        {"exitCode": 1, "timedOut": False, "truncated": False, "outcome": "EXITED_ZERO"},
    ):
        with pytest.raises(InvalidEvidence):
            _complete_sandbox_result(result)


@pytest.mark.parametrize(
    ("response", "exception"),
    [
        ({"status": "REFUSED", "reason": "DENIED"}, ExecutorFailed),
        ({"status": "EXITED", "exitCode": 0}, ExecutorUnknown),
    ],
)
def test_refused_and_malformed_results_are_distinct(
    monkeypatch: pytest.MonkeyPatch, response: dict[str, object], exception: type[Exception]
) -> None:
    class Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def call(self, *args: object, **kwargs: object) -> dict[str, object]:
            return response

    monkeypatch.setattr(aiagent_core.rpc, "RpcClient", Client)
    executor = SandboxCellExecutor("unix:/unused", b"r" * 32, b"g" * 32)
    executor.context["key"] = {
        "tenant_id": "t",
        "task_id": "t",
        "run_id": "r",
        "intent_id": "i",
        "lease_epoch": 1,
        "attempt_id": "test-attempt",
    }
    with pytest.raises(exception):
        asyncio.run(executor.execute("key", {"argv": ["tool"], "cwdRelative": "."}, None))


@pytest.mark.parametrize("error_kind", ["NO_RESULT", "INTERNAL", "RESPONSE_AUTH"])
def test_rpc_errors_do_not_claim_execution_was_refused(monkeypatch: pytest.MonkeyPatch, error_kind: str) -> None:
    from aiagent_domain.rpcwire import RpcAuthError, RpcError

    class Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def call(self, *args: object, **kwargs: object) -> dict[str, object]:
            if error_kind == "RESPONSE_AUTH":
                raise RpcAuthError(error_kind)
            raise RpcError(error_kind)

    monkeypatch.setattr(aiagent_core.rpc, "RpcClient", Client)
    executor = SandboxCellExecutor("unix:/unused", b"r" * 32, b"g" * 32)
    executor.context["key"] = {
        "tenant_id": "t",
        "task_id": "t",
        "run_id": "r",
        "intent_id": "i",
        "lease_epoch": 1,
        "attempt_id": "test-attempt",
    }
    with pytest.raises(ExecutorUnknown):
        asyncio.run(executor.execute("key", {"argv": ["tool"], "cwdRelative": "."}, None))


def test_gateway_clipping_marks_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    class Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def call(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {
                "status": "EXITED",
                "attemptId": "test-attempt",
                "exitCode": 0,
                "timedOut": False,
                "truncated": False,
                "stdout": "x" * 8001,
                "stderr": "",
            }

    monkeypatch.setattr(aiagent_core.rpc, "RpcClient", Client)
    executor = SandboxCellExecutor("unix:/unused", b"r" * 32, b"g" * 32)
    executor.context["key"] = {
        "tenant_id": "t",
        "task_id": "t",
        "run_id": "r",
        "intent_id": "i",
        "lease_epoch": 1,
        "attempt_id": "test-attempt",
    }
    result = asyncio.run(executor.execute("key", {"argv": ["tool"], "cwdRelative": "."}, None))
    assert result["truncated"] and result["outcome"] == "OUTPUT_LIMIT"
    with pytest.raises(InvalidEvidence):
        _complete_sandbox_result(result)


@pytest.mark.parametrize("kind", ["build", "static-check", "test"])
def test_real_adapter_refuses_unattested_source_before_dispatch(kind: str) -> None:
    from unittest.mock import Mock

    from aiagent_development.contracts import ExecutionRequest, ProjectScope, RunFence
    from aiagent_development.factory import ToolGatewaySandboxAdapter

    db, gateway, claim = Mock(), Mock(), Mock()
    adapter = ToolGatewaySandboxAdapter(db, gateway, claim)
    scope = ProjectScope("t", "o", "p", "w", "org/repo", "branch", "a" * 40, "rev")
    request = ExecutionRequest(
        scope,
        RunFence("t", "r", 1, 1, 1),
        "plan",
        kind,
        ("tool",),
        "a" * 64,
        "intent",
        test_id="unit" if kind == "test" else None,
    )
    with pytest.raises(InvalidEvidence, match="verified workspace"):
        adapter.execute(request)
    db.tx.assert_not_called()
    gateway.submit.assert_not_called()


@pytest.mark.parametrize("altered", [None, "attempt", "test_id", "source_commit", "timed_out"])
def test_opt_in_adapter_requires_verified_matching_receipt(monkeypatch, altered):
    from contextlib import nullcontext
    from unittest.mock import AsyncMock, Mock

    from aiagent_core import control
    from aiagent_development.contracts import EffectUnknown, ExecutionRequest, ProjectScope, RunFence
    from aiagent_development.factory import ToolGatewaySandboxAdapter

    monkeypatch.setenv("AIAGENT_PROFILE", "test")
    claim = control.Claim("tenant", "owner", "task", "run", 2, 3, "worker", 4)
    scope = ProjectScope("tenant", "owner", "project", "workspace", "org/repo", "branch", "a" * 40, "1")
    run = RunFence("task", "run", 3, 2, 4, "worker")
    request = ExecutionRequest(
        scope, run, "plan", "test", ("/usr/bin/true",), "a" * 64, "intent", test_id="unit", target_commit_sha="b" * 40
    )
    db = Mock()
    db.tx.return_value = nullcontext(object())
    monkeypatch.setattr(control, "check_current", lambda *_args: {"workspace_id": "workspace"})
    monkeypatch.setattr(
        control,
        "find_proposal",
        lambda *_args: ({"name": "sandbox_exec", "args": {"argv": ["/usr/bin/true"], "cwdRelative": "."}}, "main"),
    )
    payload = {
        "tenant": "tenant",
        "owner": "owner",
        "task": "task",
        "run": "run",
        "goal": 3,
        "lease": 2,
        "workspace": "workspace",
        "workspace_revision": 1,
        "execution": "execution",
        "attempt": "attempt",
        "kind": "test",
        "test_id": "unit",
        "toolchain_sha256": "a" * 64,
        "exit_code": 0,
        "timed_out": False,
        "truncated": False,
        "source_commit": "b" * 40,
    }
    if altered == "attempt":
        payload["attempt"] = "other"
    elif altered == "test_id":
        payload["test_id"] = "other"
    elif altered == "source_commit":
        payload["source_commit"] = None
    elif altered == "timed_out":
        payload["timed_out"] = None
    gateway = Mock()
    gateway.submit = AsyncMock(
        return_value={
            "status": "CONFIRMED",
            "result": {
                "executionId": "execution",
                "receiptId": "receipt",
                "toolAttemptId": "attempt",
                "bodySha256": "d" * 64,
            },
        }
    )

    class Reader:
        def preflight(self, *_args):
            pass

        def __call__(self, *_args):
            return payload, "d" * 64

    adapter = ToolGatewaySandboxAdapter(db, gateway, claim, receipt_reader=Reader())
    if altered is None:
        evidence = adapter.execute(request)
        assert evidence.source_commit_sha == "b" * 40 and evidence.test_id == "unit"
        assert evidence.timed_out is False and evidence.truncated is False
    else:
        with pytest.raises(EffectUnknown, match="receipt mismatch"):
            adapter.execute(request)


def test_legacy_persisted_test_without_completion_flags_cannot_pass() -> None:
    from aiagent_development.contracts import (
        CommitEvidence,
        Cycle,
        ExecutionEvidence,
        Phase,
        ProjectScope,
        ReviewVerdict,
        RunFence,
        TaskPlan,
    )
    from aiagent_development.pg_store import _deserialize_cycle, _serialize_cycle
    from aiagent_development.workflow import DevelopmentWorkflow

    scope = ProjectScope("tenant", "owner", "project", "workspace", "org/repo", "branch", "a" * 40, "1")
    run = RunFence("task", "run", 1, 1, 1)
    commit = CommitEvidence("a" * 40, "b" * 40, "c" * 64, "patch", "git-receipt")
    evidence = ExecutionEvidence(
        "test-intent",
        run,
        "test",
        0,
        True,
        "d" * 64,
        "e" * 64,
        "",
        "",
        "b" * 40,
        "unit",
        "test-receipt",
        timed_out=False,
        truncated=False,
    )
    reviews = tuple(
        ReviewVerdict(f"review-{role}", role, "b" * 40, True, f"receipt-{role}")
        for role in ("functional", "security", "ui-e2e")
    )
    cycle = Cycle(
        "cycle",
        scope,
        run,
        Phase.CODE_READY,
        "change",
        plan=TaskPlan("plan", "change", ("unit",), 1),
        commit=commit,
        tests=(evidence,),
        reviews=reviews,
        execution_receipts=(evidence,),
    )
    assert DevelopmentWorkflow._gate(cycle, cycle.reviews)
    serialized = _serialize_cycle(cycle)
    for item in (*serialized["tests"], *serialized["execution_receipts"]):
        item.pop("timed_out")
        item.pop("truncated")
    restored = _deserialize_cycle(serialized)
    assert restored.tests[0].timed_out is None and restored.tests[0].truncated is None
    assert not DevelopmentWorkflow._gate(restored, restored.reviews)
    serialized["tests"][0]["exit_code"] = False
    with pytest.raises(InvalidEvidence, match="invalid stored execution status"):
        _deserialize_cycle(serialized)


def test_multibyte_and_large_results_do_not_gain_false_completeness() -> None:
    text = "日本語" * 2000
    result = {"exitCode": 0, "timedOut": False, "truncated": True, "outcome": "OUTPUT_LIMIT", "stdout": text}
    with pytest.raises(InvalidEvidence):
        _complete_sandbox_result(result)
    result.update(truncated=False, outcome="EXITED_ZERO")
    assert _complete_sandbox_result(result)["stdout"] == text


@pytest.mark.parametrize("different_attempt", [False, True])
def test_gateway_binds_signed_tool_attempt_to_fake_cell_response(
    monkeypatch: pytest.MonkeyPatch, different_attempt: bool
) -> None:
    from aiagent_sandbox.grant import verify

    signing_key = b"g" * 32
    attempt = "authority-issued-attempt"

    class FakeCellClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def call(self, method: str, body: dict[str, object]) -> dict[str, object]:
            assert method == "exec.run"
            signed = body["grant"]
            grant = verify(signing_key, signed, ["tool"], ".")
            assert grant.attempt_id == attempt
            assert (grant.tenant_id, grant.run_id, grant.lease_epoch) == ("t", "r", 1)
            return {
                "status": "EXITED",
                "attemptId": "other" if different_attempt else attempt,
                "exitCode": 0,
                "timedOut": False,
                "truncated": False,
                "stdout": "",
                "stderr": "",
            }

    monkeypatch.setattr(aiagent_core.rpc, "RpcClient", FakeCellClient)
    executor = SandboxCellExecutor("unix:/unused", b"r" * 32, signing_key)
    executor.context["key"] = {
        "tenant_id": "t",
        "task_id": "t",
        "run_id": "r",
        "intent_id": "i",
        "lease_epoch": 1,
        "attempt_id": attempt,
    }
    if different_attempt:
        with pytest.raises(ExecutorUnknown):
            asyncio.run(executor.execute("key", {"argv": ["tool"], "cwdRelative": "."}, None))
    else:
        result = asyncio.run(executor.execute("key", {"argv": ["tool"], "cwdRelative": "."}, None))
        assert result["outcome"] == "EXITED_ZERO"
        assert result["toolAttemptId"] == attempt


@pytest.mark.parametrize(("field", "value"), [("attempt_id", "other"), ("tenant_id", "other"), ("lease_epoch", 2)])
def test_attempt_grant_tampering_is_rejected(field: str, value: object) -> None:
    from aiagent_sandbox.grant import Grant, GrantError, sign, verify

    key = b"g" * 32
    grant = Grant(
        "g",
        "tenant",
        "task",
        "run",
        "intent",
        1,
        ("tool",),
        ".",
        1000,
        1024,
        9999999999,
        attempt_id="authority-attempt",
    )
    token = sign(key, grant)
    token["grant"][field] = value
    with pytest.raises(GrantError, match="BAD_SIGNATURE"):
        verify(key, token, ["tool"], ".")


def test_attempt_binding_versions_fail_closed() -> None:
    from aiagent_sandbox.grant import Grant, GrantError, sign, verify

    key = b"g" * 32
    values = ("g", "tenant", "task", "run", "intent", 1, ("tool",), ".", 1000, 1024, 9999999999)
    legacy = Grant(*values)
    assert "attempt_binding_version" not in legacy.payload()
    with pytest.raises(GrantError, match="LEGACY_DISABLED"):
        verify(key, sign(key, legacy), ["tool"], ".")
    assert verify(key, sign(key, legacy), ["tool"], ".", allow_legacy=True).attempt_binding_version is None
    with pytest.raises(GrantError, match="MISSING_ATTEMPT"):
        verify(key, sign(key, Grant(*values, attempt_binding_version=1)), ["tool"], ".")
    for invalid_version in (2, True):
        with pytest.raises(GrantError, match="UNSUPPORTED_ATTEMPT_BINDING"):
            verify(
                key, sign(key, Grant(*values, attempt_id="a", attempt_binding_version=invalid_version)), ["tool"], "."
            )


@pytest.mark.parametrize("attempt", [None, "", "  ", 42])
def test_gateway_rejects_missing_attempt_before_cell(monkeypatch: pytest.MonkeyPatch, attempt: object) -> None:
    from unittest.mock import Mock

    client = Mock()
    monkeypatch.setattr(aiagent_core.rpc, "RpcClient", client)
    executor = SandboxCellExecutor("unix:/unused", b"r" * 32, b"g" * 32)
    executor.context["key"] = {
        "tenant_id": "t",
        "task_id": "t",
        "run_id": "r",
        "intent_id": "i",
        "lease_epoch": 1,
        "attempt_id": attempt,
    }
    with pytest.raises(ExecutorFailed, match="tool attempt"):
        asyncio.run(executor.execute("key", {"argv": ["tool"], "cwdRelative": "."}, None))
    client.assert_not_called()


def test_execution_binding_is_signed_and_requires_complete_versioned_fields() -> None:
    from aiagent_domain import uuid7
    from aiagent_sandbox.grant import Grant, GrantError, sign, verify

    key = b"g" * 32
    values = ("g", "tenant", "task", "run", "intent", 1, ("tool",), ".", 1000, 1024, 9999999999)
    execution_id = uuid7()
    grant = Grant(
        *values,
        attempt_id="attempt",
        attempt_binding_version=1,
        execution_binding_version=1,
        execution_id=execution_id,
        execution_binding_sha256="a" * 64,
    )
    token = sign(key, grant)
    verified = verify(key, token, ["tool"], ".")
    assert verified.execution_id == execution_id and verified.execution_binding_sha256 == "a" * 64
    for field, value in (("execution_id", uuid7()), ("execution_binding_sha256", "b" * 64)):
        changed = {"grant": dict(token["grant"]), "mac": token["mac"]}
        changed["grant"][field] = value
        with pytest.raises(GrantError, match="BAD_SIGNATURE"):
            verify(key, changed, ["tool"], ".")
    invalid = Grant(*values, attempt_id="attempt", attempt_binding_version=1, execution_id=execution_id)
    with pytest.raises(GrantError, match="INVALID_EXECUTION_BINDING"):
        verify(key, sign(key, invalid), ["tool"], ".")


def test_capture_signature_requires_signed_binding_and_exact_private_bytes(tmp_path) -> None:
    import hashlib
    import hmac
    import json
    import os
    import time

    from aiagent_development.code_intelligence.artifact_capture import capture_scip
    from aiagent_domain import canonical_json, sha256_hex, uuid7
    from aiagent_sandbox.capture_signing import sign_capture
    from aiagent_sandbox.grant import Grant, GrantError

    output = tmp_path / "output"
    parent = tmp_path / "private"
    output.mkdir()
    parent.mkdir(mode=0o700)
    data = b"synthetic artifact"
    (output / "index.scip").write_bytes(data)
    fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    execution = uuid7()
    binding = {
        "execution": execution,
        "attempt": "a",
        "intent": "i",
        "tenant": "t",
        "owner": "o",
        "task": "task",
        "run": "run",
        "goal_revision": 1,
        "lease_epoch": 1,
    }
    grant = Grant(
        "g",
        "t",
        "task",
        "run",
        "i",
        1,
        ("tool",),
        ".",
        1000,
        1024,
        int(time.time()) + 30,
        attempt_id="a",
        attempt_binding_version=1,
        execution_binding_version=1,
        execution_id=execution,
        execution_binding_sha256=sha256_hex(binding),
    )
    key = b"k" * 32
    try:
        with capture_scip(fd, parent, expected_uid=os.geteuid(), deadline=time.monotonic() + 5) as captured:
            proof = sign_capture(
                key,
                grant,
                binding,
                captured.fd,
                expected_sha256=captured.sha256,
                size=captured.byte_length,
                deadline=time.monotonic() + 5,
            )
            envelope = json.loads(proof)
            expected = hmac.new(key, canonical_json(envelope["payload"]).encode(), hashlib.sha256).hexdigest()
            assert hmac.compare_digest(envelope["mac"], expected)
            assert envelope["payload"]["artifact_sha256"] == hashlib.sha256(data).hexdigest()
            with pytest.raises(GrantError, match="BINDING"):
                sign_capture(
                    key,
                    grant,
                    {**binding, "owner": "other"},
                    captured.fd,
                    expected_sha256=captured.sha256,
                    size=captured.byte_length,
                    deadline=time.monotonic() + 5,
                )
            with pytest.raises(GrantError, match="DIGEST"):
                sign_capture(
                    key,
                    grant,
                    binding,
                    captured.fd,
                    expected_sha256="0" * 64,
                    size=captured.byte_length,
                    deadline=time.monotonic() + 5,
                )
    finally:
        os.close(fd)
