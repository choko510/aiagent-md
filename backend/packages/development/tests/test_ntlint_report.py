"""Read-only ntlint report integration tests."""

from __future__ import annotations

import json

import pytest

from aiagent_development.contracts import InvalidEvidence
from aiagent_development.ntlint_report import parse_ntlint_jsonl

SHA = "a" * 40


def finding(**kw: object) -> dict[str, object]:
    d: dict[str, object] = dict(
        type="finding",
        ruleId="test-name-verifies-claim",
        docs="docs/rules/claim.md",
        severity="warning",
        messageId="candidate",
        file="tests/test_cart.py",
        line=12,
        endLine=14,
        subject="untrusted text",
        subjectSource="test",
        value=0.72,
        at=0.66,
        margin=1.09,
        confidence=None,
    )
    d.update(kw)
    return d


def summary(**kw: object) -> dict[str, object]:
    d: dict[str, object] = dict(
        type="summary",
        verdicts=2,
        reported=1,
        candidates=1,
        unsure=0,
        missing=0,
        requests=1,
        inputTokens=100,
        usd=0.001,
        cached=0,
        failures=0,
        complete=True,
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
        "",
        "not json",
        encode(finding()),
        encode(summary(), finding()),
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


def test_disjoint_verdict_counts_cannot_exceed_total() -> None:
    raw = encode(finding(), summary(verdicts=2, reported=1, unsure=2))
    with pytest.raises(InvalidEvidence, match="inconsistent ntlint summary"):
        parse(raw)


@pytest.mark.parametrize("field", ["value", "at", "margin", "confidence"])
def test_giant_numeric_finding_is_a_typed_failure(field: str) -> None:
    raw = encode(finding(**{field: 10**309}), summary())
    with pytest.raises(InvalidEvidence):
        parse(raw)


def test_giant_usd_is_a_typed_failure() -> None:
    with pytest.raises(InvalidEvidence):
        parse(encode(finding(), summary(usd=10**309)))


def test_non_utf8_encodable_text_is_a_typed_failure() -> None:
    raw = encode(finding(), summary()) + "\ud800"
    with pytest.raises(InvalidEvidence, match="UTF-8"):
        parse(raw)


def test_sha256_is_accepted_via_shared_development_contract() -> None:
    sha256 = "c" * 64
    result = parse_ntlint_jsonl(
        encode(finding(), summary()), exit_code=0, source_sha=sha256, expected_sha=sha256
    )
    assert result.source_sha == sha256
    assert result.status == "REVIEW_REQUIRED"


def test_mismatched_sha_algorithms_are_rejected() -> None:
    with pytest.raises(InvalidEvidence):
        parse_ntlint_jsonl(
            encode(finding(), summary()),
            exit_code=0,
            source_sha="a" * 40,
            expected_sha="a" * 64,
        )


@pytest.mark.parametrize("index", [0, 1])
def test_duplicate_json_keys_are_rejected(index: int) -> None:
    lines = encode(finding(), summary()).splitlines()
    key = '"ruleId": "test-name-verifies-claim"' if index == 0 else '"reported": 1'
    lines[index] = lines[index].replace(key, key + ", " + key)
    with pytest.raises(InvalidEvidence, match="duplicate"):
        parse("\n".join(lines))


@pytest.mark.parametrize(
    "invalid",
    [
        encode(finding(unexpected="injected"), summary()),
        encode(finding(), summary(extra=1)),
        encode(finding(subjectSource="arbitrary"), summary()),
        encode(finding(confidence={"nested": "object"}), summary()),
        encode(finding(at=2.0), summary()),
        encode(finding(), summary(requests=True)),
        encode(finding(), summary(requests=-1)),
        encode(finding(), summary(cached="1")),
        encode(finding(), summary(cached=-1)),
    ],
)
def test_unknown_fields_or_unused_metadata_are_checked(invalid: str) -> None:
    with pytest.raises(InvalidEvidence):
        parse(invalid)


def test_finding_count_is_bounded() -> None:
    raw = encode(*([finding()] * 1025), summary(reported=1025, candidates=1025, verdicts=1025))
    with pytest.raises(InvalidEvidence):
        parse(raw)
