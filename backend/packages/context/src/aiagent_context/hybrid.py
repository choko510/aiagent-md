"""Hybrid selective in-process projection compaction (AI-02 §18.23 / §19)."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from hashlib import sha256
from typing import Literal

from .compaction import (
    ContextService,
    MockExcerptSummarizer,
    _check_polarity_preservation,
    _revoke_written_summaries,
    _serialize,
    verify_tool_pairs,
)
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
_NEGATION_GUARD_RE = re.compile(
    r"(?i)(?:\b(?:error|fail(?:ed|ure)?|exception|denied|refused|not|never|no|excluding|"
    r"without|except|unless|neither|nor|cannot|can't|won't|don't|doesn't|didn't|"
    r"isn't|aren't|wasn't|weren't|prohibited|forbidden|excluded|unavailable)\b|"
    r"(?:不可|禁止|なし|ない|しません|しない|できません|ではない|ではなく|対象外|除外))"
)


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
                out: list[object] = [trim(x) for x in obj[:3]]
                benign_run: list[object] = []
                # Never elide elements containing negations/errors/failures; collapse benign runs around them.
                for item in middle:
                    if _NEGATION_GUARD_RE.search(json.dumps(item, ensure_ascii=False)):
                        if len(benign_run) > 2:
                            out.append({"_omitted": len(benign_run), "_total": len(obj)})
                        else:
                            out.extend(trim(x) for x in benign_run)
                        benign_run.clear()
                        out.append(trim(item))
                    else:
                        benign_run.append(item)
                if len(benign_run) > 2:
                    out.append({"_omitted": len(benign_run), "_total": len(obj)})
                else:
                    out.extend(trim(x) for x in benign_run)
                out.extend(trim(x) for x in obj[-2:])
                return out
            return obj

        return json.dumps(trim(data), ensure_ascii=False, separators=(",", ":"))


def _is_polarity_safe(raw_bytes: bytes, start_b: int, end_b: int, span: str) -> bool:
    if not span:
        return False
    try:
        _check_polarity_preservation(raw_bytes, start_b, end_b, span)
    except SafeStop:
        return False
    return True


def _span_tuple(decoded: str, start: int, end: int) -> tuple[int, int, str]:
    span = decoded[start:end]
    start_b = len(decoded[:start].encode("utf-8"))
    end_b = start_b + len(span.encode("utf-8"))
    return start_b, end_b, span


def _snap_window_for_cluster(
    decoded: str,
    raw_bytes: bytes,
    line_start: int,
    line_end: int,
    cluster_start: int,
    cluster_end: int,
    max_chars: int,
) -> tuple[int, int] | None:
    """Choose a clean [start, end] <= max_chars inside a long single line preserving polarity & JSON structure."""
    if cluster_end - cluster_start > max_chars:
        cluster_end = cluster_start + max_chars
    # Reserve room after cluster_end so trailing message/value (and line end for trailing errors) stays intact.
    tail_room = min(line_end - cluster_end, max(80, max_chars // 2))
    win_start = max(line_start, cluster_end + tail_room - max_chars)
    preferred_start = win_start
    # Snap start forward onto a clean object/record boundary before cluster_start (leaving >= tail_room after cluster_end).
    max_snap = max(0, min(cluster_start - preferred_start, 64))
    for idx in range(preferred_start, preferred_start + max_snap):
        if (
            idx > line_start
            and decoded[idx - 1] in '{[," \t\n:'
            and idx + max_chars >= cluster_end + min(tail_room, 48)
        ):
            win_start = idx
            break
    win_end = min(line_end, win_start + max_chars)

    # Ensure start & end never split ASCII alnum tokens.
    while (
        win_start < cluster_start
        and win_start > 0
        and decoded[win_start - 1].isascii()
        and decoded[win_start - 1].isalnum()
        and decoded[win_start].isascii()
        and decoded[win_start].isalnum()
    ):
        win_start += 1
    while (
        win_end > cluster_end
        and win_end < len(decoded)
        and decoded[win_end - 1].isascii()
        and decoded[win_end - 1].isalnum()
        and decoded[win_end].isascii()
        and decoded[win_end].isalnum()
    ):
        win_end -= 1

    sb, eb, sp = _span_tuple(decoded, win_start, win_end)
    if _is_polarity_safe(raw_bytes, sb, eb, sp):
        return win_start, win_end

    # Try adjusting boundaries along punctuation/delimiters so _check_polarity_preservation succeeds.
    candidate_starts = [win_start] + [
        idx for idx in range(win_start + 1, cluster_start + 1) if decoded[idx - 1] in ".!?;:\r\n。！？,{"
    ]
    candidate_ends = [win_end] + [
        idx
        for idx in range(win_end, cluster_end - 1, -1)
        if idx == len(decoded) or decoded[idx - 1] in ".!?;:\r\n。！？,}])"
    ]
    for s in candidate_starts[:16]:
        for e in candidate_ends[:16]:
            if e <= s or e - s > max_chars:
                continue
            sb, eb, sp = _span_tuple(decoded, s, e)
            if _is_polarity_safe(raw_bytes, sb, eb, sp):
                return s, e
    return None


def _select_evenly(items: list[tuple[int, int]], limit: int) -> list[tuple[int, int]]:
    if len(items) <= limit:
        return items
    if limit <= 1:
        return [items[-1]]
    indices = sorted({round(i * (len(items) - 1) / (limit - 1)) for i in range(limit)})
    return [items[idx] for idx in indices]


def _verbatim_raw_spans(
    raw_bytes: bytes,
    max_chars: int,
    *,
    max_spans: int = 4,
    compact_head_chars: int | None = None,
) -> tuple[tuple[int, int, str], ...]:
    """Select exact verbatim UTF-8 byte spans inside unmodified raw bytes, keeping guard/error clauses intact."""
    decoded = raw_bytes.decode("utf-8", "strict")
    if len(decoded) <= max_chars:
        return ((0, len(raw_bytes), decoded),)

    guard_cap = min(max_chars, max(96, compact_head_chars)) if compact_head_chars is not None else max_chars
    matches = list(_NEGATION_GUARD_RE.finditer(decoded))
    if matches:
        clusters: list[tuple[int, int]] = []
        for match in matches:
            ms, me = match.start(), match.end()
            if clusters and me - clusters[-1][0] <= guard_cap - 32:
                clusters[-1] = (clusters[-1][0], me)
            else:
                clusters.append((ms, me))

        selected_windows: list[tuple[int, int]] = []
        for c_start, c_end in _select_evenly(clusters, max_spans):
            line_start = decoded.rfind("\n", 0, c_start) + 1
            line_end = decoded.find("\n", c_end)
            if line_end == -1:
                line_end = len(decoded)
            win: tuple[int, int] | None = None
            if 0 < line_end - line_start <= max_chars:
                sb, eb, sp = _span_tuple(decoded, line_start, line_end)
                if _is_polarity_safe(raw_bytes, sb, eb, sp):
                    win = (line_start, line_end)
            if win is None:
                win = _snap_window_for_cluster(decoded, raw_bytes, line_start, line_end, c_start, c_end, guard_cap)
            if win is not None:
                ws, we = win
                if not any(ps <= ws and we <= pe for ps, pe in selected_windows):
                    selected_windows.append((ws, we))

        if selected_windows:
            return tuple(_span_tuple(decoded, ws, we) for ws, we in selected_windows)

    head_cap = min(max_chars, compact_head_chars) if compact_head_chars is not None else max_chars
    head_end = min(len(decoded), head_cap)
    # Try snapping compact head cleanly onto JSON element or newline boundary when compact_head_chars is set.
    if compact_head_chars is not None and head_end < len(decoded):
        for delim in ("}", "\n", ","):
            pos = decoded.find(delim, 1, max_chars + 1)
            if pos != -1:
                head_end = pos + 1
                break
    text = decoded[:head_end]
    if (
        head_end < len(decoded)
        and text
        and decoded[head_end].isascii()
        and decoded[head_end].isalnum()
        and text[-1].isascii()
        and text[-1].isalnum()
    ):
        cut = len(text)
        while cut > 0 and text[cut - 1].isascii() and text[cut - 1].isalnum():
            cut -= 1
        trimmed = text[:cut].rstrip()
        if trimmed:
            text = trimmed
    return ((0, len(text.encode("utf-8")), text),)


def _verbatim_raw_span(raw_bytes: bytes, max_chars: int) -> tuple[int, int, str]:
    """Select the primary verbatim UTF-8 byte span inside unmodified raw bytes."""
    return _verbatim_raw_spans(raw_bytes, max_chars)[0]


class _HybridSummarizer:
    def __init__(self, spans_by_ref: dict[str, tuple[tuple[int, int, str], ...]]) -> None:
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
                text=span_text,
                quote=span_text,
                artifact_ref=ref.artifact_ref,
                source_hash=ref.sha256,
                observed_at=ref.observed_at,
                start_byte=start_b,
                end_byte=end_b,
            )
            for ref in refs
            for start_b, end_b, span_text in self._spans[ref.artifact_ref]
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

        spans_by_ref: dict[str, tuple[tuple[int, int, str], ...]] = {}
        traces: list[SourceProjectionTrace] = []
        written_projections: list[str] = []
        projection_entries: list[dict[str, str]] = []
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
                    projection_entries.append(
                        {
                            "artifactRef": raw_ref.artifact_ref,
                            "projectionRef": proj_ref,
                            "projectionSha256": proj_sha,
                            "text": opt_src.body.decode("utf-8", "strict"),
                        }
                    )
                    any_optimized = True
                spans = _verbatim_raw_spans(
                    raw_bytes,
                    max_excerpt_chars,
                    compact_head_chars=72 if proj_ref is not None else None,
                )
                spans_by_ref[raw_ref.artifact_ref] = spans
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
                        preserved_start_byte=spans[0][0],
                        preserved_end_byte=spans[-1][1],
                        outcome=receipt.outcome,
                        before_tokens=receipt.before_tokens,
                        after_tokens=receipt.after_tokens,
                        reason_code=receipt.reason_code,
                    )
                )

            summarizer = _HybridSummarizer(spans_by_ref)
            tool_pairs = verify_tool_pairs(events)
            baseline_candidate = MockExcerptSummarizer()(request, raw_refs, self._service.artifacts, max_excerpt_chars)
            hybrid_candidate = summarizer(request, raw_refs, self._service.artifacts, max_excerpt_chars)
            _, baseline_full_payload = _serialize(
                request, raw_refs, baseline_candidate, tool_pairs, len(request.memory)
            )
            base_full_tokens = self._service.tokenizer.count_tokens(
                baseline_full_payload,
                model_id=request.budget.model_id,
                tokenizer_id=request.budget.tokenizer_id,
            )
            applied_projections: tuple[dict[str, str], ...] = ()
            if projection_entries:
                _, opt_full_payload = _serialize(
                    request,
                    raw_refs,
                    hybrid_candidate,
                    tool_pairs,
                    len(request.memory),
                    optimized_projections=tuple(projection_entries),
                )
                opt_full_tokens = self._service.tokenizer.count_tokens(
                    opt_full_payload,
                    model_id=request.budget.model_id,
                    tokenizer_id=request.budget.tokenizer_id,
                )
                if opt_full_tokens < base_full_tokens:
                    applied_projections = tuple(projection_entries)
                else:
                    # Re-compute claims without compact_head_chars truncation if projections weren't applied.
                    for (src, cls), raw_ref in zip(sources, raw_refs, strict=True):
                        del src, cls
                        spans_by_ref[raw_ref.artifact_ref] = _verbatim_raw_spans(
                            raw_by_ref[raw_ref.artifact_ref], max_excerpt_chars
                        )

            plan = self._service.compact(
                request,
                raw_sources,
                events=events,
                summarizer=summarizer,
                max_excerpt_chars=max_excerpt_chars,
                _existing_refs=raw_refs,
                _optimized_projections=applied_projections,
            )
            _, matched_base_payload = _serialize(request, raw_refs, baseline_candidate, tool_pairs, len(plan.memory))
            payload_tokens_before = self._service.tokenizer.count_tokens(
                matched_base_payload,
                model_id=request.budget.model_id,
                tokenizer_id=request.budget.tokenizer_id,
            )
            payload_tokens_after = self._service.tokenizer.count_tokens(
                plan.model_payload,
                model_id=request.budget.model_id,
                tokenizer_id=request.budget.tokenizer_id,
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
            payload_tokens_before=payload_tokens_before,
            payload_tokens_after=payload_tokens_after,
        )
        return replace(plan, optimization_receipt=agg_receipt)
