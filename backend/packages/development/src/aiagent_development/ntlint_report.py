"""Bounded read-only ntlint 0.1.1 JSONL importer.

No execution, API call, shell access, provider key or release-gate mutation.
Trusted owners must authenticate the artifact and supply its source SHA.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Literal, cast

from .contracts import InvalidEvidence, InvalidPath
from .paths import safe_relative_path

_RULE = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
Status = Literal["INCOMPLETE", "NO_TESTS", "REVIEW_REQUIRED", "NO_CANDIDATES"]


@dataclass(frozen=True, slots=True)
class NtlintFinding:
    """A candidate only, never an independently verified ReviewFinding."""
    rule_id: str
    file: str
    line: int
    end_line: int
    message_id: Literal["candidate", "violation"]
    severity: Literal["info", "warning", "error"]
    value: float


@dataclass(frozen=True, slots=True)
class NtlintReport:
    source_sha: str
    exit_code: int
    status: Status
    findings: tuple[NtlintFinding, ...]
    verdicts: int
    missing: int
    unsure: int
    failures: int
    input_tokens: int
    usd: float

    @property
    def complete(self) -> bool:
        """Completeness of the scan, NOT a test/review/release PASS."""
        return self.status not in ("INCOMPLETE", "NO_TESTS")


def _record(v: object) -> dict[str, object]:
    if not isinstance(v, dict) or any(not isinstance(k, str) for k in v):
        raise InvalidEvidence("invalid ntlint JSON record")
    return cast(dict[str, object], v)


def _count(record: dict[str, object], key: str) -> int:
    n = record.get(key)
    if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= 10_000_000:
        raise InvalidEvidence(f"invalid ntlint count: {key}")
    return n


def _number(record: dict[str, object], key: str, high: float) -> float:
    n = record.get(key)
    if not isinstance(n, (int, float)) or isinstance(n, bool):
        raise InvalidEvidence(f"invalid ntlint number: {key}")
    value = float(n)
    if not math.isfinite(value) or not 0 <= value <= high:
        raise InvalidEvidence(f"out-of-range ntlint number: {key}")
    return value


def _finding(d: dict[str, object]) -> NtlintFinding:
    rule, path = d.get("ruleId"), d.get("file")
    if not isinstance(rule, str) or not _RULE.fullmatch(rule):
        raise InvalidEvidence("invalid ntlint rule ID")
    if not isinstance(path, str):
        raise InvalidEvidence("invalid ntlint file path")
    try:
        safe_relative_path(path)
    except InvalidPath as exc:
        raise InvalidEvidence("unsafe ntlint file path") from exc
    start, end = _count(d, "line"), _count(d, "endLine")
    if start < 1 or end < start:
        raise InvalidEvidence("invalid ntlint line span")
    message, severity = d.get("messageId"), d.get("severity")
    if message not in ("candidate", "violation") or severity not in ("info", "warning", "error"):
        raise InvalidEvidence("unsupported ntlint finding type")
    return NtlintFinding(
        rule, path, start, end,
        cast(Literal["candidate", "violation"], message),
        cast(Literal["info", "warning", "error"], severity),
        _number(d, "value", 1.0),
    )


def _reject_nonfinite(_: str) -> None:
    raise InvalidEvidence("nonfinite ntlint JSON number")


def parse_ntlint_jsonl(output: str, *, exit_code: int, source_sha: str, expected_sha: str) -> NtlintReport:
    """Parse a verified Cell artifact; source_sha and expected_sha must originate from trusted owners.

    Does not validate their authority, send data to Typesafe AI or run ntlint.
    """
    if (
        not isinstance(source_sha, str) or not isinstance(expected_sha, str)
        or not _SHA.fullmatch(source_sha) or source_sha != expected_sha
    ):
        raise InvalidEvidence("missing or stale trusted ntlint SHA")
    if type(exit_code) is not int or exit_code not in (0, 1, 2):
        raise InvalidEvidence("invalid ntlint exit code")
    if not isinstance(output, str) or not output or len(output.encode("utf-8")) > 1_048_576:
        raise InvalidEvidence("empty or oversized ntlint output")
    lines = output.splitlines()
    if len(lines) > 1025 or any(not line or len(line) > 16_384 for line in lines):
        raise InvalidEvidence("ntlint report size or record count exceeded")

    findings: list[NtlintFinding] = []
    summary: dict[str, object] | None = None
    for line in lines:
        try:
            d = _record(json.loads(line, parse_constant=_reject_nonfinite))
        except (ValueError, TypeError, RecursionError) as exc:
            raise InvalidEvidence("malformed ntlint JSONL") from exc
        if summary is not None:
            raise InvalidEvidence("ntlint has records after summary")
        if d.get("type") == "finding":
            findings.append(_finding(d))
        elif d.get("type") == "summary":
            summary = d
        else:
            raise InvalidEvidence("unknown ntlint JSONL record")
    if summary is None:
        raise InvalidEvidence("missing ntlint summary")

    verdicts = _count(summary, "verdicts")
    reported = _count(summary, "reported")
    candidates = _count(summary, "candidates")
    missing = _count(summary, "missing")
    unsure = _count(summary, "unsure")
    failures = _count(summary, "failures")
    tokens = _count(summary, "inputTokens")
    usd = _number(summary, "usd", 10_000_000)
    complete = summary.get("complete")
    if type(complete) is not bool:
        raise InvalidEvidence("missing ntlint completeness")
    if (reported != len(findings) or candidates != sum(f.message_id == "candidate" for f in findings)
            or reported > verdicts or missing + unsure > verdicts):
        raise InvalidEvidence("inconsistent ntlint summary")

    if (exit_code == 2 or not complete or missing or failures
            or (exit_code == 1 and not any(f.message_id == "violation" for f in findings))
            or (exit_code == 0 and any(f.message_id == "violation" for f in findings))):
        status: Status = "INCOMPLETE"
    elif verdicts == 0:
        status = "NO_TESTS"
    elif findings or unsure:
        status = "REVIEW_REQUIRED"
    else:
        status = "NO_CANDIDATES"
    return NtlintReport(source_sha, exit_code, status, tuple(findings), verdicts, missing, unsure, failures, tokens, usd)
