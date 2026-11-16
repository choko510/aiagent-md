"""Hybrid selective in-process projection compaction (AI-02 §18.23 / §19)."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from hashlib import sha256
from typing import Literal

from .compaction import ContextService, _revoke_written_summaries
from .models import (
    ContextPlan,
    ContextRequest,
    OptimizationReceipt,
    RawArtifact,
    SafeStop,
    SourceInput,
    SourceProjectionTrace,
    SummaryClaim,
    SummaryProposal,
    ToolEvent,
    forbid_secrets,
    stable_digest,
)
from .ports import ArtifactStorePort
from .tokenizer import ModelBoundTokenEstimator, TokenEstimatorPort

SourceClass = Literal["cli_stdout", "json_data", "search_hits", "prose", "unknown"]
_NEGATION_GUARD_RE = re.compile(r"(?i)\b(?:error|fail(?:ed)?|exception|denied|refused|not|never|no|excluding)\b")


class HybridOptimizer:
    """In-process lossless/selective projection filter that preserves safety and fails closed on regressions."""

    CODEC_ID = "aiagent-hybrid-selective"
    CODEC_VERSION = "1.0"

    def __init__(self, tokenizer: TokenEstimatorPort | None = None) -> None:
        self._tok = tokenizer if tokenizer is not None else ModelBoundTokenEstimator()

    def optimize_source(
        self,
        source: SourceInput,
        *,
        source_class: SourceClass = "unknown",
        model_id: str = "fake-gateway",
        tokenizer_id: str = "cl100k_base",
    ) -> tuple[SourceInput, OptimizationReceipt]:
        raw_bytes = source.body
        forbid_secrets(raw_bytes)
        text = raw_bytes.decode("utf-8", "strict")
        input_sha = sha256(raw_bytes).hexdigest()
        before_tokens = self._tok.count_tokens(text, model_id=model_id, tokenizer_id=tokenizer_id)

        transformed = text
        if source_class == "cli_stdout":
            transformed = self._compact_cli(text)
        elif source_class == "json_data":
            transformed = self._compact_json(text)
        elif source_class in ("search_hits", "prose", "unknown"):
            return source, OptimizationReceipt(
                codec_id=self.CODEC_ID,
                codec_version=self.CODEC_VERSION,
                outcome="PASSTHROUGH",
                before_tokens=before_tokens,
                after_tokens=before_tokens,
                input_sha256=input_sha,
                output_sha256=input_sha,
                reason_code="UNELIGIBLE_SOURCE_CLASS",
            )

        out_bytes = transformed.encode("utf-8")
        forbid_secrets(out_bytes)
        out_sha = sha256(out_bytes).hexdigest()
        after_tokens = self._tok.count_tokens(transformed, model_id=model_id, tokenizer_id=tokenizer_id)

        if after_tokens >= before_tokens:
            return source, OptimizationReceipt(
                codec_id=self.CODEC_ID,
                codec_version=self.CODEC_VERSION,
                outcome="PASSTHROUGH",
                before_tokens=before_tokens,
                after_tokens=before_tokens,
                input_sha256=input_sha,
                output_sha256=input_sha,
                reason_code="NO_TOKEN_SAVINGS",
            )

        return (
            replace(source, body=out_bytes),
            OptimizationReceipt(
                codec_id=self.CODEC_ID,
                codec_version=self.CODEC_VERSION,
                outcome="OPTIMIZED",
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                input_sha256=input_sha,
                output_sha256=out_sha,
                reason_code="SELECTIVE_COMPACTION",
            ),
        )

    @staticmethod
    def _compact_cli(text: str) -> str:
        """Collapse repeated benign lines while always preserving errors, failures, negations, and tail lines."""
        lines = text.splitlines()
        if len(lines) <= 6:
            return text
        out: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            j = i + 1
            if not _NEGATION_GUARD_RE.search(line):
                while j < len(lines) and lines[j] == line:
                    j += 1
            count = j - i
            out.append(line)
            if count > 2:
                out.append(f"... ({count - 1} identical lines omitted)")
            elif count == 2:
                out.append(lines[i + 1])
            i = j
        return "\n".join(out)

    @staticmethod
    def _compact_json(text: str) -> str:
        try:
            data = json.loads(text)
        except ValueError:
            return text

        def trim(obj: object) -> object:
            if isinstance(obj, dict):
                return {k: trim(v) for k, v in obj.items()}
            if isinstance(obj, list) and len(obj) > 8:
                middle = obj[3:-2]
                # Never elide elements containing negations/errors/failures.
                if any(_NEGATION_GUARD_RE.search(json.dumps(item, ensure_ascii=False)) for item in middle):
                    return [trim(x) for x in obj]
                head = [trim(x) for x in obj[:3]]
                tail = [trim(x) for x in obj[-2:]]
                return [*head, {"_omitted": len(middle), "_total": len(obj)}, *tail]
            return obj

        return json.dumps(trim(data), ensure_ascii=False, separators=(",", ":"))


def _verbatim_raw_span(raw_bytes: bytes, max_chars: int) -> tuple[int, int, str]:
    """Select an exact verbatim UTF-8 byte span inside unmodified raw bytes, keeping guard/error lines intact."""
    decoded = raw_bytes.decode("utf-8", "strict")
    if len(decoded) <= max_chars:
        return 0, len(raw_bytes), decoded
    # If a guard/negation/error line exists later in the raw text and fits within max_chars, ground directly to it.
    for match in _NEGATION_GUARD_RE.finditer(decoded):
        line_start = decoded.rfind("\n", 0, match.start()) + 1
        line_end = decoded.find("\n", match.end())
        if line_end == -1:
            line_end = len(decoded)
        span = decoded[line_start:line_end]
        if 0 < len(span) <= max_chars:
            start_b = len(decoded[:line_start].encode("utf-8"))
            end_b = start_b + len(span.encode("utf-8"))
            return start_b, end_b, span
    text = decoded[:max_chars]
    if (
        text
        and decoded[max_chars].isascii()
        and decoded[max_chars].isalnum()
        and text[-1].isascii()
        and text[-1].isalnum()
    ):
        cut = len(text)
        while cut > 0 and text[cut - 1].isascii() and text[cut - 1].isalnum():
            cut -= 1
        trimmed = text[:cut].rstrip()
        if trimmed:
            text = trimmed
    return 0, len(text.encode("utf-8")), text


class _HybridSummarizer:
    def __init__(self, spans_by_ref: dict[str, tuple[int, int, str]]) -> None:
        self._spans = spans_by_ref

    def __call__(
        self,
        request: ContextRequest,
        refs: tuple[RawArtifact, ...],
        store: ArtifactStorePort,
        max_chars: int,
    ) -> SummaryProposal:
        del store, max_chars
        claims = tuple(
            SummaryClaim(
                text=self._spans[ref.artifact_ref][2],
                quote=self._spans[ref.artifact_ref][2],
                artifact_ref=ref.artifact_ref,
                source_hash=ref.sha256,
                observed_at=ref.observed_at,
                start_byte=self._spans[ref.artifact_ref][0],
                end_byte=self._spans[ref.artifact_ref][1],
            )
            for ref in refs
        )
        return SummaryProposal(
            request.goal_revision,
            request.anchor_digest,
            tuple(ref.artifact_ref for ref in refs),
            claims,
        )


class HybridCompactionPipeline:
    """Persists unmodified raw artifacts FIRST, creates derived projections separately, and records full traces."""

    def __init__(self, context_service: object, optimizer: HybridOptimizer | None = None) -> None:
        self._service = context_service
        self._optimizer = optimizer if optimizer is not None else HybridOptimizer()

    def compact(
        self,
        request: ContextRequest,
        sources: tuple[tuple[SourceInput, SourceClass], ...],
        *,
        events: tuple[ToolEvent, ...] = (),
        max_excerpt_chars: int = 240,
    ) -> ContextPlan:
        if not isinstance(self._service, ContextService):
            raise SafeStop("INVALID_SERVICE")
        if max_excerpt_chars < 1 or max_excerpt_chars > 4096 or len(sources) > 64:
            raise SafeStop("INVALID_COMPACTION_LIMIT")
        raw_sources = tuple(src for src, _ in sources)
        if len({src.source_ref for src in raw_sources}) != len(raw_sources):
            raise SafeStop("DUPLICATE_SOURCE")

        self._service._validate_current(request)
        # 1. Persist every unmodified raw source FIRST before any projection or optimization.
        raw_refs = tuple(self._service.artifacts.put_raw(request.scope, src) for src in raw_sources)
        raw_by_ref = {ref.artifact_ref: self._service.artifacts.get_raw(request.scope, ref) for ref in raw_refs}

        spans_by_ref: dict[str, tuple[int, int, str]] = {}
        traces: list[SourceProjectionTrace] = []
        written_projections: list[str] = []
        total_before = 0
        total_after = 0
        any_optimized = False

        try:
            for (src, cls), raw_ref in zip(sources, raw_refs, strict=True):
                raw_bytes = raw_by_ref[raw_ref.artifact_ref]
                if len(raw_bytes) != len(src.body):
                    raise SafeStop("SOURCE_MISMATCH")
                opt_src, receipt = self._optimizer.optimize_source(
                    src,
                    source_class=cls,
                    model_id=request.budget.model_id,
                    tokenizer_id=request.budget.tokenizer_id,
                )
                proj_ref: str | None = None
                if receipt.outcome == "OPTIMIZED":
                    proj_ref, proj_sha = self._service.artifacts.put_summary(request.scope, opt_src.body)
                    written_projections.append(proj_ref)
                    if proj_sha != receipt.output_sha256:
                        raise SafeStop("PROJECTION_DIGEST_MISMATCH")
                    any_optimized = True
                start_b, end_b, span_text = _verbatim_raw_span(raw_bytes, max_excerpt_chars)
                spans_by_ref[raw_ref.artifact_ref] = (start_b, end_b, span_text)
                total_before += receipt.before_tokens
                total_after += receipt.after_tokens
                traces.append(
                    SourceProjectionTrace(
                        source_ref=src.source_ref,
                        raw_artifact_ref=raw_ref.artifact_ref,
                        raw_sha256=raw_ref.sha256,
                        raw_byte_length=raw_ref.byte_length,
                        projection_artifact_ref=proj_ref,
                        projection_sha256=receipt.output_sha256,
                        preserved_start_byte=start_b,
                        preserved_end_byte=end_b,
                        outcome=receipt.outcome,
                        before_tokens=receipt.before_tokens,
                        after_tokens=receipt.after_tokens,
                        reason_code=receipt.reason_code,
                    )
                )

            plan = self._service.compact(
                request,
                raw_sources,
                events=events,
                summarizer=_HybridSummarizer(spans_by_ref),
                max_excerpt_chars=max_excerpt_chars,
                _existing_refs=raw_refs,
            )
        except SafeStop as exc:
            if exc.code != "SIMULATED_KILL_BEFORE_CAS":
                _revoke_written_summaries(self._service.artifacts, request.scope, written_projections)
            raise
        except Exception:
            _revoke_written_summaries(self._service.artifacts, request.scope, written_projections)
            raise

        if not traces:
            return plan
        agg_input_sha = (
            traces[0].raw_sha256 if len(traces) == 1 else stable_digest([(t.source_ref, t.raw_sha256) for t in traces])
        )
        agg_output_sha = (
            traces[0].projection_sha256
            if len(traces) == 1
            else stable_digest([(t.source_ref, t.projection_sha256) for t in traces])
        )
        agg_receipt = OptimizationReceipt(
            codec_id=self._optimizer.CODEC_ID,
            codec_version=self._optimizer.CODEC_VERSION,
            outcome="OPTIMIZED" if any_optimized else traces[-1].outcome,
            before_tokens=total_before,
            after_tokens=total_after,
            input_sha256=agg_input_sha,
            output_sha256=agg_output_sha,
            reason_code="SELECTIVE_COMPACTION" if any_optimized else traces[-1].reason_code,
            traces=tuple(traces),
        )
        return replace(plan, optimization_receipt=agg_receipt)
