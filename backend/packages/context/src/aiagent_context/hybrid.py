"""Hybrid selective in-process projection compaction (AI-02 §18.23 / §19)."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from hashlib import sha256
from typing import Literal

from .models import (
    ContextPlan,
    ContextRequest,
    OptimizationReceipt,
    SafeStop,
    SourceInput,
    ToolEvent,
    forbid_secrets,
)
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


class HybridCompactionPipeline:
    """Runs optional selective pre-projection optimization, grounded ContextService compaction, and NLI audit."""

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
        from .compaction import ContextService

        if not isinstance(self._service, ContextService):
            raise SafeStop("INVALID_SERVICE")
        optimized_sources: list[SourceInput] = []
        last_receipt: OptimizationReceipt | None = None
        total_before = 0
        total_after = 0
        any_optimized = False
        for src, cls in sources:
            opt_src, receipt = self._optimizer.optimize_source(
                src,
                source_class=cls,
                model_id=request.budget.model_id,
                tokenizer_id=request.budget.tokenizer_id,
            )
            optimized_sources.append(opt_src)
            total_before += receipt.before_tokens
            total_after += receipt.after_tokens
            if receipt.outcome == "OPTIMIZED":
                any_optimized = True
            last_receipt = receipt
        plan = self._service.compact(
            request,
            tuple(optimized_sources),
            events=events,
            max_excerpt_chars=max_excerpt_chars,
        )
        if last_receipt is None:
            return plan
        agg_receipt = OptimizationReceipt(
            codec_id=self._optimizer.CODEC_ID,
            codec_version=self._optimizer.CODEC_VERSION,
            outcome="OPTIMIZED" if any_optimized else last_receipt.outcome,
            before_tokens=total_before,
            after_tokens=total_after,
            input_sha256=last_receipt.input_sha256,
            output_sha256=last_receipt.output_sha256,
            reason_code="SELECTIVE_COMPACTION" if any_optimized else last_receipt.reason_code,
        )
        return replace(plan, optimization_receipt=agg_receipt)
