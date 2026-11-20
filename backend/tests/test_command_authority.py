from __future__ import annotations

import time

import pytest
from aiagent_development.code_intelligence.indexer_recipe import approved_recipe_digest
from aiagent_domain import sha256_hex, uuid7
from aiagent_sandbox.command_authority import command_template_digest, issue_command_grant, verify_command_binding
from aiagent_sandbox.command_receipt import sign_command_receipt, verify_command_receipt
from aiagent_sandbox.grant import GrantError, verify
from aiagent_sandbox.source_ticket import sign_source_ticket, verify_source_ticket


def test_command_grant_binds_exact_recipe_argv_and_test_id() -> None:
    descriptor = {
        "kind": "test",
        "test_id": "unit",
        "argv": ["/usr/local/bin/python3.13", "-B", "test.py"],
        "cwdRelative": ".",
    }
    p = {
        "version": 1,
        "direction": "gateway-to-supervisor",
        "session": uuid7(),
        "tenant": uuid7(),
        "owner": uuid7(),
        "task": uuid7(),
        "run": uuid7(),
        "goal": 1,
        "lease": 1,
        "execution": uuid7(),
        "attempt": uuid7(),
        "snapshot": uuid7(),
        "recipe": uuid7(),
        "binding_sha256": "0" * 64,
        "repository_binding": uuid7(),
        "revision": "1",
        "commit": "a" * 40,
        "tree": "b" * 40,
        "manifest_sha256": "c" * 64,
        "source_sha256": "d" * 64,
        "source_bytes": 128,
        "expires_at": int(time.time()) + 30,
    }
    request_sha = sha256_hex({"tool": "sandbox_exec", "args": {"argv": descriptor["argv"], "cwdRelative": "."}})
    binding = {
        "execution": p["execution"],
        "tenant": p["tenant"],
        "owner": p["owner"],
        "task": p["task"],
        "run": p["run"],
        "goal_revision": 1,
        "lease_epoch": 1,
        "attempt": p["attempt"],
        "snapshot": p["snapshot"],
        "recipe_id": p["recipe"],
        "repository_binding": p["repository_binding"],
        "workspace_revision": 1,
        "commit": p["commit"],
        "tree": p["tree"],
        "manifest_sha256": p["manifest_sha256"],
        "source_sha256": p["source_sha256"],
        "intent": uuid7(),
        "test_id": "unit",
        "plan_sha256": "e" * 64,
        "request_sha256": request_sha,
        "toolchain_sha256": "f" * 64,
        "image_digest": "sha256:" + "1" * 64,
    }
    binding["recipe_sha256"] = approved_recipe_digest(
        kind="test",
        test_id="unit",
        plan_sha256=binding["plan_sha256"],
        request_sha256=request_sha,
        template_sha256=command_template_digest(descriptor),
        toolchain_sha256=binding["toolchain_sha256"],
        image_digest=binding["image_digest"],
    )
    p["binding_sha256"] = sha256_hex(binding)
    ticket = sign_source_ticket(b"t" * 32, p)
    grant = issue_command_grant(b"t" * 32, b"g" * 32, ticket, binding, descriptor)
    verify(b"g" * 32, grant, descriptor["argv"], ".")
    with pytest.raises(GrantError):
        verify(b"g" * 32, grant, ["/usr/local/bin/python3.13", "-B", "other.py"], ".")
    authenticated = verify_source_ticket(b"t" * 32, ticket)
    for bad in (
        {**descriptor, "test_id": "security"},
        {**descriptor, "argv": ["/usr/local/bin/python3.13", "-B", "other.py"]},
        {**descriptor, "cwdRelative": "../escape"},
        {**descriptor, "argv": ["/tmp/unapproved"]},
        {**descriptor, "kind": "build", "test_id": None},
    ):
        with pytest.raises(ValueError):
            verify_command_binding(authenticated, binding, bad)


def test_command_receipt_withholds_output_and_rejects_tampering() -> None:
    from aiagent_sandbox.grant import Grant

    execution, attempt = uuid7(), uuid7()
    binding = {
        "execution": execution,
        "tenant": "tenant",
        "owner": "owner",
        "task": "task",
        "run": "run",
        "intent": "intent",
        "attempt": attempt,
        "goal_revision": 1,
        "lease_epoch": 1,
        "snapshot": "snapshot",
        "workspace": "workspace",
        "workspace_revision": 1,
        "commit": "a" * 40,
        "recipe_id": "recipe",
        "toolchain_sha256": "b" * 64,
    }
    grant = Grant(
        "grant",
        "tenant",
        "task",
        "run",
        "intent",
        1,
        ("/usr/bin/python3", "test.py"),
        ".",
        1000,
        1024,
        int(time.time()) + 30,
        attempt_id=attempt,
        attempt_binding_version=1,
        execution_binding_version=1,
        execution_id=execution,
        execution_binding_sha256=sha256_hex(binding),
    )
    descriptor = {"kind": "test", "test_id": "unit"}
    result = {
        "status": "EXITED",
        "executionId": execution,
        "attemptId": attempt,
        "exitCode": 0,
        "timedOut": False,
        "truncated": False,
        "stdout": "sensitive-output",
        "stderr": "secret-error",
    }
    body, proof = sign_command_receipt(b"r" * 32, grant, binding, descriptor, result)
    assert b"sensitive-output" not in body and b"secret-error" not in body
    verified = verify_command_receipt(b"r" * 32, body, proof, binding, descriptor)
    assert verified["exit_code"] == 0 and verified["timed_out"] is False and verified["test_id"] == "unit"
    for bad_body, bad_proof, bad_binding, bad_descriptor in (
        (body + b" ", proof, binding, descriptor),
        (body, b"0" * len(proof), binding, descriptor),
        (body, proof, {**binding, "attempt": uuid7()}, descriptor),
        (body, proof, binding, {**descriptor, "test_id": "other"}),
    ):
        with pytest.raises(ValueError):
            verify_command_receipt(b"r" * 32, bad_body, bad_proof, bad_binding, bad_descriptor)


def test_unattested_command_execution_is_unavailable_in_production(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from aiagent_sandbox.command_execution import command_registration_factory

    monkeypatch.setenv("AIAGENT_PROFILE", "production")
    with pytest.raises(RuntimeError, match="production toolchain attestation"):
        command_registration_factory(None, tmp_path, tmp_path, b"r" * 32, ("/usr/bin/python3",))  # type: ignore[arg-type]
