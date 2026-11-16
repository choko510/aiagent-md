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
                "exitCode": exit_code,
                "timedOut": timeout,
                "truncated": truncated,
                "stdout": "",
                "stderr": "",
            }

    monkeypatch.setattr(aiagent_core.rpc, "RpcClient", Client)
    executor = SandboxCellExecutor("unix:/unused", b"r" * 32, b"g" * 32)
    executor.context["key"] = {"tenant_id": "t", "task_id": "t", "run_id": "r", "intent_id": "i", "lease_epoch": 1}
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
    executor.context["key"] = {"tenant_id": "t", "task_id": "t", "run_id": "r", "intent_id": "i", "lease_epoch": 1}
    with pytest.raises(exception):
        asyncio.run(executor.execute("key", {"argv": ["tool"], "cwdRelative": "."}, None))


def test_gateway_clipping_marks_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    class Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def call(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {
                "status": "EXITED",
                "exitCode": 0,
                "timedOut": False,
                "truncated": False,
                "stdout": "x" * 8001,
                "stderr": "",
            }

    monkeypatch.setattr(aiagent_core.rpc, "RpcClient", Client)
    executor = SandboxCellExecutor("unix:/unused", b"r" * 32, b"g" * 32)
    executor.context["key"] = {"tenant_id": "t", "task_id": "t", "run_id": "r", "intent_id": "i", "lease_epoch": 1}
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
    request = ExecutionRequest(scope, RunFence("t", "r", 1, 1, 1), "plan", kind, ("tool",), "a" * 64, "intent")
    with pytest.raises(InvalidEvidence, match="verified workspace"):
        adapter.execute(request)
    db.tx.assert_not_called()
    gateway.submit.assert_not_called()


def test_multibyte_and_large_results_do_not_gain_false_completeness() -> None:
    text = "日本語" * 2000
    result = {"exitCode": 0, "timedOut": False, "truncated": True, "outcome": "OUTPUT_LIMIT", "stdout": text}
    with pytest.raises(InvalidEvidence):
        _complete_sandbox_result(result)
    result.update(truncated=False, outcome="EXITED_ZERO")
    assert _complete_sandbox_result(result)["stdout"] == text
