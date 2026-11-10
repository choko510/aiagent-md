"""AI-02 CTX/ACM deterministic acceptance and fault injection (mock only)."""
from dataclasses import replace

import pytest

from aiagent_context import (
    Anchor, AnchorKind, ContextRequest, ContextService, MockArtifactStore, MockHeadPort,
    ModelBudget, ProviderOverflow, SafeStop, Scope, SourceInput, SummaryClaim,
    SummaryProposal, ToolEvent, verify_tool_pairs,
)


def setup():
    scope = Scope('tenant-A', 'owner-A', 'task-A', 'run-A')
    budget = ModelBudget('fake', 'r1', 'byte-upper-bound', 10000, 2500, 150,
                         250, 300, 50)
    anchors = (
        Anchor(AnchorKind.GOAL_REF, 'goal:2', 'hotel for three'),
        Anchor(AnchorKind.CONSTRAINT, 'goal:2', 'maximum total price'),
        Anchor(AnchorKind.EXCLUSION, 'goal:2', 'no nonrefundable rooms'),
        Anchor(AnchorKind.DATE_TIME, 'goal:2', '2026-11-21T18:00:00+09:00'),
        Anchor(AnchorKind.PARTY_SIZE, 'goal:2', '3'),
        Anchor(AnchorKind.MONEY, 'goal:2', '48000', currency='JPY'),
        Anchor(AnchorKind.TIMEZONE, 'goal:2', 'Asia/Tokyo'),
        Anchor(AnchorKind.PENDING_EFFECT, 'ledger:9', 'UNKNOWN'),
        Anchor(AnchorKind.APPROVAL, 'approval:7', 'NOT_GRANTED'),
        Anchor(AnchorKind.UNRESOLVED, 'goal:2', 'parking billed separately'),
    )
    req = ContextRequest(scope, 'head:0', '11', '2', 'p1', 'epoch-1', 'cursor:9', anchors, budget)
    head = MockHeadPort(scope, head='head:0', goal_revision='2', lease_epoch='11',
                        policy_version='p1', memory_epoch='epoch-1')
    store = MockArtifactStore()
    sources = (SourceInput('offer:1', '2026-11-11T00:00:00Z',
                           ('Base JPY 48,000. Parking JPY 2,000 extra. ' * 800).encode(),
                           locator_ref='line:1-800'),)
    return req, head, store, sources


def expect_code(code, callback):
    with pytest.raises(SafeStop) as exc:
        callback()
    assert exc.value.code == code


def test_anchors_are_typed_immutable_and_preserved():
    req, head, store, sources = setup()
    plan = ContextService(store, head).compact(req, sources)
    assert plan.anchors == req.anchors
    assert plan.reserved_output_tokens == 2500
    assert plan.estimated_input_tokens + 2500 + req.budget.safety_margin <= 10000
    assert 'NOT_GRANTED' in plan.model_payload and 'UNKNOWN' in plan.model_payload
    assert 'Asia/Tokyo' in plan.model_payload and '48000' in plan.model_payload
    with pytest.raises((AttributeError, TypeError)):
        plan.anchors = ()
    assert len(plan.raw_artifacts) == 1
    assert store.get_raw(req.scope, plan.raw_artifacts[0]) == sources[0].body
    assert len(plan.model_payload) < len(sources[0].body)
    assert store.get_summary(req.scope, plan.summary_artifact_ref)


def test_attempt_to_invent_price_fails_without_head_change():
    req, head, store, sources = setup()

    class BadSummarizer:
        def __call__(self, request, refs, artifacts, max_chars):
            ref = refs[0]
            return SummaryProposal(request.goal_revision, request.anchor_digest,
                (ref.artifact_ref,), (SummaryClaim('Parking included in JPY 48000',
                'Base JPY 48,000', ref.artifact_ref, ref.sha256, ref.observed_at, 0, 15),))

    expect_code('UNSUPPORTED_CLAIM', lambda: ContextService(store, head).compact(
        req, sources, summarizer=BadSummarizer()))
    assert head.head == req.expected_head
    assert head.published == []
    assert len(store._raw) == 1  # original survives rejected candidate


def test_source_digest_or_locator_mismatch_fails():
    req, head, store, sources = setup()

    class BadHash:
        def __call__(self, request, refs, artifacts, max_chars):
            r = refs[0]
            return SummaryProposal(request.goal_revision, request.anchor_digest,
                (r.artifact_ref,), (SummaryClaim('Base', 'Base', r.artifact_ref,
                '0' * 64, r.observed_at, 0, 4),))

    expect_code('SOURCE_MISMATCH', lambda: ContextService(store, head).compact(
        req, sources, summarizer=BadHash()))
    assert head.head == 'head:0'


def test_original_lost_or_tampered_after_commit_blocks_dispatch():
    for mutate, expected in [('drop_raw', 'ARTIFACT_UNAVAILABLE'), ('tamper_raw', 'SOURCE_MISMATCH')]:
        req, head, store, sources = setup()
        service = ContextService(store, head)
        plan = service.compact(req, sources)
        if mutate == 'drop_raw':
            store.drop_raw(plan.raw_artifacts[0].artifact_ref)
        else:
            store.tamper_raw(plan.raw_artifacts[0].artifact_ref, b'fabricated')
        expect_code(expected, lambda: service.check_dispatch(plan, '11'))


def test_kill_after_summary_before_cas_preserves_previous_head():
    req, head, store, sources = setup()
    head.fail_before_cas = True
    expect_code('SIMULATED_KILL_BEFORE_CAS', lambda: ContextService(store, head).compact(req, sources))
    assert head.head == 'head:0'
    assert head.published == []
    assert len(store._raw) == 1
    assert len(store._summary) == 1  # orphan, not public; cleanup owned by AI-07


def test_goal_steer_and_revoked_memory_epoch_refuse_stale_summary():
    req, head, store, sources = setup()
    plan = ContextService(store, head).compact(req, sources)
    head.goal_revision = '3'
    expect_code('STALE_CONTEXT', lambda: ContextService(store, head).compact(
        replace(req, expected_head=plan.committed_head), sources))
    expect_code('STALE_CONTEXT', lambda: ContextService(store, head).check_dispatch(plan, '11'))
    head.goal_revision = '2'
    head.memory_epoch = 'epoch-2'
    expect_code('STALE_CONTEXT', lambda: ContextService(store, head).check_dispatch(plan, '11'))


def test_provider_overflow_is_bounded_and_never_retries_ambiguous_failures():
    req, head, store, sources = setup()

    class FakeProvider:
        calls = 0
        def send(self, plan):
            self.calls += 1
            raise ProviderOverflow()

    provider = FakeProvider()
    expect_code('OVERFLOW_RETRY_EXHAUSTED', lambda: ContextService(store, head)
                .dispatch_with_overflow_limit(req, sources, provider))
    assert provider.calls == 2
    assert len(head.published) == 2

    class AmbiguousProvider:
        calls = 0
        def send(self, plan):
            self.calls += 1
            raise TimeoutError('unknown acknowledgement')

    req, head, store, sources = setup()
    other = AmbiguousProvider()
    with pytest.raises(TimeoutError):
        ContextService(store, head).dispatch_with_overflow_limit(req, sources, other)
    assert other.calls == 1


def test_budget_mandatory_anchors_cannot_be_dropped():
    req, head, store, sources = setup()
    req = replace(req, budget=replace(req.budget, context_window=2800,
                                      output_reserve=2500, safety_margin=150))
    expect_code('BLOCKED_CONTEXT_CAPACITY', lambda: ContextService(store, head).compact(req, sources))
    assert head.head == 'head:0'


def test_offload_write_failure_never_publishes_summary():
    req, head, store, sources = setup()
    store.fail_next_write = True
    expect_code('OFFLOAD_FAILED', lambda: ContextService(store, head).compact(req, sources))
    assert head.head == 'head:0'
    assert not store._summary


def test_tools_multi_call_pairing_and_orphans():
    events = (ToolEvent('tool_call', 'a'), ToolEvent('tool_call', 'b'),
              ToolEvent('tool_result', 'b'), ToolEvent('tool_result', 'a'))
    assert verify_tool_pairs(events) == ('b', 'a')
    for invalid in [events[:-1], (ToolEvent('tool_result', 'a'),),
                    (ToolEvent('tool_call', 'a'), ToolEvent('tool_result', 'a'),
                     ToolEvent('tool_result', 'a'))]:
        expect_code('TOOL_PAIR_BROKEN', lambda: verify_tool_pairs(invalid))
    req, head, store, sources = setup()
    plan = ContextService(store, head).compact(req, sources, events=events)
    assert plan.tool_pairs == ('b', 'a')


def test_secrets_rejected_at_ingress():
    expect_code('SECRET_DETECTED', lambda: SourceInput('source', '2026-11-10T00:00:00Z',
                           b'Authorization: Bearer ABC123', locator_ref='line:1'))
    expect_code('SECRET_DETECTED', lambda: SourceInput('api_key=something',
                           '2026-11-10T00:00:00Z', b'normal', locator_ref='line:1'))


def test_custom_summarizer_cannot_escalate_authority():
    req, head, store, sources = setup()

    class FakePolicy:
        def __call__(self, request, refs, artifacts, max_chars):
            r = refs[0]
            return SummaryProposal(request.goal_revision, request.anchor_digest,
                (r.artifact_ref,), (SummaryClaim('Base', 'Base', r.artifact_ref,
                r.sha256, r.observed_at, 0, 4),), asserted_authority='SYSTEM')

    expect_code('QUALITY_FAILED', lambda: ContextService(store, head).compact(
        req, sources, summarizer=FakePolicy()))
    assert head.head == 'head:0'
