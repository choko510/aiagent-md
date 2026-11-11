"""Safe ContextPlan construction: raw first, audited excerpt, sealed selection, HEAD CAS last.

No LLM summary is accepted as an instruction or a new authoritative fact. This
minimal auditable vertical slice permits ONLY exact, source-backed UTF-8 excerpts;
free-form semantic summarization intentionally remains INTEGRATION_PENDING.
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from hashlib import sha256
from typing import Protocol

from .models import (
    ContextPlan,
    ContextPublication,
    ContextRequest,
    RawArtifact,
    SafeStop,
    SourceInput,
    SummaryClaim,
    SummaryProposal,
    ToolEvent,
    forbid_secrets,
    stable_digest,
)
from .ports import ArtifactStorePort, HeadPort


class ProviderOverflow(RuntimeError):
    """Explicit provider context-window rejection, not a network timeout/unknown ACK."""


class ProviderPort(Protocol):
    def send(self, plan: ContextPlan) -> str: ...


class SummarizerPort(Protocol):
    def __call__(
        self, request: ContextRequest, refs: tuple[RawArtifact, ...], store: ArtifactStorePort, max_chars: int
    ) -> SummaryProposal: ...


class MockExcerptSummarizer:
    """Deterministic finite source excerpt producer, no new claims."""

    def __call__(
        self, request: ContextRequest, refs: tuple[RawArtifact, ...], store: ArtifactStorePort, max_chars: int
    ) -> SummaryProposal:
        claims: list[SummaryClaim] = []
        for record in refs:
            original = store.get_raw(request.scope, record)
            try:
                decoded = original.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise SafeStop("UNSUPPORTED_SOURCE_ENCODING") from exc
            text = decoded[:max_chars]
            claims.append(
                SummaryClaim(
                    text, text, record.artifact_ref, record.sha256, record.observed_at, 0, len(text.encode("utf-8"))
                )
            )
        return SummaryProposal(
            request.goal_revision, request.anchor_digest, tuple(ref.artifact_ref for ref in refs), tuple(claims)
        )


def verify_tool_pairs(events: tuple[ToolEvent, ...]) -> tuple[str, ...]:
    """Allow parallel outstanding calls, but never accept orphan/duplicate results."""
    pending: set[str] = set()
    seen: set[str] = set()
    completed: list[str] = []
    for event in events:
        if not event.call_id:
            raise SafeStop("TOOL_PAIR_BROKEN")
        if event.role == "tool_call":
            if event.call_id in seen:
                raise SafeStop("TOOL_PAIR_BROKEN")
            pending.add(event.call_id)
            seen.add(event.call_id)
        elif event.role == "tool_result":
            if event.call_id not in pending:
                raise SafeStop("TOOL_PAIR_BROKEN")
            completed.append(event.call_id)
            pending.remove(event.call_id)
        else:
            raise SafeStop("TOOL_PAIR_BROKEN")
    if pending:
        raise SafeStop("TOOL_PAIR_BROKEN")
    return tuple(completed)


def audit_proposal(
    request: ContextRequest, refs: tuple[RawArtifact, ...], store: ArtifactStorePort, candidate: SummaryProposal
) -> None:
    """Byte-span/source integrity and strict textual entailment (exact quote only).

    A hash or an existing citation alone is never evidence of semantic support.
    Unsupported paraphrases/derived prices must be rejected, not auto-verified.
    """
    if (
        candidate.goal_revision != request.goal_revision
        or candidate.anchor_digest != request.anchor_digest
        or candidate.asserted_authority != "OBSERVATION"
    ):
        raise SafeStop("QUALITY_FAILED")
    expected = {record.artifact_ref: record for record in refs}
    if (
        len(expected) != len(refs)
        or set(candidate.covered_artifact_refs) != set(expected)
        or len(candidate.covered_artifact_refs) != len(expected)
    ):
        raise SafeStop("COVERAGE_GAP")
    if len(candidate.claims) != len(refs) or {c.artifact_ref for c in candidate.claims} != set(expected):
        raise SafeStop("COVERAGE_GAP")
    for claim in candidate.claims:
        record = expected[claim.artifact_ref]
        original = store.get_raw(request.scope, record)  # ACL + rehash, including after generation
        if claim.source_hash != record.sha256 or claim.observed_at != record.observed_at:
            raise SafeStop("SOURCE_MISMATCH")
        if not (0 <= claim.start_byte < claim.end_byte <= len(original)):
            raise SafeStop("INVALID_LOCATOR")
        try:
            quote = original[claim.start_byte : claim.end_byte].decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise SafeStop("INVALID_LOCATOR") from exc
        if not quote or quote != claim.quote or claim.text != claim.quote:
            raise SafeStop("UNSUPPORTED_CLAIM")
        forbid_secrets(claim.text)


def _serialize(
    request: ContextRequest,
    refs: tuple[RawArtifact, ...],
    candidate: SummaryProposal,
    tool_pairs: tuple[str, ...],
    memory_count: int,
) -> tuple[bytes, str]:
    summary = {
        "schemaMajor": 1,
        "goalRevision": request.goal_revision,
        "anchorDigest": request.anchor_digest,
        "rawEventCursor": request.raw_event_cursor,
        "rawSources": [asdict(item) for item in refs],
        "claims": [asdict(item) for item in candidate.claims],
        "toolPairs": tool_pairs,
        "memory": [asdict(item) for item in request.memory[:memory_count]],
    }
    sealed = json.dumps(summary, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload = json.dumps(
        {
            "untrustedDataOnly": True,
            "sourceBackedExcerpts": [asdict(item) for item in candidate.claims],
            "typedCanonicalAnchors": [
                dict(kind=a.kind.value, sourceRef=a.source_ref, value=a.value, unit=a.unit, currency=a.currency)
                for a in request.anchors
            ],
            "memoryDataNotInstructions": [asdict(item) for item in request.memory[:memory_count]],
            "toolPairIds": tool_pairs,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    forbid_secrets(payload)
    return sealed, payload


class ContextService:
    def __init__(self, artifacts: ArtifactStorePort, head: HeadPort):
        self.artifacts = artifacts
        self.head = head

    def _validate_current(self, request: ContextRequest) -> None:
        self.head.assert_current(
            request.scope,
            request.expected_head,
            request.goal_revision,
            request.lease_epoch,
            request.policy_version,
            request.memory_source_epoch,
        )

    def compact(
        self,
        request: ContextRequest,
        sources: tuple[SourceInput, ...],
        *,
        events: tuple[ToolEvent, ...] = (),
        summarizer: SummarizerPort | None = None,
        max_excerpt_chars: int = 240,
    ) -> ContextPlan:
        self._validate_current(request)
        if max_excerpt_chars < 1 or max_excerpt_chars > 4096 or len(sources) > 64:
            raise SafeStop("INVALID_COMPACTION_LIMIT")
        if len({src.source_ref for src in sources}) != len(sources):
            raise SafeStop("DUPLICATE_SOURCE")
        tool_pairs = verify_tool_pairs(events)
        # No summarizer calls or publication before *every* raw source is durable.
        refs = tuple(self.artifacts.put_raw(request.scope, src) for src in sources)
        for ref in refs:
            self.artifacts.get_raw(request.scope, ref)
        candidate = (summarizer or MockExcerptSummarizer())(request, refs, self.artifacts, max_excerpt_chars)
        audit_proposal(request, refs, self.artifacts, candidate)
        memory_count = len(request.memory)
        while True:
            summary, payload = _serialize(request, refs, candidate, tool_pairs, memory_count)
            # Byte length is a conservative *fixture estimator*, not verified model tokenizer.
            estimated = (
                len(payload.encode("utf-8"))
                + request.budget.system_tokens
                + request.budget.tool_schema_tokens
                + request.budget.framing_tokens
            )
            if (
                estimated + request.budget.output_reserve + request.budget.safety_margin
                <= request.budget.context_window
            ):
                break
            if memory_count:
                memory_count -= 1
                continue
            raise SafeStop("BLOCKED_CONTEXT_CAPACITY")
        # Summary is a selection record. Original bytes remain separately retrievable.
        summary_ref, summary_hash = self.artifacts.put_summary(request.scope, summary)
        payload_ref, payload_digest = self.artifacts.put_summary(request.scope, payload.encode("utf-8"))
        plan_digest = stable_digest(
            {
                "scope": asdict(request.scope),
                "oldHead": request.expected_head,
                "leaseEpoch": request.lease_epoch,
                "goalRevision": request.goal_revision,
                "policy": request.policy_version,
                "memoryEpoch": request.memory_source_epoch,
                "model": asdict(request.budget),
                "sources": [asdict(r) for r in refs],
                "summary": (summary_ref, summary_hash),
                "anchors": request.anchor_digest,
                "payload": sha256(payload.encode()).hexdigest(),
                "cursor": request.raw_event_cursor,
            }
        )
        self._validate_current(request)
        new_head = self.head.publish(
            request.scope,
            request.expected_head,
            request.goal_revision,
            request.lease_epoch,
            request.policy_version,
            request.memory_source_epoch,
            plan_digest,
            ContextPublication(
                request, refs, request.memory[:memory_count], summary_ref, summary_hash, payload_ref, payload_digest
            ),
        )
        return ContextPlan(
            request.scope,
            request.goal_revision,
            request.policy_version,
            request.memory_source_epoch,
            request.expected_head,
            new_head,
            request.anchors,
            refs,
            summary_ref,
            summary_hash,
            candidate.claims,
            request.memory[:memory_count],
            estimated,
            request.budget.output_reserve,
            plan_digest,
            payload,
            sha256(payload.encode()).hexdigest(),
            request.budget.model_id,
            request.budget.model_revision,
            request.budget.tokenizer_id,
            tool_pairs,
            request.raw_event_cursor,
        )

    def check_dispatch(self, plan: ContextPlan, lease_epoch: str) -> None:
        self.head.check_dispatch(plan, lease_epoch)
        # Final raw re-validation also detects retention expiry or post-publication tampering.
        for record in plan.raw_artifacts:
            self.artifacts.get_raw(plan.scope, record)
        if sha256(plan.model_payload.encode()).hexdigest() != plan.model_payload_digest:
            raise SafeStop("MODEL_PAYLOAD_MISMATCH")

    def dispatch_with_overflow_limit(
        self,
        request: ContextRequest,
        sources: tuple[SourceInput, ...],
        provider: ProviderPort,
        *,
        events: tuple[ToolEvent, ...] = (),
        max_overflow_retries: int = 1,
    ) -> tuple[str, ContextPlan]:
        if max_overflow_retries < 0 or max_overflow_retries > 1:
            raise SafeStop("INVALID_RETRY_LIMIT")
        seen_payloads: set[str] = set()
        current = request
        for attempt in range(max_overflow_retries + 1):
            plan = self.compact(current, sources, events=events, max_excerpt_chars=240 // (4**attempt))
            self.check_dispatch(plan, current.lease_epoch)
            if plan.model_payload_digest in seen_payloads:
                raise SafeStop("UNCHANGED_OVERFLOW_PAYLOAD")
            seen_payloads.add(plan.model_payload_digest)
            try:
                return provider.send(plan), plan
            except ProviderOverflow as exc:
                if attempt >= max_overflow_retries:
                    raise SafeStop("OVERFLOW_RETRY_EXHAUSTED") from exc
                # Explicit overflow only; never retry timeout/UNKNOWN effects.
                current = replace(current, expected_head=plan.committed_head)
        raise AssertionError("unreachable")
