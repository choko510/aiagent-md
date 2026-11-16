"""NLI and SourceBackedFact entailment verifiers for AI-02 Context auditing."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Protocol

from .models import (
    ContextRequest,
    EntailmentVerdict,
    Polarity,
    RawArtifact,
    SafeStop,
    SourceBackedFact,
    SummaryClaim,
)
from .ports import ArtifactStorePort

_NEGATION_RE = re.compile(
    r"(?i)(?:\b(?:not|no|never|without|except|excluding|unless|neither|nor|cannot|can't|won't|don't|doesn't|didn't|"
    r"prohibited|forbidden|denied|excluded|unavailable|refund|non-refundable)\b|"
    r"(?:非|不|未|無|不可|禁止|除外|なし|ない|ではない|ではなく|対象外))"
)
_NUMBER_TOKEN_RE = re.compile(r"(?:[$€£¥]?\s*\d+(?:[.,]\d+)?(?:\s*(?:%|USD|JPY|EUR|GBP|円|泊|名|人|days?|nights?))?)")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?\r\n。！？])\s*")


class SemanticVerifierPort(Protocol):
    def verify_claims(
        self,
        request: ContextRequest,
        refs: tuple[RawArtifact, ...],
        store: ArtifactStorePort,
        claims: tuple[SummaryClaim, ...],
    ) -> tuple[SourceBackedFact, ...]: ...


def _span_sentence_window(full_text: str, start_char: int, end_char: int) -> tuple[str, str, str]:
    """Return (prefix_in_same_sentence, quote, suffix_in_same_sentence) around [start_char:end_char]."""
    quote = full_text[start_char:end_char]
    if start_char > 0 and full_text[start_char - 1] in ".!?\r\n。！？":
        prefix = ""
    else:
        left = 0
        for m in re.finditer(r"[.!?\r\n。！？]", full_text[:start_char]):
            left = m.end()
        prefix = full_text[left:start_char]
    if quote and quote[-1] in ".!?\r\n。！？":
        suffix = ""
    else:
        right_m = re.search(r"[.!?\r\n。！？]", full_text[end_char:])
        right = end_char + right_m.start() if right_m is not None else len(full_text)
        suffix = full_text[end_char:right]
    return prefix, quote, suffix


def _normalize_num_token(tok: str) -> str:
    return re.sub(r"\s+", "", tok).lower()


class HeuristicNliVerifier:
    """Deterministic source entailment verifier checking polarity and numeric/currency fidelity."""

    def verify_claims(
        self,
        request: ContextRequest,
        refs: tuple[RawArtifact, ...],
        store: ArtifactStorePort,
        claims: tuple[SummaryClaim, ...],
    ) -> tuple[SourceBackedFact, ...]:
        by_ref = {r.artifact_ref: r for r in refs}
        facts: list[SourceBackedFact] = []
        for claim in claims:
            record = by_ref.get(claim.artifact_ref)
            if record is None:
                raise SafeStop("COVERAGE_GAP")
            raw = store.get_raw(request.scope, record)
            if len(raw) == 0:
                continue
            full_text = raw.decode("utf-8", "strict")
            start_char = len(raw[: claim.start_byte].decode("utf-8", "strict"))
            end_char = len(raw[: claim.end_byte].decode("utf-8", "strict"))
            prefix, spanned_quote, suffix = _span_sentence_window(full_text, start_char, end_char)
            window = prefix + spanned_quote + suffix

            edge_neg = bool(_NEGATION_RE.search(prefix) or _NEGATION_RE.search(suffix))
            quote_neg = bool(_NEGATION_RE.search(claim.quote))
            if edge_neg and not quote_neg:
                raise SafeStop("FACT_REFUTED")

            win_nums = {_normalize_num_token(m.group(0)) for m in _NUMBER_TOKEN_RE.finditer(window)}
            # Ignore a trailing number split at an excerpt byte boundary (e.g. "2" before ",000").
            quote_num_matches = [
                m
                for m in _NUMBER_TOKEN_RE.finditer(claim.quote)
                if not (m.end() == len(claim.quote) and suffix[:1] in (",", ".") and suffix[1:2].isdigit())
            ]
            quote_nums = {_normalize_num_token(m.group(0)) for m in quote_num_matches}
            text_nums = {_normalize_num_token(m.group(0)) for m in _NUMBER_TOKEN_RE.finditer(claim.text)}
            # Reject claims that alter numbers or drop numerical constraints present inside the immediate clause.
            if not quote_nums.issubset(win_nums) or (claim.text != claim.quote and not text_nums.issubset(win_nums)):
                raise SafeStop("FACT_REFUTED")

            polarity: Polarity = "NEGATED" if quote_neg else "AFFIRMED"
            fact_type = "NUMERIC" if quote_nums else ("CONSTRAINT" if quote_neg else "EXCERPT")
            value = ";".join(sorted(quote_nums)) if quote_nums else claim.quote[:128]
            facts.append(
                SourceBackedFact(
                    artifact_ref=claim.artifact_ref,
                    fact_type=fact_type,
                    value=value,
                    polarity=polarity,
                    verdict="SUPPORTED",
                    source_hash=record.sha256,
                )
            )
        return tuple(facts)


LlmEntailmentCallback = Callable[[str, str, SummaryClaim], EntailmentVerdict]


class GatedLlmNliVerifier:
    """Fail-closed wrapper for external LLM NLI verifiers; rejects REFUTED/UNVERIFIED or missing keys."""

    def __init__(
        self,
        callback: LlmEntailmentCallback | None = None,
        *,
        require_llm: bool = True,
        baseline: SemanticVerifierPort | None = None,
    ) -> None:
        self._callback = callback
        self._require_llm = require_llm
        self._baseline = baseline if baseline is not None else HeuristicNliVerifier()

    def verify_claims(
        self,
        request: ContextRequest,
        refs: tuple[RawArtifact, ...],
        store: ArtifactStorePort,
        claims: tuple[SummaryClaim, ...],
    ) -> tuple[SourceBackedFact, ...]:
        facts = self._baseline.verify_claims(request, refs, store, claims)
        if self._callback is None:
            if self._require_llm:
                raise SafeStop("SEMANTIC_VERIFIER_UNAVAILABLE")
            return facts
        by_ref = {r.artifact_ref: r for r in refs}
        verified: list[SourceBackedFact] = []
        for idx, claim in enumerate(claims):
            record = by_ref[claim.artifact_ref]
            raw = store.get_raw(request.scope, record)
            if len(raw) == 0:
                continue
            full_text = raw.decode("utf-8", "strict")
            verdict = self._callback(claim.quote, full_text, claim)
            if verdict != "SUPPORTED":
                raise SafeStop("FACT_REFUTED")
            if idx < len(facts):
                verified.append(facts[idx])
        return tuple(verified) if verified else facts
