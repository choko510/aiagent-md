"""Read-only ntlint report integration tests."""
from __future__ import annotations

import json

import pytest

from aiagent_development.contracts import InvalidEvidence
from aiagent_development.ntlint_report import parse_ntlint_jsonl

SHA = "a" * 40


def finding(**kw: object) -> dict[str, object]:
    d: dict[str, object] = dict(
        type="finding", ruleId="test-name-verifies-claim", docs="docs/rules/claim.md",
        severity="warning", messageId="candidate", file="tests/test_cart.py",
        line=12, endLine=14, subject="untrusted text", subjectSource="test",
        value=0.72, at=0.66, margin=1.09, confidence=None,
    )
    d.update(kw)
    return d


def summary(**kw: object) -> dict[str, object]:
    d: dict[str, object] = dict(
        type="summary", verdicts=2, reported=1, candidates=1, unsure=0,
        missing=0, requests=1, inputTokens=100, usd=0.001,
        cached=0, failures=0, complete=True,
    )
    d.update(kw)
    return d


def encode(*entries: dict[str, object]) -> str:
    return "\n".join(json.dumps(entry) for entry in entries)


def parse(output: str, *, exit_code: int = 0, sha: str = SHA):
    return parse_ntlint_jsonl(output, exit_code=exit_code, source_sha=sha, expected_sha=SHA)


def test_candidate_not_promoted_or_forwarded_as_untrusted_subject() -> None:
    result = parse(encode(finding(), summary()))
    assert result.status == "REVIEW_REQUIRED"
    assert result.complete
    assert result.findings[0].message_id == "candidate"
    assert result.source_sha == SHA
    assert not hasattr(result.findings[0], "subject")


def test_complete_empty_findings_only_mean_no_candidates() -> None:
    result = parse(encode(summary(reported=0, candidates=0)))
    assert result.status == "NO_CANDIDATES"
    assert result.findings == ()


@pytest.mark.parametrize(
    "changes,code,expected",
    [
        ({"complete": False}, 2, "INCOMPLETE"),
        ({"complete": False, "missing": 1}, 2, "INCOMPLETE"),
        ({"complete": False, "failures": 1}, 2, "INCOMPLETE"),
        ({"unsure": 1}, 0, "REVIEW_REQUIRED"),
        ({"verdicts": 0}, 0, "NO_TESTS"),
    ],
)
def test_unknown_or_zero_verdicts_never_pass(changes: dict[str, object], code: int, expected: str) -> None:
    result = parse(encode(summary(reported=0, candidates=0, **changes)), exit_code=code)
    assert result.status == expected


def test_future_fitted_violation_retained_only_as_diagnostic() -> None:
    result = parse(encode(finding(messageId="violation"), summary(candidates=0)), exit_code=1)
    assert result.status == "REVIEW_REQUIRED"
    assert result.findings[0].message_id == "violation"


@pytest.mark.parametrize(
    "bad",
    [
        "",\n        "not json",\n        encode(finding()),\n        encode(summary(), finding()),
        encode(summary(), summary()),
        encode(finding(file="../secret"), summary()),
        encode(finding(file="C:\\secrets"), summary()),
        encode(finding(file=".git/config"), summary()),
        encode(finding(file="x\n::error::foo"), summary()),
        encode(finding(line=0), summary()),
        encode(finding(endLine=1), summary()),
        encode(finding(value=float("nan")), summary()),
        encode(finding(value=True), summary()),
        encode(finding(messageId="missing"), summary()),
        encode(finding(ruleId="../x"), summary()),
        encode(finding(), summary(reported=0)),
        encode(finding(), summary(candidates=0)),
        encode(finding(), summary(complete="yes")),
        encode(finding(), summary(usd=-1)),
        encode(summary(reported=False, candidates=0)),
        encode(summary()) + "\n{}\n",
        "x" * 1_048_577,
    ],
)
def test_malformed_untrusted_output_rejected(bad: str) -> None:
    with pytest.raises(InvalidEvidence):
        parse(bad)


@pytest.mark.parametrize("code", [-1, 3, True])
def test_invalid_exit_code_rejected(code: int) -> None:
    with pytest.raises(InvalidEvidence):
        parse(encode(finding(), summary()), exit_code=code)


def test_inconsistent_failure_exit_is_not_clean() -> None:
    assert parse(encode(finding(), summary()), exit_code=1).status == "INCOMPLETE"


def test_stale_sha_rejected() -> None:
    raw = encode(finding(), summary())
    for sha in ("b" * 40, "x" * 40, "a" * 39, ""):
        with pytest.raises(InvalidEvidence):
            parse(raw, sha=sha)


def test_finding_count_is_bounded() -> None:
    raw = encode(*([finding()] * 1025), summary(reported=1025, candidates=1025, verdicts=1025))
    with pytest.raises(InvalidEvidence):
        parse(raw)
