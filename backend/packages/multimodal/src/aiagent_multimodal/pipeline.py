"""Bounded parse -> extract -> independently verify pipeline.

This module is deliberately unaware of HTTP, PostgreSQL and model providers. It
takes an already-admitted immutable artifact, runs the fixed parser registry,
extracts typed candidates, re-reads the source with the verifier, and returns a
contract-validated outcome plus an authorized citation projection.

Isolation note: the parser executor is injected. The in-process executor is a
development/test convenience and must not be enabled in production; production
must supply a worker executor that enforces the no-network/non-root/read-only
confinement (15-01 MMI-014).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from .contracts import MmiOutcomeV1, MmiRequestV1, MmiSourceLocator
from .detection import DeclaredInput, detect
from .errors import MmiError
from .extraction.fields import extract_candidates
from .limits import MmiLimits
from .parser_registry import DEFAULT_REGISTRY, ParserRegistry
from .parsers.base import Document, ParseInput
from .verification import VerificationResult, verify_candidates

_TERMINAL_WARNING: Final = "OUTCOME_PARTIAL"


@dataclass(frozen=True, slots=True)
class ProcessResult:
    outcome: dict[str, Any]
    citations: list[dict[str, Any]]
    document: Document
    verification: VerificationResult


class ParserExecutor(Protocol):
    """Runs one bounded parse. Implementations must fail closed, never downgrade."""

    def execute(self, inp: ParseInput, deadline_monotonic: float) -> Document: ...


class InProcessParserExecutor:
    """Development-only executor. Explicit opt-in; never a production fallback."""

    def __init__(self, registry: ParserRegistry | None = None) -> None:
        self.registry = registry or DEFAULT_REGISTRY

    def execute(self, inp: ParseInput, deadline_monotonic: float) -> Document:
        if time.monotonic() > deadline_monotonic:
            raise MmiError("PARSE_TIMEOUT", "parse deadline expired before start")
        return self.registry.parse(inp)


@dataclass
class Pipeline:
    limits: MmiLimits
    executor: ParserExecutor
    registry: ParserRegistry = field(default_factory=lambda: DEFAULT_REGISTRY)

    def detect(self, raw: bytes, *, file_name: str, declared_mime: str, format_hint: str | None = None) -> Any:
        if len(raw) > self.limits.max_file_bytes:
            raise MmiError("MAX_INPUT", "input exceeds file byte limit")
        return detect(
            raw,
            DeclaredInput(file_name=file_name, declared_mime=declared_mime, format_hint=format_hint),
            self.limits,
        )

    def process(
        self,
        *,
        source_artifact_id: str,
        source_sha256: str,
        raw: bytes,
        request: MmiRequestV1,
        detection: Any,
        run_ocr: bool = False,
        deadline_monotonic: float | None = None,
        owner_revoked: bool = False,
    ) -> ProcessResult:
        if owner_revoked:
            raise MmiError("OWNER_REVOKED", "owner revoked access before parse")
        if hashlib.sha256(raw).hexdigest() != source_sha256:
            raise MmiError("SOURCE_MISMATCH", "artifact bytes do not match the recorded digest")
        deadline = deadline_monotonic if deadline_monotonic is not None else time.monotonic() + 60.0
        inp = ParseInput(
            source_artifact_id=source_artifact_id,
            source_sha256=source_sha256,
            format=detection.format,
            data=raw,
            parser_revision="aiagent-multimodal/1.0.0",
            build_digest=self.registry.build_digest,
            policy_revision="mmi/1",
            limits=self.limits,
            options={"run_ocr": bool(run_ocr)},
            encoding=detection.encoding,
            delimiter=detection.delimiter,
            deadline_monotonic=deadline,
        )
        worker_document = self.executor.execute(inp, deadline)
        if worker_document.source_sha256 != source_sha256 or worker_document.source_artifact_id != source_artifact_id:
            raise MmiError("SOURCE_MISMATCH", "parser returned a document for a different source")
        # The worker output is untrusted. Re-read the immutable bytes through the verifier path and
        # derive candidates from that independent document, not from the worker's copy. Without an
        # executor/jail this is still an in-process re-read, so production admission stays closed.
        document = self.registry.parse(inp)
        if document.source_sha256 != source_sha256 or document.source_artifact_id != source_artifact_id:
            raise MmiError("SOURCE_MISMATCH", "verifier returned a document for a different source")
        extraction = extract_candidates(
            document, request.requested_fields, locale=request.preferred_locale, limits=self.limits
        )
        verification = verify_candidates(
            document, extraction.candidates, raw, owner_revoked=owner_revoked, locale=request.preferred_locale
        )
        candidates = verification.candidates
        requested = tuple(request.requested_fields) or ("amount",)
        present = {c.field_key for c in candidates}
        missing = tuple(field for field in requested if _canonical_field(field) not in present)
        unresolved = {
            c.field_key for c in candidates if c.value_state in {"unknown", "estimated"} or c.verification != "anchored"
        }
        warnings = list(extraction.warnings)
        warnings.extend(verification.warnings)
        if document.partial:
            warnings.append("DOCUMENT_PARTIAL")
        warnings.extend(document.warnings)
        if verification.anchored == 0 and candidates:
            warnings.append("NO_ANCHORED_CANDIDATES")
        if missing:
            warnings.append("MISSING_FIELDS")
        if candidates and not missing and not unresolved and not warnings and not document.partial:
            status = "complete"
        elif candidates:
            status = "partial"
        else:
            status = "unknown"
        if status != "complete":
            warnings.append(_TERMINAL_WARNING)
        outcome = MmiOutcomeV1(
            source_artifact_id=source_artifact_id,
            status=status,
            candidates=candidates,
            missing_fields=missing,
            warning_codes=tuple(dict.fromkeys(warnings)),
            derived_artifact_id=None,
            parser_build_digest=self.registry.build_digest,
            processed_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        citations = [_citation(c, detection.format, source_artifact_id, source_sha256) for c in candidates]
        return ProcessResult(outcome.to_dict(), citations, document, verification)


def _canonical_field(name: str) -> str:
    lowered = name.strip().lower().replace(" ", "_").replace("-", "_")
    return {"price": "amount", "total": "amount", "effectiveprice": "effective_price"}.get(lowered, lowered)


def _citation(candidate: Any, fmt: str, artifact_id: str, sha: str) -> dict[str, Any]:
    locator: MmiSourceLocator | None = candidate.locator
    return {
        "fieldKey": candidate.field_key,
        "rawText": candidate.raw_text,
        "valueState": candidate.value_state,
        "verification": candidate.verification,
        "normalized": candidate.normalized,
        "conditions": list(candidate.conditions),
        "missingReason": candidate.missing_reason,
        "extractionMethod": candidate.extraction_method,
        "recognitionConfidenceRaw": candidate.recognition_confidence_raw,
        "locator": locator.to_dict() if locator is not None else None,
        "source": {"artifactId": artifact_id, "sha256": sha, "format": fmt},
    }


def citation_label(citation: Mapping[str, Any]) -> str:
    """Human display label. Values are data; callers must escape them in HTML."""
    loc = citation.get("locator") or {}
    kind = loc.get("kind")
    if kind == "pdf":
        return f"page {loc.get('page1')}"
    if kind == "sheet":
        return f"{loc.get('sheetName')}!{loc.get('cellA1')}"
    if kind == "csv":
        return f"record {loc.get('record1')}, field {loc.get('field1')}"
    if kind == "office":
        if loc.get("format") == "pptx":
            return f"slide {loc.get('slide1')}"
        return f"paragraph {loc.get('paragraph1')}"
    if kind == "image":
        return "image region"
    return "document"


def citations_from_outcome(outcome: Mapping[str, Any], *, fmt: str, artifact_id: str, sha: str) -> list[dict[str, Any]]:
    from .contracts import MmiCandidate

    return [_citation(MmiCandidate.from_dict(c), fmt, artifact_id, sha) for c in outcome.get("candidates", [])]
