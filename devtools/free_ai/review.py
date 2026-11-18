"""Strict JSON parsing and deterministic scoring for STEP 3 code-review tasks.

A provider answer is trusted only after it matches the exact object shape documented in
the fixture prompt. Parsing never strips markdown fences, never extracts JSON from prose
and never coerces types; anything else is a format failure. Scoring is pure Python: the
model never grades itself and the answer key never leaves the machine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import NoReturn, cast

from . import ReasonCode
from .review_key import (
    MAX_REVIEW_FINDINGS,
    REVIEW_KEY,
    RULE_IDS,
    SEVERITIES,
    Defect,
    RuleId,
    Severity,
)

MAX_REVIEW_CONTENT_CHARS = 4096
MAX_JSON_DEPTH = 8


class ReviewFormatError(ValueError):
    """The review answer is not the exact JSON object the task asked for."""

    def __init__(self, reason_code: ReasonCode) -> None:
        super().__init__("review answer failed strict JSON validation")
        self.reason_code: ReasonCode = reason_code


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    """One model-reported finding; values are validated against closed sets."""

    line: int
    rule_id: RuleId
    severity: Severity


@dataclass(frozen=True, slots=True)
class ReviewScore:
    """Deterministic TP/FP/FN outcome plus location, rule and severity agreement."""

    fixture_id: str
    tp: int
    fp: int
    fn: int
    severity_matches: int
    precision: float | None
    recall: float | None
    f1: float | None
    clean_task: bool
    clean_pass: bool
    reported_findings: int


def _reject_non_finite(token: str) -> object:
    raise ValueError("non-finite number in review answer")


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key in review answer")
        result[key] = value
    return result


def _check_depth(value: object, depth: int) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ReviewFormatError("INVALID_SCHEMA")
    if isinstance(value, str) or value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, dict):
        for nested in value.values():
            _check_depth(nested, depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _check_depth(item, depth + 1)
        return
    raise ReviewFormatError("INVALID_SCHEMA")


def _reject(reason_code: ReasonCode) -> NoReturn:
    raise ReviewFormatError(reason_code)


def _parse_line(value: object, max_line: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _reject("INVALID_SCHEMA")
    if not 1 <= value <= max_line:
        _reject("INVALID_SCHEMA")
    return value


def _parse_rule_id(value: object) -> RuleId:
    if not isinstance(value, str) or value not in RULE_IDS:
        _reject("INVALID_SCHEMA")
    return cast("RuleId", value)


def _parse_severity(value: object) -> Severity:
    if not isinstance(value, str) or value not in SEVERITIES:
        _reject("INVALID_SCHEMA")
    return cast("Severity", value)


def parse_review_findings(text: str, *, max_line: int) -> tuple[ReviewFinding, ...]:
    """Parse only the exact ``{"findings": [...]}`` object; no fences or prose.

    ``max_line`` is the fixture's real numbered line count, so a finding cannot point
    outside the code that was actually sent. Duplicate keys, ``NaN``/``Infinity``,
    excessive nesting, missing or extra keys, wrong types, unknown enums and over-long
    lists are all rejected as ``INVALID_JSON`` or ``INVALID_SCHEMA``.
    """
    if len(text) > MAX_REVIEW_CONTENT_CHARS:
        _reject("INVALID_SCHEMA")
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_non_finite,
        )
    except (ValueError, RecursionError) as exc:
        raise ReviewFormatError("INVALID_JSON") from exc
    _check_depth(parsed, 0)
    if not isinstance(parsed, dict) or set(parsed) != {"findings"}:
        _reject("INVALID_SCHEMA")
    findings = parsed.get("findings")
    if not isinstance(findings, list) or len(findings) > MAX_REVIEW_FINDINGS:
        _reject("INVALID_SCHEMA")
    result: list[ReviewFinding] = []
    for item in findings:
        if not isinstance(item, dict) or set(item) != {"line", "rule_id", "severity"}:
            _reject("INVALID_SCHEMA")
        result.append(
            ReviewFinding(
                line=_parse_line(item.get("line"), max_line),
                rule_id=_parse_rule_id(item.get("rule_id")),
                severity=_parse_severity(item.get("severity")),
            )
        )
    return tuple(result)


def _metrics(tp: int, fp: int, fn: int) -> tuple[float | None, float | None, float | None]:
    precision = None if tp + fp == 0 else round(tp / (tp + fp), 4)
    recall = None if tp + fn == 0 else round(tp / (tp + fn), 4)
    denominator = 2 * tp + fp + fn
    f1 = None if denominator == 0 else round(2 * tp / denominator, 4)
    return precision, recall, f1


def score_review(fixture_id: str, findings: tuple[ReviewFinding, ...]) -> ReviewScore:
    """Match findings to the independent key by ``(line, rule_id)``.

    The first finding matching an unmatched gold defect is a true positive; every other
    finding (including a duplicate of a matched one) is a false positive. Severity is
    scored separately so a right location and rule with the wrong severity is still TP.
    """
    truth = REVIEW_KEY[fixture_id]
    unmatched: list[Defect] = list(truth)
    tp = 0
    fp = 0
    severity_matches = 0
    for finding in findings:
        match = next(
            (
                index
                for index, defect in enumerate(unmatched)
                if defect.line == finding.line and defect.rule_id == finding.rule_id
            ),
            None,
        )
        if match is None:
            fp += 1
            continue
        defect = unmatched.pop(match)
        tp += 1
        if finding.severity == defect.severity:
            severity_matches += 1
    fn = len(unmatched)
    precision, recall, f1 = _metrics(tp, fp, fn)
    clean_task = not truth
    return ReviewScore(
        fixture_id=fixture_id,
        tp=tp,
        fp=fp,
        fn=fn,
        severity_matches=severity_matches,
        precision=precision,
        recall=recall,
        f1=f1,
        clean_task=clean_task,
        clean_pass=clean_task and not findings,
        reported_findings=len(findings),
    )
