from __future__ import annotations

import pytest
from aiagent_domain import uuid7
from aiagent_sandbox.source_ticket import sign_source_ticket, source_transfer_key, verify_source_ticket


def test_ticket_authentication_expiry_and_execution_scope() -> None:
    key = b"t" * 32
    payload = {
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
        "binding_sha256": "a" * 64,
        "repository_binding": uuid7(),
        "revision": "1",
        "commit": "b" * 40,
        "tree": "c" * 40,
        "manifest_sha256": "d" * 64,
        "source_sha256": "e" * 64,
        "source_bytes": 128,
        "expires_at": 1045,
    }
    ticket = sign_source_ticket(key, payload)
    assert verify_source_ticket(key, ticket, now=1000) == payload
    assert source_transfer_key(key, payload["session"]) != source_transfer_key(key, uuid7())
    for invalid in (
        {**ticket, "payload": {**payload, "execution": uuid7()}},
        sign_source_ticket(key, {**payload, "execution": "bad\nidentifier"}),
        sign_source_ticket(key, {**payload, "direction": "supervisor-to-gateway"}),
        sign_source_ticket(key, {**payload, "source_bytes": 128 * 1024 * 1024 + 1}),
        sign_source_ticket(key, {**payload, "expires_at": 999}),
    ):
        with pytest.raises(ValueError):
            verify_source_ticket(key, invalid, now=1000)
