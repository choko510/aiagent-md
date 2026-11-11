"""Memory Writer transaction and privilege checks against PostgreSQL."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from aiagent_context.models import Scope as ContextScope
from aiagent_context.models import SourceInput
from aiagent_core import commands, control, memory_writer
from aiagent_core.db import Database
from aiagent_gateways.context_artifacts import PostgresArtifactStore

pytestmark = pytest.mark.pg


def setup_memory(dbs: dict[str, Database], tenant: dict[str, str]):  # noqa: ANN201
    owner = commands.Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    ids = commands.submit_task(dbs["api"], owner, "memory test")
    claim = control.claim_run(dbs["runtime"], owner.tenant_id, ids["runId"], "memory-worker")
    scope = ContextScope(claim.tenant_id, claim.owner_user_id, claim.task_id, claim.run_id)
    store = PostgresArtifactStore(
        dbs["model_gateway"], claim, b"k" * 32, expires_at=datetime.now(UTC) + timedelta(days=2)
    )
    source = store.put_raw(scope, SourceInput("source", "2026-01-01T00:00:00Z", b"source", locator_ref="source-loc"))
    projection = store.put_raw(
        scope, SourceInput("projection", "2026-01-01T00:00:00Z", b"remembered data", locator_ref="projection-loc")
    )
    return owner, scope, store, source, projection


def create(dbs: dict[str, Database], owner: commands.Scope, scope: ContextScope, source: str, projection: str):  # noqa: ANN201
    return memory_writer.create_memory(
        dbs["api"],
        owner,
        task_id=scope.task_id,
        source_ref=source,
        projection_ref=projection,
        recipient="fake",
        observed_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=1),
        consent_basis="user_explicit",
    )


def test_writer_epoch_and_source_revocation(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    owner, scope, store, source, projection = setup_memory(dbs, tenant)
    memory_id, epoch = create(dbs, owner, scope, source.artifact_ref, projection.artifact_ref)
    assert epoch == 2
    with dbs["model_gateway"].tx() as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("UPDATE agent.memory_scope_epochs SET source_epoch=1")
    store.revoke(scope, source.artifact_ref)
    assert (
        dbs["owner"].one("SELECT source_epoch FROM agent.memory_scope_epochs WHERE task_id=%s", (scope.task_id,))[
            "source_epoch"
        ]
        == 3
    )
    with pytest.raises(commands.CommandError, match="MEMORY_SOURCE_UNAVAILABLE"):
        create(dbs, owner, scope, source.artifact_ref, projection.artifact_ref)
    assert memory_writer.revoke_memory(dbs["api"], owner, task_id=scope.task_id, memory_id=memory_id) == 4
    assert memory_writer.revoke_memory(dbs["api"], owner, task_id=scope.task_id, memory_id=memory_id) == 4
    record = dbs["owner"].one("SELECT revoked_at, revision FROM agent.memory_records WHERE id=%s", (memory_id,))
    assert record["revoked_at"] is not None and record["revision"] == 2


def test_concurrent_revoke_once(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    owner, scope, _, source, projection = setup_memory(dbs, tenant)
    memory_id, _ = create(dbs, owner, scope, source.artifact_ref, projection.artifact_ref)
    barrier = threading.Barrier(2)
    results: list[int] = []
    errors: list[Exception] = []

    def revoke() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(memory_writer.revoke_memory(dbs["api"], owner, task_id=scope.task_id, memory_id=memory_id))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=revoke) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors and not any(thread.is_alive() for thread in threads)
    assert sorted(results) == [3, 3]


def test_memory_denies_cross_task_and_untrusted_identifiers(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    owner, scope, _, source, projection = setup_memory(dbs, tenant)
    _, other_scope, _, _, _ = setup_memory(dbs, tenant)
    with pytest.raises(commands.CommandError, match="MEMORY_SOURCE_UNAVAILABLE"):
        create(dbs, owner, other_scope, source.artifact_ref, projection.artifact_ref)
    with pytest.raises(commands.CommandError, match="INVALID_MEMORY"):
        create(dbs, owner, scope, "' OR true--", projection.artifact_ref)
    with dbs["model_gateway"].tx() as c:
        assert not c.execute("SELECT id FROM agent.memory_records").fetchall()


def test_real_memory_retriever_rechecks_consent_and_epoch(
    dbs: dict[str, Database], tenant: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiagent_context.models import SafeStop
    from aiagent_gateways.context_memory import PostgresMemoryRepository

    owner, scope, store, source, projection = setup_memory(dbs, tenant)
    memory_id, epoch = create(dbs, owner, scope, source.artifact_ref, projection.artifact_ref)
    repo = PostgresMemoryRepository(dbs["model_gateway"], store._claim, store)
    result = repo.retrieval(scope, query="remembered", recipient="fake")
    assert result.source_epoch == str(epoch)
    assert len(result.projections) == 1 and result.projections[0].ref == memory_id
    assert result.projections[0].text == "remembered data" and result.projections[0].record_revision == 1
    assert not repo.retrieval(scope, query="remembered", recipient="other").projections
    search = repo.search

    def revoke_after_search(scope_arg: ContextScope, query: str, limit: int) -> tuple[str, ...]:
        refs = search(scope_arg, query, limit)
        memory_writer.revoke_memory(dbs["api"], owner, task_id=scope.task_id, memory_id=memory_id)
        return refs

    monkeypatch.setattr(repo, "search", revoke_after_search)
    with pytest.raises(SafeStop, match="STALE_MEMORY"):
        repo.retrieval(scope, query="remembered", recipient="fake")
    assert not repo.retrieval(scope, query="remembered", recipient="fake").projections
