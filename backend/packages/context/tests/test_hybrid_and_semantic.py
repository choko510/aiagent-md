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
    assert len(plan.optimization_receipt.traces) == 3
    assert len(plan.verified_facts) == 3
    assert all(f.verdict == "SUPPORTED" for f in plan.verified_facts)

    # P1-5: Raw artifact store MUST hold exact unmodified raw bytes for every source (never elided bytes!)
    raw_cli = cli_lines.encode("utf-8")
    raw_json = json.dumps(json_items).encode("utf-8")
    expected_bodies = {"cli:1": raw_cli, "json:1": raw_json, "prose:1": short_prose}
    for ref in plan.raw_artifacts:
        stored = store.get_raw(req.scope, ref)
        assert stored == expected_bodies[ref.source_ref]
    # And the trace records derived projections separately from the unmodified raw SHA-256.
    by_src = {t.source_ref: t for t in plan.optimization_receipt.traces}
    assert by_src["cli:1"].raw_sha256 != by_src["cli:1"].projection_sha256
    assert by_src["cli:1"].projection_artifact_ref is not None
    assert by_src["prose:1"].projection_artifact_ref is None
    assert any("ERROR: timeout on step 4" in c.quote for c in plan.selected_claims)

    # Multi-source aggregate SHA-256 changes when a non-last source changes (cannot reuse only last_receipt).
    cli_lines_alt = "\n".join(["ok heartbeat line"] * 26 + ["ERROR: timeout on step 4"])
    head2 = MockHeadPort(
        req.scope, head="head:0", goal_revision="1", lease_epoch="1", policy_version="policy:1", memory_epoch="1"
    )
    pipe2 = HybridCompactionPipeline(
        ContextService(MockArtifactStore(), head2), HybridOptimizer(ModelBoundTokenEstimator())
    )
    plan2 = pipe2.compact(
        req,
        (
            (
                SourceInput("cli:1", "2026-11-16T00:00:00Z", cli_lines_alt.encode("utf-8"), locator_ref="log:cli"),
                "cli_stdout",
            ),
            (SourceInput("prose:1", "2026-11-16T00:00:00Z", short_prose, locator_ref="log:prose"), "prose"),
        ),
    )
    assert plan2.optimization_receipt is not None
    assert plan2.optimization_receipt.input_sha256 != plan.optimization_receipt.input_sha256

    # Passthrough when no savings
    tiny = SourceInput("tiny:1", "2026-11-16T00:00:00Z", b"short ok", locator_ref="log:tiny")
    out_src, rec = HybridOptimizer().optimize_source(tiny, source_class="cli_stdout")
    assert out_src == tiny and rec.outcome == "PASSTHROUGH"


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-4o", "o200k_base"),
        ("gpt-5", "o200k_base"),
        ("gpt-5-mini", "o200k_base"),
        ("o3-mini", "o200k_base"),
        ("claude-sonnet-4.5", "anthropic_claude_est"),
        ("gemini-2.5-flash", "cl100k_base"),
        ("fake-gateway", "fake_bpe_est"),
        ("production-main", "cl100k_base"),
    ],
)
def test_tokenizer_profiles_cover_reasoning_and_unknown_models(model: str, expected: str) -> None:
    from aiagent_context.tokenizer import tokenizer_for_model

    assert tokenizer_for_model(model) == expected


def test_estimator_handles_japanese_english_code_and_json_inputs() -> None:
    tok = ModelBoundTokenEstimator()
    for model in ("gpt-4o", "gpt-5", "o3-mini", "claude-sonnet-4.5", "gemini-2.5-flash"):
        tokenizer = "anthropic_claude_est" if model.startswith("claude") else "o200k_base"
        for text in ("日本語の文章です。", "English prose.", "def f(x): return x + 1", '{"key": "value"}'):
            assert tok.count_tokens(text, model_id=model, tokenizer_id=tokenizer) > 0


def test_anthropic_tokenizer_uses_conservative_non_openai_margin_and_revokes_projections_on_cas_failure() -> None:
    from aiagent_context.tokenizer import tokenizer_for_model

    tok = ModelBoundTokenEstimator()
    assert tokenizer_for_model("claude-sonnet-5-5") == "anthropic_claude_est"
    assert tok.count_tokens(
        "hello world status", model_id="claude-sonnet-5-5", tokenizer_id="anthropic_claude_est"
    ) >= tok.count_tokens("hello world status", model_id="gpt-4o", tokenizer_id="cl100k_base")

    req = _req()
    store = MockArtifactStore()

    class _FailHead(MockHeadPort):
        def publish(self, *args, **kwargs):  # type: ignore[override]
            raise SafeStop("HEAD_CONFLICT")

    svc = ContextService(
        store,
        _FailHead(
            req.scope, head="head:0", goal_revision="1", lease_epoch="1", policy_version="policy:1", memory_epoch="1"
        ),
    )
    pipe = HybridCompactionPipeline(svc)
    cli_lines = "\n".join(["ok heartbeat line"] * 25 + ["ERROR: timeout on step 4"])
    with pytest.raises(SafeStop, match="HEAD_CONFLICT"):
        pipe.compact(
            req,
            (
                (
                    SourceInput("cli:1", "2026-11-16T00:00:00Z", cli_lines.encode("utf-8"), locator_ref="log:cli"),
                    "cli_stdout",
                ),
            ),
        )
    # Derived summary/projection artifacts must be revoked on CAS failure while original raw stays intact.
    assert len(store._summary) == 0
    assert len(store._raw) == 1
    assert next(iter(store._raw.values()))[2] == cli_lines.encode("utf-8")


def test_long_single_line_json_preserves_trailing_error_multiple_negations_and_applies_model_payload_projection() -> (
    None
):
    req = _req()
    store = MockArtifactStore()
    head = MockHeadPort(
        req.scope, head="head:0", goal_revision="1", lease_epoch="1", policy_version="policy:1", memory_epoch="1"
    )
    pipe = HybridCompactionPipeline(ContextService(store, head), HybridOptimizer(ModelBoundTokenEstimator()))

    compact_json = json.dumps(
        [{"id": i, "status": "ok"} for i in range(25)] + [{"id": 99, "status": "ERROR: downstream timeout"}],
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(compact_json) > 500
    assert b"\n" not in compact_json

    multi_negation_log = "\n".join(
        ["not authorized for admin scope"]
        + ["ok benign telemetry tick"] * 25
        + ["ERROR: final checkpoint write failed"]
    ).encode("utf-8")

    plan = pipe.compact(
        req,
        (
            (SourceInput("json:1", "2026-11-16T00:00:00Z", compact_json, locator_ref="log:json"), "json_data"),
            (SourceInput("cli:1", "2026-11-16T00:00:00Z", multi_negation_log, locator_ref="log:cli"), "cli_stdout"),
        ),
        max_excerpt_chars=240,
    )

    quotes = "\n".join(c.quote for c in plan.selected_claims)
    assert "ERROR: downstream timeout" in quotes
    assert "not authorized for admin scope" in quotes
    assert "ERROR: final checkpoint write failed" in quotes

    payload = json.loads(plan.model_payload)
    projs = payload["untrustedData"].get("optimizedProjections")
    assert isinstance(projs, list) and len(projs) == 2
    assert any('"_omitted":21' in p["text"] for p in projs)
    assert any("identical lines omitted" in p["text"] for p in projs)

    receipt = plan.optimization_receipt
    assert receipt is not None
    assert receipt.outcome == "OPTIMIZED"
    assert receipt.payload_tokens_before is not None and receipt.payload_tokens_after is not None
    assert receipt.payload_tokens_after < receipt.payload_tokens_before

    raw_expected = {"json:1": compact_json, "cli:1": multi_negation_log}
    for ref in plan.raw_artifacts:
        assert store.get_raw(req.scope, ref) == raw_expected[ref.source_ref]
