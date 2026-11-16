"""Tests for NLI/SourceBackedFact verification, model-bound tokenizer budgets, and hybrid compaction."""

from __future__ import annotations

import json

import pytest
from aiagent_context import (
    Anchor,
    AnchorKind,
    ContextRequest,
    ContextService,
    GatedLlmNliVerifier,
    HeuristicNliVerifier,
    HybridCompactionPipeline,
    HybridOptimizer,
    MockArtifactStore,
    MockHeadPort,
    ModelBoundTokenEstimator,
    ModelBudget,
    SafeStop,
    Scope,
    SourceInput,
    SummaryClaim,
    SummaryProposal,
)


def _req() -> ContextRequest:
    return ContextRequest(
        scope=Scope("t1", "o1", "task1", "run1"),
        expected_head="head:0",
        lease_epoch="1",
        goal_revision="1",
        policy_version="policy:1",
        memory_source_epoch="1",
        raw_event_cursor="cursor:1",
        anchors=(Anchor(AnchorKind.GOAL_REF, "goal:1", "Book hotel under $150"),),
        budget=ModelBudget("claude-sonnet-5-5", "rev1", "cl100k_base", 8000, 1000, 500),
        recipient="anthropic",
    )


def test_heuristic_nli_verifies_supported_facts_and_refutes_negation_drop() -> None:
    store = MockArtifactStore()
    req = _req()
    src = SourceInput(
        source_ref="hotel:1",
        observed_at="2026-11-16T00:00:00Z",
        body=b"Hotel room is $120 per night. Breakfast is not included in the rate.",
        locator_ref="fixture://hotel/1",
    )
    rec = store.put_raw(req.scope, src)
    raw = src.body.decode("utf-8")
    # Selecting "included in the rate" while dropping "not" in the same sentence must fail closed.
    idx = raw.index("included in the rate")
    bad_claim = SummaryClaim(
        text="included in the rate",
        quote="included in the rate",
        artifact_ref=rec.artifact_ref,
        source_hash=rec.sha256,
        observed_at=rec.observed_at,
        start_byte=idx,
        end_byte=idx + len("included in the rate"),
    )
    prop = SummaryProposal("1", req.anchor_digest, (rec.artifact_ref,), (bad_claim,))
    with pytest.raises(SafeStop, match=r"UNSUPPORTED_CLAIM|FACT_REFUTED"):
        HeuristicNliVerifier().verify_claims(req, (rec,), store, prop.claims)

    # Whole sentence preserves polarity and numeric value -> SUPPORTED
    sent = "Hotel room is $120 per night."
    good_claim = SummaryClaim(
        text=sent,
        quote=sent,
        artifact_ref=rec.artifact_ref,
        source_hash=rec.sha256,
        observed_at=rec.observed_at,
        start_byte=0,
        end_byte=len(sent.encode("utf-8")),
    )
    facts = HeuristicNliVerifier().verify_claims(req, (rec,), store, (good_claim,))
    assert len(facts) == 1
    assert facts[0].verdict == "SUPPORTED" and facts[0].fact_type == "NUMERIC"


def test_gated_llm_nli_fails_closed_without_provider_and_on_refuted() -> None:
    store = MockArtifactStore()
    req = _req()
    rec = store.put_raw(
        req.scope,
        SourceInput("s:1", "2026-11-16T00:00:00Z", b"Parking fee $25 daily.", locator_ref="loc:1"),
    )
    claim = SummaryClaim(
        "Parking fee $25 daily.", "Parking fee $25 daily.", rec.artifact_ref, rec.sha256, rec.observed_at, 0, 22
    )
    with pytest.raises(SafeStop, match="SEMANTIC_VERIFIER_UNAVAILABLE"):
        GatedLlmNliVerifier(None, require_llm=True).verify_claims(req, (rec,), store, (claim,))

    with pytest.raises(SafeStop, match="FACT_REFUTED"):
        GatedLlmNliVerifier(lambda *_a: "REFUTED", require_llm=True).verify_claims(req, (rec,), store, (claim,))

    facts = GatedLlmNliVerifier(lambda *_a: "SUPPORTED", require_llm=True).verify_claims(req, (rec,), store, (claim,))
    assert len(facts) == 1 and facts[0].verdict == "SUPPORTED"


def test_hybrid_compaction_reduces_repetitive_cli_and_json_and_passthroughs_short() -> None:
    req = _req()
    store = MockArtifactStore()
    head = MockHeadPort(
        req.scope, head="head:0", goal_revision="1", lease_epoch="1", policy_version="policy:1", memory_epoch="1"
    )
    svc = ContextService(store, head)
    pipe = HybridCompactionPipeline(svc, HybridOptimizer(ModelBoundTokenEstimator()))

    cli_lines = "\n".join(["ok heartbeat line"] * 25 + ["ERROR: timeout on step 4"])
    json_items = [{"id": i, "status": "ok"} for i in range(20)]
    short_prose = b"Concise note."
    plan = pipe.compact(
        req,
        (
            (
                SourceInput("cli:1", "2026-11-16T00:00:00Z", cli_lines.encode("utf-8"), locator_ref="log:cli"),
                "cli_stdout",
            ),
            (
                SourceInput(
                    "json:1", "2026-11-16T00:00:00Z", json.dumps(json_items).encode("utf-8"), locator_ref="log:json"
                ),
                "json_data",
            ),
            (SourceInput("prose:1", "2026-11-16T00:00:00Z", short_prose, locator_ref="log:prose"), "prose"),
        ),
    )
    assert plan.optimization_receipt is not None
    assert plan.optimization_receipt.outcome == "OPTIMIZED"
    assert plan.optimization_receipt.after_tokens < plan.optimization_receipt.before_tokens
    assert len(plan.verified_facts) == 3
    assert all(f.verdict == "SUPPORTED" for f in plan.verified_facts)

    # Passthrough when no savings
    tiny = SourceInput("tiny:1", "2026-11-16T00:00:00Z", b"short ok", locator_ref="log:tiny")
    out_src, rec = HybridOptimizer().optimize_source(tiny, source_class="cli_stdout")
    assert out_src == tiny and rec.outcome == "PASSTHROUGH"
