"""AI-02 Memory ACL, provenance, epoch, consent, retention, failure gates."""
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from aiagent_context import ContextRequest, ContextService, MockArtifactStore, MockHeadPort, ModelBudget, SafeStop, Scope, SourceInput, Anchor, AnchorKind
from aiagent_memory import (MemoryRecord, MemoryRetriever, MockConsent,
                            MockMemoryRepository, MockRetention, MockSources)

SCOPE = Scope('tenant-1', 'owner-1', 'task-1', 'run-1')
NOW = datetime(2026, 11, 11, tzinfo=UTC)


def memory(ref='mem:1', text='likes scenic walking', *, tenant='tenant-1', owner='owner-1',
           task=None, status='active', expires='2026-11-12T00:00:00Z'):
    return MemoryRecord(ref, tenant, owner, task, text, ('source:known',),
                        '2026-11-10T00:00:00Z', expires, status=status)


def retriever(records):
    repo = MockMemoryRepository(records)
    return repo, MemoryRetriever(repo, consent=MockConsent(True),
                                 retention=MockRetention(True),
                                 sources=MockSources({'source:known'}))


def test_cross_tenant_and_cross_owner_poisoned_index_denied():
    repo, m = retriever((memory(), memory('other-tenant', tenant='tenant-2'),
                         memory('other-owner', owner='owner-2'),
                         memory('other-task', task='task-2')))
    repo.index_override = ('other-tenant', 'other-owner', 'other-task', 'mem:1')
    projection = m.read(SCOPE, query='walking', recipient='fake:us', now=NOW)
    assert [p.ref for p in projection.projections] == ['mem:1']
    assert projection.projections[0].trust_tier == 'MEMORY_DATA'
    assert projection.projections[0].source_refs == ('source:known',)


def test_default_consent_retention_source_fail_closed():
    repo = MockMemoryRepository((memory(),))
    for consent, retention, sources in [
            (MockConsent(), MockRetention(True), MockSources({'source:known'})),
            (MockConsent(True), MockRetention(), MockSources({'source:known'})),
            (MockConsent(True), MockRetention(True), MockSources()),
    ]:
        m = MemoryRetriever(repo, consent=consent, retention=retention, sources=sources)
        assert not m.read(SCOPE, query='scenic', recipient='fake:us', now=NOW).projections


def test_epoch_revoked_during_search_blocks_stale_projection():
    repo, m = retriever((memory(),))
    repo.after_search = lambda repository: setattr(repository, 'epoch_value', 'epoch-2')
    with pytest.raises(SafeStop, match='STALE_MEMORY'):
        m.read(SCOPE, query='scenic', recipient='fake:us', now=NOW)


def test_retention_expiry_and_tombstone_are_not_resurrected():
    repo, m = retriever((memory('expired', expires='2026-11-10T00:00:00Z'),
                         memory('revoked', status='revoked')))
    repo.index_override = ('expired', 'revoked')
    assert not m.read(SCOPE, query='scenic', recipient='fake:us', now=NOW).projections


def test_secret_contamination_rejected():
    with pytest.raises(SafeStop, match='SECRET_DETECTED'):
        memory(text='cookie: SID=x')
    repo, m = retriever((memory(),))
    with pytest.raises(SafeStop, match='SECRET_DETECTED'):
        m.read(SCOPE, query='password=abc123', recipient='fake:us', now=NOW)


def test_readonly_projection_injected_as_data_not_permission():
    repo, m = retriever((memory(),))
    result = m.read(SCOPE, query='walking', recipient='fake:us', now=NOW)
    anchors = (Anchor(AnchorKind.GOAL_REF, 'goal:1', 'walk'),
               Anchor(AnchorKind.EXCLUSION, 'goal:1', 'no toll roads'))
    request = ContextRequest(SCOPE, 'h0', '1', '1', 'p1', result.source_epoch,
                             'cursor1', anchors, ModelBudget('fake', '1', 'byte', 8000, 1000, 100),
                             result.projections)
    head = MockHeadPort(SCOPE, head='h0', goal_revision='1', lease_epoch='1',
                        policy_version='p1', memory_epoch=result.source_epoch)
    store = MockArtifactStore()
    plan = ContextService(store, head).compact(request,
        (SourceInput('s', '2026-11-11T00:00:00Z', b'Walking trail', locator_ref='line:1'),))
    assert 'MEMORY_DATA' in plan.model_payload
    assert 'no toll roads' in plan.model_payload
    assert plan.anchors == request.anchors
    head.memory_epoch = 'epoch-2'  # revocation after building, before sending
    with pytest.raises(SafeStop, match='STALE_CONTEXT'):
        ContextService(store, head).check_dispatch(plan, '1')
