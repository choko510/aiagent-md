"""PostgreSQL-backed Context artifact slice. Not a model-dispatch integration test."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from aiagent_context.models import SafeStop, Scope, SourceInput
from aiagent_core import commands, control
from aiagent_core.db import Database
from aiagent_gateways.context_artifacts import PostgresArtifactStore

pytestmark = pytest.mark.pg


def make_store(dbs: dict[str, Database], tenant: dict[str, str]) -> tuple[PostgresArtifactStore, Scope, control.Claim]:
    ids = commands.submit_task(
        dbs["api"], commands.Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"]), "artifact test"
    )
    claim = control.claim_run(dbs["runtime"], tenant["tenantId"], ids["runId"], "artifact-worker")
    scope = Scope(claim.tenant_id, claim.owner_user_id, claim.task_id, claim.run_id)
    store = PostgresArtifactStore(
        dbs["model_gateway"], claim, b"k" * 32, expires_at=datetime.now(UTC) + timedelta(days=1)
    )
    return store, scope, claim


def test_artifact_durable_encrypted_scoped_and_revoked(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    store, scope, claim = make_store(dbs, tenant)
    source = SourceInput(
        "source-private", "2026-01-01T00:00:00Z", b"private original text", locator_ref="locator-private"
    )
    item = store.put_raw(scope, source)
    ref, digest = store.put_summary(scope, b"exact summary")
    # A new store instance simulates application restart while preserving the same key.
    restarted = PostgresArtifactStore(
        dbs["model_gateway"], claim, b"k" * 32, expires_at=datetime.now(UTC) + timedelta(days=1)
    )
    assert restarted.get_raw(scope, item) == source.body
    assert restarted.read_range(scope, item, 0, 7) == b"private"
    with pytest.raises(SafeStop, match="INVALID_LOCATOR"):
        restarted.read_range(scope, item, 0, len(source.body) + 1)
    assert restarted.get_summary(scope, ref) == b"exact summary"
    assert len(digest) == 64
    row = dbs["owner"].one("SELECT ciphertext FROM agent.context_artifacts WHERE id=%s", (item.artifact_ref,))
    assert b"private original text" not in bytes(row["ciphertext"])
    assert b"source-private" not in bytes(row["ciphertext"])
    with pytest.raises(SafeStop, match="ARTIFACT_UNAVAILABLE"):
        restarted.get_raw(Scope(scope.tenant_id, "other", scope.task_id, scope.run_id), item)
    with pytest.raises(SafeStop, match="ARTIFACT_UNAVAILABLE"):
        restarted.get_raw(Scope("other-tenant", scope.owner_id, scope.task_id, scope.run_id), item)
    _, other_scope, _ = make_store(dbs, tenant)
    with pytest.raises(SafeStop, match="ARTIFACT_UNAVAILABLE"):
        restarted.get_raw(other_scope, item)
    with pytest.raises(SafeStop, match="SOURCE_MISMATCH"):
        restarted.get_raw(scope, replace(item, sha256="0" * 64))
    wrong_key = PostgresArtifactStore(
        dbs["model_gateway"], claim, b"x" * 32, expires_at=datetime.now(UTC) + timedelta(days=1)
    )
    with pytest.raises(SafeStop, match="SOURCE_MISMATCH"):
        wrong_key.get_raw(scope, item)
    with pytest.raises(SafeStop, match="SECRET_DETECTED"):
        restarted.put_summary(scope, b"password=unsafe")
    restarted.revoke(scope, item.artifact_ref)
    with pytest.raises(SafeStop, match="ARTIFACT_UNAVAILABLE"):
        restarted.get_raw(scope, item)
    assert (
        dbs["owner"].one("SELECT ciphertext FROM agent.context_artifacts WHERE id=%s", (item.artifact_ref,))[
            "ciphertext"
        ]
        is None
    )


def test_tamper_expiry_and_stale_lease_fail_closed(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    store, scope, claim = make_store(dbs, tenant)
    source = SourceInput("source", "2026-01-01T00:00:00Z", b"original", locator_ref="locator")
    item = store.put_raw(scope, source)
    dbs["owner"].one(
        "UPDATE agent.context_artifacts SET ciphertext = decode('00','hex') WHERE id=%s RETURNING id",
        (item.artifact_ref,),
    )
    with pytest.raises(SafeStop, match="SOURCE_MISMATCH"):
        store.get_raw(scope, item)
    expired = store.put_raw(scope, source)
    dbs["owner"].one(
        "UPDATE agent.context_artifacts SET created_at=now()-interval '2 days',expires_at=now()-interval '1 day' WHERE id=%s RETURNING id",
        (expired.artifact_ref,),
    )
    with pytest.raises(SafeStop, match="ARTIFACT_UNAVAILABLE"):
        store.get_raw(scope, expired)
    assert dbs["ops"].one("SELECT agent.gc_expired_context_artifacts() AS n")["n"] >= 1
    assert (
        dbs["owner"].one("SELECT ciphertext FROM agent.context_artifacts WHERE id=%s", (expired.artifact_ref,))[
            "ciphertext"
        ]
        is None
    )
    retained = store.put_raw(scope, source)
    dbs["owner"].one(
        "UPDATE agent.runs SET lease_expires_at=now()-interval '1 second' WHERE id=%s RETURNING id", (claim.run_id,)
    )
    with pytest.raises(SafeStop, match="STALE_CONTEXT"):
        store.put_raw(scope, source)
    new_claim = control.claim_run(dbs["runtime"], scope.tenant_id, scope.run_id, "new-worker")
    with pytest.raises(SafeStop, match="STALE_CONTEXT"):
        store.get_raw(scope, retained)
    recovered = PostgresArtifactStore(
        dbs["model_gateway"], new_claim, b"k" * 32, expires_at=datetime.now(UTC) + timedelta(days=1)
    )
    assert recovered.get_raw(scope, retained) == source.body


def test_database_rollback_leaves_no_artifact(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    _, scope, claim = make_store(dbs, tenant)

    class AbortDatabase:
        @contextmanager
        def tx(self):
            with dbs["model_gateway"].tx() as c:
                yield c
                raise RuntimeError("simulated crash before commit")

    store = PostgresArtifactStore(AbortDatabase(), claim, b"k" * 32, expires_at=datetime.now(UTC) + timedelta(days=1))  # type: ignore[arg-type]
    source = SourceInput("source", "2026-01-01T00:00:00Z", b"not committed", locator_ref="locator")
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.put_raw(scope, source)
    assert (
        dbs["owner"].one("SELECT count(*) AS n FROM agent.context_artifacts WHERE run_id=%s", (claim.run_id,))["n"] == 0
    )
