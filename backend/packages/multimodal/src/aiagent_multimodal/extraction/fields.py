"""Deterministic field candidates from typed document text (15-01 §12).

A candidate always carries its source locator and remains `unverified` until
the independent verifier re-reads the source. The extractor never fills an
unreadable amount with a guess, never evaluates a spreadsheet formula and never
converts a cached formula error to zero.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final

from ..contracts import MmiCandidate, MmiSourceLocator
from ..limits import MmiLimits
from ..parsers.base import Document
from .normalization import NORMALIZER_VERSION, MoneyParse, parse_money, tax_condition

_KNOWN_FIELDS: Final[frozenset[str]] = frozenset(
    {"amount", "currency", "tax", "effective_price", "date", "quantity", "unit"}
)
_ALIASES: Final[dict[str, str]] = {"price": "amount", "total": "amount", "effectiveprice": "effective_price"}
_DATE = re.compile(r"(?P<y>20\d{2})\s*[-/年]\s*(?P<m>\d{1,2})\s*[-/月]\s*(?P<d>\d{1,2})\s*日?")
_QUANTITY = re.compile(
    r"(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<unit>個|点|枚|箱|袋|本|kg|g|ml|L|パック)"
)
_AMOUNT = re.compile(r"[¥￥$€£]?\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,3})?\s*(?:円|ドル|ユーロ)?")
_MAX_CANDIDATES = 250
_MAX_SOURCES = 5_000


@dataclass(frozen=True, slots=True)
class TextSource:
    text: str
    locator: MmiSourceLocator
    method: str
    confidence: float | None = None
    hidden: bool = False


@dataclass(frozen=True, slots=True)
class Extraction:
    candidates: tuple[MmiCandidate, ...]
    warnings: tuple[str, ...]
    missing_fields: tuple[str, ...]


def extract_candidates(
    document: Document,
    requested_fields: Iterable[str] = (),
    *,
    locale: str | None = None,
    limits: MmiLimits | None = None,
) -> Extraction:
    fields = [_canonical(name) for name in requested_fields] or ["amount"]
    sources, truncated = _sources(document)
    warnings: list[str] = []
    if truncated:
        warnings.append("EXTRACTION_TRUNCATED")
    out: list[MmiCandidate] = []
    seen: set[str] = set()

    for source in sources:
        tax = tax_condition(source.text)
        money_parses = _money_matches(source.text, locale)
        if "amount" in fields:
            for raw, parsed in money_parses:
                candidate = _money_candidate(document, source, raw, parsed, tax, fields=("amount",))
                _add(out, seen, candidate, "amount")
        if "currency" in fields and money_parses:
            raw, parsed = money_parses[0]
            if parsed.currency is None:
                _add(out, seen, _unknown(document, source, "currency", parsed.reason or "UNKNOWN_CURRENCY"), "currency")
            else:
                _add(
                    out,
                    seen,
                    _text_candidate(
                        document,
                        source,
                        "currency",
                        parsed.currency,
                        {"schema": "mmi.currency/1", "code": parsed.currency},
                    ),
                    "currency",
                )
        if "tax" in fields:
            conditions = (f"tax:{tax}",)
            if tax == "unknown":
                _add(
                    out,
                    seen,
                    _unknown(document, source, "tax", "UNKNOWN_TAX_TREATMENT", conditions=conditions),
                    "tax",
                )
            else:
                _add(
                    out,
                    seen,
                    _text_candidate(
                        document,
                        source,
                        "tax",
                        tax,
                        {"schema": "mmi.tax/1", "value": tax},
                        conditions=conditions,
                    ),
                    "tax",
                )
        if "amount" in fields or "effective_price" in fields:
            for _raw, parsed in money_parses:
                if "effective_price" in fields:
                    _add(
                        out,
                        seen,
                        _effective_price(document, source, parsed, tax),
                        "effective_price",
                    )
    if "date" in fields:
        for source in sources:
            for date_match in _DATE.finditer(source.text):
                try:
                    value = date(
                        int(date_match.group("y")), int(date_match.group("m")), int(date_match.group("d"))
                    ).isoformat()
                except ValueError:
                    _add(out, seen, _unknown(document, source, "date", "INVALID_DATE"), "date")
                    continue
                _add(
                    out,
                    seen,
                    _text_candidate(
                        document,
                        source,
                        "date",
                        date_match.group(0),
                        {"schema": "mmi.date/1", "value": value},
                    ),
                    "date",
                )
    if "quantity" in fields or "unit" in fields:
        for source in sources:
            for quantity_match in _QUANTITY.finditer(source.text):
                if "quantity" in fields:
                    _add(
                        out,
                        seen,
                        _text_candidate(
                            document,
                            source,
                            "quantity",
                            quantity_match.group("num"),
                            {
                                "schema": "mmi.quantity/1",
                                "value": quantity_match.group("num").replace(",", ""),
                            },
                        ),
                        "quantity",
                    )
                if "unit" in fields:
                    _add(
                        out,
                        seen,
                        _text_candidate(
                            document,
                            source,
                            "unit",
                            quantity_match.group("unit"),
                            {"schema": "mmi.unit/1", "value": quantity_match.group("unit")},
                        ),
                        "unit",
                    )
    generic = [f for f in fields if f not in _KNOWN_FIELDS]
    for field in generic:
        _generic_field(document, sources, field, out, seen)

    missing = tuple(field for field in dict.fromkeys(fields) if not any(c.field_key == field for c in out))
    if len(out) > _MAX_CANDIDATES:
        out = out[:_MAX_CANDIDATES]
        warnings.append("CANDIDATE_LIMIT_REACHED")
    return Extraction(tuple(out), tuple(dict.fromkeys(warnings)), missing)


def _sources(document: Document) -> tuple[list[TextSource], bool]:
    out: list[TextSource] = []
    truncated = False
    for block in document.blocks():
        if block.text:
            out.append(
                TextSource(
                    text=block.text,
                    locator=block.locator,
                    method=block.extraction_method,
                    confidence=block.recognition_confidence_raw,
                    hidden=bool(block.locator.hidden_state and block.locator.hidden_state != "visible"),
                )
            )
    for cell in document.cells():
        if cell.text:
            hidden = cell.hidden or (cell.locator.hidden_state not in {None, "visible"})
            out.append(
                TextSource(
                    text=cell.text,
                    locator=cell.locator,
                    method=_cell_method(document.format),
                    hidden=hidden,
                )
            )
        if len(out) >= _MAX_SOURCES:
            truncated = True
            break
    if len(out) > _MAX_SOURCES:
        return out[:_MAX_SOURCES], True
    return out, truncated


def _cell_method(fmt: str) -> str:
    return {"csv": "csv", "tsv": "csv", "xlsx": "xlsx", "docx": "ooxml", "pptx": "ooxml", "pdf": "native"}.get(
        fmt, "native"
    )


def _money_matches(text: str, locale: str | None) -> list[tuple[str, MoneyParse]]:
    out: list[tuple[str, MoneyParse]] = []
    for match in _AMOUNT.finditer(text):
        raw = match.group(0).strip()
        if not raw or not any(ch.isdigit() for ch in raw):
            continue
        out.append((raw, parse_money(raw, locale=locale, near_text=text)))
    return out


def _money_candidate(
    document: Document,
    source: TextSource,
    raw: str,
    parsed: MoneyParse,
    tax: str,
    *,
    fields: tuple[str, ...],
) -> MmiCandidate:
    conditions = [f"tax:{tax}"]
    if source.hidden:
        conditions.append("hidden_source")
    if source.method == "ocr" and source.confidence is not None and source.confidence < 70:
        conditions.append("low_ocr_confidence")
    if parsed.reason is not None:
        return _unknown(document, source, "amount", parsed.reason, raw=raw, conditions=tuple(conditions))
    normalized = {
        "schema": "mmi.money/1",
        "amount": parsed.amount,
        "currency": parsed.currency,
        "tax": tax,
    }
    return _build(
        document,
        source,
        "amount",
        raw,
        "estimated" if source.method == "ocr" and (source.confidence or 0) < 70 else "observed",
        normalized,
        tuple(conditions),
        None,
    )


def _effective_price(document: Document, source: TextSource, parsed: MoneyParse, tax: str) -> MmiCandidate:
    conditions = [f"tax:{tax}"]
    if parsed.reason is not None or parsed.amount is None:
        return _unknown(
            document, source, "effective_price", parsed.reason or "AMOUNT_UNKNOWN", conditions=tuple(conditions)
        )
    if tax == "unknown":
        return _unknown(document, source, "effective_price", "UNKNOWN_TAX_TREATMENT", conditions=tuple(conditions))
    if tax == "excluded":
        return _unknown(document, source, "effective_price", "TAX_RATE_UNKNOWN", conditions=tuple(conditions))
    return _build(
        document,
        source,
        "effective_price",
        parsed.raw,
        "derived",
        {"schema": "mmi.money/1", "amount": parsed.amount, "currency": parsed.currency, "tax": "included"},
        tuple(conditions),
        None,
    )


def _generic_field(
    document: Document, sources: list[TextSource], field: str, out: list[MmiCandidate], seen: set[str]
) -> None:
    pattern = re.compile(rf"{re.escape(field)}\s*[:：]\s*(?P<value>[^\n\r]{{1,200}})", re.IGNORECASE)
    for source in sources:
        match = pattern.search(source.text)
        if not match:
            continue
        value = match.group("value").strip()
        if not value:
            _add(out, seen, _unknown(document, source, field, "EMPTY_VALUE"), field)
        else:
            _add(
                out,
                seen,
                _text_candidate(document, source, field, value, {"schema": "mmi.text/1", "value": value}),
                field,
            )
    for table in document.tables():
        headers = [h.strip().casefold() if h else "" for h in table.headers]
        if field.casefold() not in headers:
            continue
        column = headers.index(field.casefold()) + 1
        for cell in table.cells:
            if cell.column != column or cell.row == 1:
                continue
            source = TextSource(
                text=cell.text or "",
                locator=cell.locator,
                method=_cell_method(document.format),
                hidden=cell.hidden,
            )
            if not cell.text:
                _add(out, seen, _unknown(document, source, field, "EMPTY_VALUE"), field)
            else:
                _add(
                    out,
                    seen,
                    _text_candidate(document, source, field, cell.text, {"schema": "mmi.text/1", "value": cell.text}),
                    field,
                )


def _text_candidate(
    document: Document,
    source: TextSource,
    field: str,
    raw: str,
    normalized: Mapping[str, object],
    *,
    conditions: tuple[str, ...] = (),
) -> MmiCandidate:
    return _build(document, source, field, raw, "observed", normalized, _conditions(source, conditions), None)


def _unknown(
    document: Document,
    source: TextSource,
    field: str,
    reason: str,
    *,
    raw: str | None = None,
    conditions: tuple[str, ...] = (),
) -> MmiCandidate:
    return _build(
        document,
        source,
        field,
        raw,
        "unknown",
        None,
        _conditions(source, conditions),
        reason,
    )


def _conditions(source: TextSource, extra: tuple[str, ...]) -> tuple[str, ...]:
    out = list(extra)
    if source.hidden and "hidden_source" not in out:
        out.append("hidden_source")
    if source.method == "ocr":
        out.append("ocr_unverified")
        if source.confidence is not None and source.confidence < 70:
            out.append("low_ocr_confidence")
    return tuple(dict.fromkeys(out))


def _build(
    document: Document,
    source: TextSource,
    field: str,
    raw: str | None,
    value_state: str,
    normalized: Mapping[str, object] | None,
    conditions: tuple[str, ...],
    missing_reason: str | None,
) -> MmiCandidate:
    return MmiCandidate(
        field_key=field,
        raw_text=raw,
        value_state=value_state,
        normalized=normalized,
        conditions=conditions,
        missing_reason=missing_reason,
        locator=source.locator,
        extraction_method=source.method,
        recognition_confidence_raw=source.confidence,
        verification="unverified",
        provenance={
            "artifactId": document.source_artifact_id,
            "sourceSha256": document.source_sha256,
            "parserRevision": document.parser_revision,
            "parserBuildDigest": document.build_digest,
            "normalizerVersion": NORMALIZER_VERSION,
            "extractionMethod": source.method,
        },
    )


def _add(out: list[MmiCandidate], seen: set[str], candidate: MmiCandidate, field: str) -> None:
    key = json.dumps(
        {
            "field": field,
            "raw": candidate.raw_text,
            "state": candidate.value_state,
            "normalized": candidate.normalized,
            "locator": candidate.locator.to_dict() if candidate.locator else None,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    if key in seen:
        return
    seen.add(key)
    out.append(candidate)


def _canonical(name: str) -> str:
    compact = name.strip().replace("_", "").replace("-", "").casefold()
    mapped = _ALIASES.get(compact)
    if mapped:
        return mapped
    for known in _KNOWN_FIELDS:
        if compact == known.replace("_", ""):
            return known
    return name.strip()
