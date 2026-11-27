"""Independent source verification (15-01 §13).

The verifier does not trust the extractor's own model. It re-reads the immutable
source bytes (CSV byte spans) or the parsed source coordinates, recomputes the
normalized value, and only then marks a candidate `anchored`. A value that
cannot be re-read stays `unverified`; a value whose source now disagrees is
`contradicted` and is never promoted to evidence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from hashlib import sha256

from .contracts import MmiCandidate
from .errors import MmiError
from .extraction.normalization import parse_money, tax_condition
from .parsers.base import Document, TextBlock

_MAX_REASON = 500
_DATE_RE = re.compile(r"(?P<y>20\d{2})\s*[-/年]\s*(?P<m>\d{1,2})\s*[-/月]\s*(?P<d>\d{1,2})\s*日?(?!\d)")


@dataclass(frozen=True, slots=True)
class VerificationResult:
    candidates: tuple[MmiCandidate, ...]
    anchored: int
    unverified: int
    contradicted: int
    warnings: tuple[str, ...]


def verify_candidates(
    document: Document,
    candidates: Iterable[MmiCandidate],
    raw_bytes: bytes,
    *,
    owner_revoked: bool = False,
    locale: str | None = None,
) -> VerificationResult:
    if owner_revoked:
        raise MmiError("OWNER_REVOKED", "source owner revoked access during verification")
    if sha256(raw_bytes).hexdigest() != document.source_sha256:
        raise MmiError("SOURCE_MISMATCH", "source bytes no longer match the parsed artifact")
    out: list[MmiCandidate] = []
    counts = {"anchored": 0, "unverified": 0, "contradicted": 0}
    for candidate in candidates:
        state, reason = _verify_one(document, candidate, raw_bytes, locale)
        metadata = dict(candidate.provenance)
        metadata["verificationReason"] = reason[:_MAX_REASON]
        metadata["verifierVersion"] = "1.0.0"
        verified = replace(candidate, verification=state, provenance=metadata)
        counts[state] += 1
        out.append(verified)
    warnings: tuple[str, ...] = ()
    if counts["contradicted"]:
        warnings = ("SOURCE_CONTRADICTION",)
    return VerificationResult(tuple(out), counts["anchored"], counts["unverified"], counts["contradicted"], warnings)


def _verify_one(document: Document, candidate: MmiCandidate, raw_bytes: bytes, locale: str | None) -> tuple[str, str]:
    locator = candidate.locator
    if locator is None:
        return "unverified", "candidate has no locator"
    if candidate.extraction_method == "ocr" or "ocr_unverified" in candidate.conditions:
        # OCR is model output, not source text. Re-reading the same OCR result through the parser
        # would be self-verification; keep it unverified until a separate pixel-level check exists.
        return "unverified", "OCR text is not independently verifiable"
    if locator.artifact_id != document.source_artifact_id:
        return "contradicted", "locator belongs to a different artifact"
    if locator.source_sha256 != document.source_sha256:
        return "contradicted", "locator source digest differs"
    if locator.kind == "csv":
        return _verify_csv(document, candidate, raw_bytes, locale)
    if locator.kind == "sheet":
        return _verify_sheet(document, candidate, locale)
    if locator.kind == "office":
        return _verify_office(document, candidate, locale)
    if locator.kind == "pdf":
        return _verify_pdf(document, candidate, locale)
    if locator.kind == "image":
        return _verify_image(document, candidate)
    return "unverified", "unsupported locator kind"


def _verify_csv(document: Document, candidate: MmiCandidate, raw_bytes: bytes, locale: str | None) -> tuple[str, str]:
    locator = candidate.locator
    assert locator is not None
    try:
        start = int(locator.byte_start or "")
        end = int(locator.byte_end or "")
    except ValueError:
        return "contradicted", "CSV byte span is not numeric"
    if start < 0 or end < start or end > len(raw_bytes):
        return "contradicted", "CSV byte span is outside the source"
    encoding = str(document.metadata.get("encoding", "utf-8"))
    try:
        text = raw_bytes[start:end].decode(encoding, "strict")
    except (UnicodeDecodeError, LookupError):
        return "contradicted", "CSV byte span does not decode in the recorded encoding"
    return _compare_text(candidate, text, locale)


def _verify_sheet(document: Document, candidate: MmiCandidate, locale: str | None) -> tuple[str, str]:
    locator = candidate.locator
    assert locator is not None
    for table in document.tables():
        cell = table.cell(a1=locator.cell_a1)
        if cell is None:
            continue
        cell_locator = cell.locator
        if cell_locator.kind != "sheet" or cell_locator.sheet_name != locator.sheet_name:
            continue
        if cell.value_state in {"unknown", "estimated"}:
            return "unverified", f"source cell is {cell.value_state}; it was not independently computed"
        if cell.text is None:
            if cell.formula:
                return "unverified", "formula cell has no cached value; it was not evaluated"
            return "contradicted", "source cell is empty"
        return _compare_text(candidate, cell.text, locale)
    return "unverified", "source cell is not present in the parsed sheet"


def _verify_office(document: Document, candidate: MmiCandidate, locale: str | None) -> tuple[str, str]:
    locator = candidate.locator
    assert locator is not None
    for block in document.blocks():
        other = block.locator
        if other.kind != "office" or other.part_name != locator.part_name:
            continue
        if locator.paragraph1 is not None and other.paragraph1 == locator.paragraph1:
            return _compare_text(candidate, block.text, locale)
    for table in document.tables():
        for cell in table.cells:
            other = cell.locator
            if other.kind != "office" or other.part_name != locator.part_name:
                continue
            if (locator.table1, locator.row1, locator.column1) == (other.table1, other.row1, other.column1):
                if cell.text is None:
                    return "contradicted", "source table cell is empty"
                return _compare_text(candidate, cell.text, locale)
    return "unverified", "source paragraph/cell is not present"


def _verify_pdf(document: Document, candidate: MmiCandidate, locale: str | None) -> tuple[str, str]:
    locator = candidate.locator
    assert locator is not None
    section = next(
        (s for s in document.sections if s.locator and s.locator.kind == "pdf" and s.locator.page1 == locator.page1),
        None,
    )
    if section is None:
        return "unverified", "source page is not present"
    blocks = section.blocks
    if locator.normalized_bbox is not None:
        x0, y0, x1, y1 = locator.normalized_bbox
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            return "contradicted", "PDF normalized bounding box is invalid"
        blocks = [block for block in blocks if _bbox_overlaps(block.locator.normalized_bbox, locator.normalized_bbox)]
        if not blocks:
            return "unverified", "no source text lies inside the candidate bounding box"
    text = "\n".join(block.text for block in blocks)
    if not text.strip():
        return "unverified", "page has no native text to verify against"
    return _compare_text(candidate, text, locale)


def _verify_image(document: Document, candidate: MmiCandidate) -> tuple[str, str]:
    locator = candidate.locator
    assert locator is not None
    if locator.source_width_px is None or locator.source_height_px is None:
        return "contradicted", "image locator has no source dimensions"
    section = next((s for s in document.sections if s.kind == "image"), None)
    if section is None or not section.images:
        return "unverified", "image metadata is missing"
    image = section.images[0]
    if (image.width_px, image.height_px) != (locator.source_width_px, locator.source_height_px):
        return "contradicted", "image dimensions differ from the parsed source"
    return "unverified", "pixel content cannot be re-read independently; OCR remains unverified"


def _bbox_overlaps(left: tuple[float, float, float, float] | None, right: tuple[float, float, float, float]) -> bool:
    if left is None:
        return False
    return not (left[2] <= right[0] or left[0] >= right[2] or left[3] <= right[1] or left[1] >= right[3])


def _compare_text(candidate: MmiCandidate, source_text: str, locale: str | None) -> tuple[str, str]:
    raw = (candidate.raw_text or "").strip()
    if not raw:
        return "unverified", "candidate has no raw text"
    if raw not in source_text:
        return "contradicted", "raw text is not present at the source coordinate"
    replayed = _replay(candidate, source_text, locale)
    if replayed is False:
        return "contradicted", "normalized value does not match a fresh parse of the raw text"
    if replayed is None:
        # Raw presence is not semantic verification. Without a type-specific replay,
        # do not promote the candidate to evidence.
        return "unverified", "normalized value cannot be independently replayed for this field"
    return "anchored", "raw text and normalized value replay at the source coordinate"


def _replay(candidate: MmiCandidate, source_text: str, locale: str | None) -> bool | None:
    raw = candidate.raw_text or ""
    normalized = candidate.normalized or {}
    if candidate.value_state in {"unknown", "estimated"}:
        return None
    if candidate.field_key in {"amount", "effective_price"} and isinstance(normalized, dict):
        parsed = parse_money(raw, locale=locale, near_text=source_text)
        if parsed.amount is None or parsed.currency is None:
            return None
        if parsed.amount != normalized.get("amount") or parsed.currency != normalized.get("currency"):
            return False
        if tax_condition(source_text) != normalized.get("tax"):
            # The candidate's tax context may come from a different block than the verifier's
            # page text. That is not proof of a wrong amount; leave it unverified.
            return None
        return True
    if candidate.field_key == "date" and isinstance(normalized, dict):
        match = _DATE_RE.search(raw)
        if match is None:
            return None
        try:
            value = date(int(match.group("y")), int(match.group("m")), int(match.group("d"))).isoformat()
        except ValueError:
            return False
        return value == normalized.get("value")
    return None


def block_text(document: Document) -> str:
    return "\n".join(b.text for b in document.blocks())


def _unused(block: TextBlock) -> None:  # pragma: no cover - typing helper
    _ = block
