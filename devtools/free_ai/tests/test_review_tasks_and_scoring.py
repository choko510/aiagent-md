"""STEP 3: gold-backed code review, strict parsing, scoring and coverage report."""

from __future__ import annotations

import ast
import json
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import httpx
import pytest

from devtools.free_ai import catalog, fixtures, review, runner, transport
from devtools.free_ai.review_key import (
    MAX_REVIEW_FINDINGS,
    REVIEW_FIXTURE_IDS,
    REVIEW_KEY,
    REVIEW_LINE_COUNTS,
    RULE_IDS,
    SEVERITIES,
)

_REAL_NEW_CLIENT = transport._new_client
_KILO_MODEL = "nvidia/nemotron-3.5-super-vl:free"
_OVH_MODEL = "gpt-oss-120b"

_EXPECTED = {
    "review_r01": (("INDEX_OUT_OF_RANGE", "P2", 2), ("SECRET_LOGGING", "P1", 5)),
    "review_r02": (("SQL_INJECTION", "P1", 2), ("BOUNDARY_BUG", "P2", 6)),
    "review_r03": (("STALE_LEASE", "P1", 2), ("UNSAFE_RETRY", "P1", 8)),
    "review_r04": (),
    "review_r05": (),
    "review_r06": (("SECRET_LOGGING", "P1", 8),),
    "review_r07": (("UNSAFE_RETRY", "P1", 5),),
    "review_r08": (),
}


def _blocked(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError("offline test attempted to open a network socket")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)


def _temp_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    runs = tmp_path / ".runs"
    monkeypatch.setattr(runner, "RUNS_DIR", runs)
    monkeypatch.setattr(runner, "QUOTA_FILE", runs / "quota.json")
    monkeypatch.setattr(runner, "LOCK_FILE", runs / "lock")
    monkeypatch.setattr(runner, "LOG_FILE", runs / "probes.jsonl")
    monkeypatch.setattr(runner, "REVIEW_SCORE_LOG_FILE", runs / "review_scores.jsonl")
    return runs


def _mock_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(transport, "_new_client", lambda: _REAL_NEW_CLIENT(httpx.MockTransport(recording)))
    return requests


def _probe_argv(*extra: str, provider: str = "kilo", fixture: str) -> list[str]:
    return [
        "probe",
        "--provider",
        provider,
        "--fixture",
        fixture,
        "--ack-synthetic-only",
        "--ack-external-request",
        *extra,
    ]


def _chat_body(content: str, *, model: str = _KILO_MODEL, cost: object = 0) -> bytes:
    body: dict[str, object] = {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }
    if cost is not None:
        body["cost"] = cost
    return json.dumps(body).encode()


def _ok(content: str, *, model: str = _KILO_MODEL, cost: object = 0) -> Callable[[httpx.Request], httpx.Response]:
    payload = _chat_body(content, model=model, cost=cost)
    return lambda _request: httpx.Response(200, headers={"content-type": "application/json"}, content=payload)


def _findings(*items: tuple[int, str, str]) -> str:
    return json.dumps(
        {"findings": [{"line": line, "rule_id": rule_id, "severity": severity} for line, rule_id, severity in items]}
    )


def _last(out: str) -> dict[str, object]:
    lines = [line for line in out.splitlines() if line.startswith("{")]
    assert lines
    parsed = json.loads(lines[-1])
    assert isinstance(parsed, dict)
    return parsed


def _write_quota(path: Path, provider: str, *, day_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {
                    provider: {
                        "last_utc_epoch": time.time() - 1000,
                        "local_day": time.strftime("%Y-%m-%d", time.gmtime()),
                        "day_count": day_count,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


# --- A: fixture and gold contract ---------------------------------------------------------


def test_exact_eight_cases_eight_gold_and_three_clean() -> None:
    assert tuple(_EXPECTED) == REVIEW_FIXTURE_IDS
    assert fixtures.REVIEW_FIXTURE_IDS == REVIEW_FIXTURE_IDS
    assert set(REVIEW_KEY) == set(_EXPECTED)
    assert sum(len(key) for key in REVIEW_KEY.values()) == 8
    assert sum(1 for key in REVIEW_KEY.values() if not key) == 3
    for fixture_id, expected in _EXPECTED.items():
        key = REVIEW_KEY[fixture_id]
        assert [(defect.rule_id, defect.severity, defect.line) for defect in key] == list(expected)
        assert fixtures.require_fixture(fixture_id).review_task is True


def test_gold_lines_exist_in_the_sent_code() -> None:
    for fixture_id, key in REVIEW_KEY.items():
        fixture = fixtures.require_fixture(fixture_id)
        assert fixture.prompt is not None
        numbered = {}
        for line in fixture.prompt.splitlines():
            prefix, separator, code = line.partition(": ")
            if separator and prefix.isdigit():
                numbered[int(prefix)] = code
        assert len(numbered) == fixture.review_line_count
        assert REVIEW_LINE_COUNTS[fixture_id] == fixture.review_line_count
        assert set(numbered) == set(range(1, fixture.review_line_count + 1))
        for defect in key:
            assert defect.line in numbered
        # The answer key itself is never inside the prompt.
        assert "REVIEW_KEY" not in fixture.prompt
        assert "Defect(" not in fixture.prompt


def test_fixtures_module_never_imports_gold() -> None:
    source = Path(fixtures.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any("review_key" in name for name in imported)


def test_payload_has_only_the_prompt_and_no_gold_values() -> None:
    kilo = catalog.require_spec("kilo")
    payload = fixtures.build_payload(kilo, "review_r02")
    messages = payload["messages"]
    assert isinstance(messages, list)
    first = messages[0]
    assert isinstance(first, dict)
    prompt = first["content"]
    assert isinstance(prompt, str)
    assert prompt == fixtures.REVIEW_R02_PROMPT
    for defect in REVIEW_KEY["review_r02"]:
        assert f'"line":{defect.line}' not in prompt
    assert '"line":<line number>' in prompt
    assert "review_key" not in prompt


def test_review_fixtures_are_not_allowed_on_legacy_services() -> None:
    for fixture_id in REVIEW_FIXTURE_IDS:
        assert fixture_id in fixtures.allowed_fixtures(catalog.require_spec("kilo"))
        assert fixture_id in fixtures.allowed_fixtures(catalog.require_spec("ovh"))
        assert fixture_id not in fixtures.allowed_fixtures(catalog.require_spec("jev_legacy"))


# --- B: strict JSON parser ---------------------------------------------------------------


def test_parser_accepts_the_exact_shape_and_empty_clean_answer() -> None:
    assert review.parse_review_findings('{"findings":[]}', max_line=7) == ()
    parsed = review.parse_review_findings(
        '{"findings":[{"line":2,"rule_id":"INDEX_OUT_OF_RANGE","severity":"P2"}]}',
        max_line=5,
    )
    assert [(item.line, item.rule_id, item.severity) for item in parsed] == [(2, "INDEX_OUT_OF_RANGE", "P2")]


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        '```json\n{"findings":[]}\n```',
        '{"findings":[]} trailing',
        "[]",
        '{"findings":{}}',
        '{"findings":[],"extra":1}',
        '{"findings":[{"line":2,"rule_id":"SQL_INJECTION"}]}',
        '{"findings":[{"line":2,"rule_id":"SQL_INJECTION","severity":"P1","extra":1}]}',
        '{"findings":[{"line":2,"kind":"SQL_INJECTION","severity":"P1"}]}',
        '{"findings":[{"line":true,"rule_id":"SQL_INJECTION","severity":"P1"}]}',
        '{"findings":[{"line":"2","rule_id":"SQL_INJECTION","severity":"P1"}]}',
        '{"findings":[{"line":0,"rule_id":"SQL_INJECTION","severity":"P1"}]}',
        '{"findings":[{"line":6,"rule_id":"SQL_INJECTION","severity":"P1"}]}',
        '{"findings":[{"line":2,"rule_id":"BUG","severity":"P1"}]}',
        '{"findings":[{"line":2,"rule_id":"SQL_INJECTION","severity":"high"}]}',
        '{"findings":[],"findings":[]}',
        '{"findings":[{"line":NaN,"rule_id":"SQL_INJECTION","severity":"P1"}]}',
        '{"findings":[[]]}',
    ],
)
def test_parser_rejects_anything_but_the_exact_contract(content: str) -> None:
    with pytest.raises(review.ReviewFormatError) as excinfo:
        review.parse_review_findings(content, max_line=5)
    assert excinfo.value.reason_code in {"INVALID_JSON", "INVALID_SCHEMA"}


def test_parser_rejects_more_than_eight_findings() -> None:
    items = [{"line": 2, "rule_id": "SQL_INJECTION", "severity": "P1"} for _ in range(MAX_REVIEW_FINDINGS + 1)]
    with pytest.raises(review.ReviewFormatError):
        review.parse_review_findings(json.dumps({"findings": items}), max_line=10)


def test_fixture_specific_line_upper_bound() -> None:
    assert review.parse_review_findings(
        '{"findings":[{"line":5,"rule_id":"SECRET_LOGGING","severity":"P1"}]}',
        max_line=5,
    )
    with pytest.raises(review.ReviewFormatError):
        review.parse_review_findings(
            '{"findings":[{"line":6,"rule_id":"SECRET_LOGGING","severity":"P1"}]}',
            max_line=5,
        )


def test_enums_are_exactly_the_formal_sets() -> None:
    assert {
        "INDEX_OUT_OF_RANGE",
        "SECRET_LOGGING",
        "SQL_INJECTION",
        "BOUNDARY_BUG",
        "STALE_LEASE",
        "UNSAFE_RETRY",
    } == RULE_IDS
    assert {"P1", "P2", "P3"} == SEVERITIES


# --- C: scoring --------------------------------------------------------------------------


def _score(fixture_id: str, *items: tuple[int, str, str]) -> review.ReviewScore:
    findings = tuple(review.ReviewFinding(*item) for item in items)  # type: ignore[arg-type]
    return review.score_review(fixture_id, findings)


def test_scoring_perfect_multi_defect_case() -> None:
    score = _score(
        "review_r01",
        (2, "INDEX_OUT_OF_RANGE", "P2"),
        (5, "SECRET_LOGGING", "P1"),
    )
    assert (score.tp, score.fp, score.fn) == (2, 0, 0)
    assert score.severity_matches == 2
    assert (score.precision, score.recall, score.f1) == (1.0, 1.0, 1.0)


def test_scoring_duplicate_is_fp_and_severity_is_separate() -> None:
    duplicate = _score(
        "review_r01",
        (2, "INDEX_OUT_OF_RANGE", "P2"),
        (2, "INDEX_OUT_OF_RANGE", "P2"),
    )
    assert (duplicate.tp, duplicate.fp, duplicate.fn) == (1, 1, 1)
    assert duplicate.severity_matches == 1

    wrong_severity = _score("review_r06", (8, "SECRET_LOGGING", "P3"))
    assert (wrong_severity.tp, wrong_severity.fp, wrong_severity.fn) == (1, 0, 0)
    assert wrong_severity.severity_matches == 0
    assert wrong_severity.precision == 1.0
    assert wrong_severity.recall == 1.0


def test_scoring_wrong_line_is_fp_and_fn() -> None:
    score = _score("review_r06", (7, "SECRET_LOGGING", "P1"))
    assert (score.tp, score.fp, score.fn) == (0, 1, 1)


def test_clean_case_scores() -> None:
    clean = _score("review_r04")
    assert clean.clean_pass is True
    assert (clean.tp, clean.fp, clean.fn) == (0, 0, 0)
    assert (clean.precision, clean.recall, clean.f1) == (None, None, None)

    flagged = _score("review_r04", (3, "SQL_INJECTION", "P1"))
    assert flagged.clean_pass is False
    assert (flagged.tp, flagged.fp, flagged.fn) == (0, 1, 0)
    assert flagged.precision == 0.0


# --- D: E2E probes and report ------------------------------------------------------------


def _probe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    fixture: str,
    content: str,
    provider: str = "kilo",
    model: str = _KILO_MODEL,
    cost: object = 0,
) -> tuple[int, dict[str, object], list[httpx.Request]]:
    requests = _mock_transport(monkeypatch, _ok(content, model=model, cost=cost))
    code = runner.main(_probe_argv(provider=provider, fixture=fixture))
    record = _last(capsys.readouterr().out)
    return code, record, requests


def test_e2e_perfect_review_is_pass_and_fully_aggregated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    content = _findings(
        (2, "INDEX_OUT_OF_RANGE", "P2"),
        (5, "SECRET_LOGGING", "P1"),
    )
    code, record, requests = _probe(monkeypatch, capsys, fixture="review_r01", content=content)
    assert code == 0
    assert record["status"] == "PASS"
    assert len(requests) == 1

    probe_line = json.loads((runs / "probes.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert len(probe_line) == 13
    review_line = json.loads((runs / "review_scores.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert list(review_line) == list(runner.REVIEW_RECORD_FIELDS)
    assert review_line["tp"] == 2
    assert review_line["fp"] == 0
    assert review_line["fn"] == 0
    assert review_line["severity_matches"] == 2
    assert review_line["http_status"] == 200
    assert review_line["reason_code"] == "OK"
    assert isinstance(review_line["elapsed_ms"], int)
    assert review_line["elapsed_ms"] >= 0

    assert runner.main(["review-report"]) == 0
    kilo = json.loads(capsys.readouterr().out)["providers"]["kilo"]
    assert kilo["attempts"] == 1
    assert kilo["valid_scored_cases"] == 1
    assert kilo["coverage"] == 1.0
    assert (kilo["tp"], kilo["fp"], kilo["fn"]) == (2, 0, 0)
    assert kilo["micro_precision"] == 1.0
    assert kilo["micro_recall"] == 1.0
    assert kilo["micro_f1"] == 1.0
    assert kilo["false_discovery_rate"] == 0.0
    assert kilo["severity_correct"] == 2
    assert kilo["severity_total_tp"] == 2
    assert kilo["severity_accuracy"] == 1.0
    assert kilo["case_exact_matches"] == 1
    assert kilo["clean_cases_scored"] == 0
    assert kilo["fixtures_executed"] == 1


def test_e2e_partial_review_metrics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _temp_runs(monkeypatch, tmp_path)
    code, record, _ = _probe(
        monkeypatch,
        capsys,
        fixture="review_r02",
        content=_findings((2, "SQL_INJECTION", "P1")),
    )
    assert code == 4
    assert record["status"] == "CONTENT_MISMATCH"
    assert runner.main(["review-report"]) == 0
    kilo = json.loads(capsys.readouterr().out)["providers"]["kilo"]
    assert (kilo["tp"], kilo["fp"], kilo["fn"]) == (1, 0, 1)
    assert kilo["micro_precision"] == 1.0
    assert kilo["micro_recall"] == 0.5
    assert kilo["micro_f1"] == 0.6667
    assert kilo["false_discovery_rate"] == 0.0
    assert kilo["case_exact_matches"] == 0


def test_e2e_clean_case_and_false_alarm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _temp_runs(monkeypatch, tmp_path)
    code, record, _ = _probe(monkeypatch, capsys, fixture="review_r04", content=_findings())
    assert code == 0
    assert record["status"] == "PASS"
    assert runner.main(["review-report"]) == 0
    kilo = json.loads(capsys.readouterr().out)["providers"]["kilo"]
    assert kilo["clean_cases_scored"] == 1
    assert kilo["clean_cases_correct"] == 1
    assert kilo["clean_cases_flagged"] == 0
    assert kilo["clean_case_false_alarm_rate"] == 0.0
    assert kilo["case_exact_matches"] == 1

    _temp_runs(monkeypatch, tmp_path / "second")
    code, record, _ = _probe(
        monkeypatch,
        capsys,
        fixture="review_r05",
        content=_findings((3, "SQL_INJECTION", "P1")),
    )
    assert code == 4
    assert record["status"] == "CONTENT_MISMATCH"
    assert runner.main(["review-report"]) == 0
    kilo = json.loads(capsys.readouterr().out)["providers"]["kilo"]
    assert kilo["clean_cases_scored"] == 1
    assert kilo["clean_cases_correct"] == 0
    assert kilo["clean_cases_flagged"] == 1
    assert kilo["clean_case_false_alarm_rate"] == 1.0
    assert kilo["false_discovery_rate"] == 1.0


def test_e2e_format_failure_is_counted_but_not_scored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    code, record, _ = _probe(monkeypatch, capsys, fixture="review_r01", content="The code has a bug.")
    assert code == 4
    assert record["status"] == "FORMAT_MISMATCH"
    lines = (runs / "review_scores.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["status"] == "FORMAT_MISMATCH"
    assert row["tp"] is None and row["fp"] is None and row["fn"] is None

    assert runner.main(["review-report"]) == 0
    kilo = json.loads(capsys.readouterr().out)["providers"]["kilo"]
    assert kilo["attempts"] == 1
    assert kilo["valid_scored_cases"] == 0
    assert kilo["format_failures"] == 1
    assert kilo["coverage"] == 0.0
    assert kilo["micro_precision"] is None
    assert kilo["case_exact_matches"] == 0


def test_coverage_includes_format_timeout_and_quota_skips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)

    _write_quota(runs / "quota.json", "kilo", day_count=0)
    _probe(
        monkeypatch,
        capsys,
        fixture="review_r01",
        content=_findings((2, "INDEX_OUT_OF_RANGE", "P2"), (5, "SECRET_LOGGING", "P1")),
    )

    _write_quota(runs / "quota.json", "kilo", day_count=1)
    _probe(monkeypatch, capsys, fixture="review_r02", content="not json")

    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("synthetic timeout")

    _write_quota(runs / "quota.json", "kilo", day_count=2)
    _mock_transport(monkeypatch, timeout)
    assert runner.main(_probe_argv(fixture="review_r03")) == 3
    capsys.readouterr()

    _write_quota(runs / "quota.json", "kilo", day_count=16)
    assert runner.main(_probe_argv(fixture="review_r06")) == 2
    local = _last(capsys.readouterr().out)
    assert local["status"] == "RATE_LIMITED"
    assert local["http_status"] is None

    assert runner.main(["review-report"]) == 0
    kilo = json.loads(capsys.readouterr().out)["providers"]["kilo"]
    assert kilo["attempts"] == 4
    assert kilo["valid_scored_cases"] == 1
    assert kilo["format_failures"] == 1
    assert kilo["communication_failures"] == 1
    assert kilo["local_preflight_skips"] == 1
    assert kilo["coverage"] == 0.25
    assert kilo["fixtures_executed"] == 4


def test_legacy_review_attempt_is_local_skip_not_scored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _temp_runs(monkeypatch, tmp_path)
    requests = _mock_transport(monkeypatch, _ok(_findings()))
    assert runner.main(_probe_argv(provider="jev_legacy", fixture="review_r01")) == 2
    assert requests == []
    capsys.readouterr()
    assert runner.main(["review-report"]) == 0
    jev = json.loads(capsys.readouterr().out)["providers"]["jev_legacy"]
    assert jev["attempts"] == 1
    assert jev["valid_scored_cases"] == 0
    assert jev["local_preflight_skips"] == 1
    assert jev["coverage"] == 0.0


def test_non_review_probe_writes_no_review_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    _mock_transport(monkeypatch, _ok("こんにちは"))
    assert runner.main(_probe_argv(provider="kilo", fixture="jp_hello")) == 0
    capsys.readouterr()
    assert (runs / "probes.jsonl").exists()
    assert not (runs / "review_scores.jsonl").exists()


# --- E: two-log failure handling ---------------------------------------------------------


def test_first_append_failure_returns_five_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    _mock_transport(
        monkeypatch,
        _ok(_findings((2, "INDEX_OUT_OF_RANGE", "P2"), (5, "SECRET_LOGGING", "P1"))),
    )

    def fail(_record: object) -> None:
        raise runner.QuotaStateError("probe log write failed")

    monkeypatch.setattr(runner, "_append_probe", fail)
    assert runner.main(_probe_argv(fixture="review_r01")) == 5
    record = _last(capsys.readouterr().out)
    assert record["status"] == "SAFETY_BLOCKED"
    assert not (runs / "probes.jsonl").exists()
    assert not (runs / "review_scores.jsonl").exists()


def test_second_append_failure_returns_five_and_no_review_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    _mock_transport(
        monkeypatch,
        _ok(_findings((2, "INDEX_OUT_OF_RANGE", "P2"), (5, "SECRET_LOGGING", "P1"))),
    )

    def fail(_record: object) -> None:
        raise runner.QuotaStateError("review log write failed")

    monkeypatch.setattr(runner, "_append_review_record", fail)
    assert runner.main(_probe_argv(fixture="review_r01")) == 5
    record = _last(capsys.readouterr().out)
    assert record["status"] == "SAFETY_BLOCKED"
    assert (runs / "probes.jsonl").exists()
    assert not (runs / "review_scores.jsonl").exists()

    assert runner.main(["review-report"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["records"] == 0
    assert report["providers"]["kilo"]["valid_scored_cases"] == 0


# --- F: report validation ----------------------------------------------------------------


def _review_row(overrides: dict[str, object] | None = None) -> dict[str, object]:
    row: dict[str, object] = {
        "schema_version": 1,
        "provider_id": "kilo",
        "model_requested": _KILO_MODEL,
        "model_observed": _KILO_MODEL,
        "fixture_id": "review_r01",
        "observed_at_utc": "2026-11-17T00:00:00Z",
        "http_status": 200,
        "status": "PASS",
        "reason_code": "OK",
        "tp": 2,
        "fp": 0,
        "fn": 0,
        "severity_matches": 2,
        "elapsed_ms": 100,
    }
    if overrides:
        row.update(overrides)
    return row


def _run_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    content: str,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    runs = _temp_runs(monkeypatch, tmp_path)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "review_scores.jsonl").write_text(content, encoding="utf-8")
    return runner.main(["review-report"]), capsys.readouterr().out


def test_report_missing_log_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _temp_runs(monkeypatch, tmp_path)
    assert runner.main(["review-report"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["records"] == 0
    assert report["providers"]["kilo"]["coverage"] is None
    assert report["providers"]["kilo"]["valid_scored_cases"] == 0


_INVALID_ROWS = [
    "{not json",
    json.dumps({**_review_row(), "unexpected": 1}),
    json.dumps(_review_row({"provider_id": "not-a-provider"})),
    json.dumps(_review_row({"fixture_id": "review_01"})),
    json.dumps(_review_row({"fixture_id": "dev_t01"})),
    json.dumps(_review_row({"model_requested": "wrong-model"})),
    json.dumps(_review_row({"tp": 1, "fn": 0})),
    json.dumps(_review_row({"severity_matches": 3})),
    json.dumps(_review_row({"status": "CONTENT_MISMATCH"})),
    json.dumps(_review_row({"status": "PASS", "fp": 1, "tp": 1})),
    json.dumps(_review_row({"tp": None, "fp": None, "fn": None, "severity_matches": None})),
    json.dumps(
        _review_row(
            {
                "status": "FORMAT_MISMATCH",
                "reason_code": "INVALID_JSON",
                "http_status": 200,
                "tp": 0,
            }
        )
    ),
    json.dumps(
        _review_row(
            {
                "provider_id": "jev_legacy",
                "model_requested": "jev-1.13-free",
                "model_observed": None,
                "status": "PASS",
            }
        )
    ),
    json.dumps(_review_row({"http_status": None})),
    json.dumps(_review_row({"schema_version": 2})),
    json.dumps(_review_row({"tp": True})),
    json.dumps(_review_row({"elapsed_ms": -1})),
    json.dumps(_review_row({"elapsed_ms": "100"})),
    json.dumps(_review_row({"elapsed_ms": True})),
]


def test_report_accepts_rows_written_before_elapsed_ms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    row = _review_row()
    del row["elapsed_ms"]
    code, out = _run_report(monkeypatch, tmp_path, json.dumps(row) + "\n", capsys)
    assert code == 0
    kilo = json.loads(out)["providers"]["kilo"]
    assert kilo["attempts"] == 1
    assert kilo["valid_scored_cases"] == 1
    assert kilo["tp"] == 2


@pytest.mark.parametrize("content", _INVALID_ROWS)
def test_report_rejects_invalid_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    content: str,
) -> None:
    code, out = _run_report(monkeypatch, tmp_path, content, capsys)
    assert code == 5
    assert json.loads(out) == {
        "schema_version": 1,
        "status": "INVALID_LOG",
        "reason_code": "INVALID_SCHEMA",
    }


def test_report_accepts_non_scored_rows_without_counting_them_as_quality(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [
        _review_row(
            {
                "status": "TIMEOUT",
                "reason_code": "UNKNOWN_OUTCOME",
                "http_status": None,
                "tp": None,
                "fp": None,
                "fn": None,
                "severity_matches": None,
            }
        ),
        _review_row(
            {
                "status": "FORMAT_MISMATCH",
                "reason_code": "INVALID_JSON",
                "tp": None,
                "fp": None,
                "fn": None,
                "severity_matches": None,
            }
        ),
    ]
    code, out = _run_report(monkeypatch, tmp_path, "\n".join(json.dumps(row) for row in rows) + "\n", capsys)
    assert code == 0
    kilo = json.loads(out)["providers"]["kilo"]
    assert kilo["attempts"] == 2
    assert kilo["valid_scored_cases"] == 0
    assert kilo["coverage"] == 0.0
    assert kilo["communication_failures"] == 1
    assert kilo["format_failures"] == 1
    assert kilo["micro_precision"] is None
    assert kilo["severity_accuracy"] is None
    assert kilo["case_exact_matches"] == 0


def test_sent_payload_has_no_gold_and_makes_no_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _temp_runs(monkeypatch, tmp_path)
    requests = _mock_transport(
        monkeypatch, _ok(_findings((2, "INDEX_OUT_OF_RANGE", "P2"), (5, "SECRET_LOGGING", "P1")))
    )
    assert runner.main(_probe_argv(fixture="review_r01")) == 0
    capsys.readouterr()
    assert len(requests) == 1
    body = requests[0].content.decode("utf-8")
    for defect in REVIEW_KEY["review_r01"]:
        assert f'"line":{defect.line},"rule_id":"{defect.rule_id}"' not in body
    assert "worktree" not in body.lower()


def test_report_counts_model_drift_and_billing_tool_buckets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [
        _review_row(
            {
                "status": "MODEL_DRIFT",
                "reason_code": "MODEL_CHANGED",
                "model_observed": "some-other-model",
                "tp": None,
                "fp": None,
                "fn": None,
                "severity_matches": None,
            }
        ),
        _review_row(
            {
                "status": "UNEXPECTED_BILLING",
                "reason_code": "REMOTE_FAILURE",
                "tp": None,
                "fp": None,
                "fn": None,
                "severity_matches": None,
            }
        ),
        _review_row(
            {
                "status": "UNEXPECTED_TOOL_OUTPUT",
                "reason_code": "PROHIBITED_TOOL",
                "tp": None,
                "fp": None,
                "fn": None,
                "severity_matches": None,
            }
        ),
    ]
    code, out = _run_report(monkeypatch, tmp_path, "\n".join(json.dumps(row) for row in rows) + "\n", capsys)
    assert code == 0
    kilo = json.loads(out)["providers"]["kilo"]
    assert kilo["attempts"] == 3
    assert kilo["valid_scored_cases"] == 0
    assert kilo["model_drift"] == 1
    assert kilo["billing_or_tool_failures"] == 2
    assert kilo["coverage"] == 0.0
    assert kilo["micro_f1"] is None


def test_report_rejects_old_eleven_field_review_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = {
        "schema_version": 1,
        "provider_id": "kilo",
        "model_requested": _KILO_MODEL,
        "model_observed": _KILO_MODEL,
        "fixture_id": "review_r01",
        "observed_at_utc": "2026-11-17T00:00:00Z",
        "status": "PASS",
        "tp": 2,
        "fp": 0,
        "fn": 0,
        "severity_matches": 2,
    }
    code, out = _run_report(monkeypatch, tmp_path, json.dumps(old) + "\n", capsys)
    assert code == 5
    assert "INVALID_LOG" in out


def test_e2e_review_model_drift_is_recorded_without_a_score(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _temp_runs(monkeypatch, tmp_path)
    content = _findings((8, "SECRET_LOGGING", "P1"))
    code, record, _ = _probe(monkeypatch, capsys, fixture="review_r06", content=content, model="some-other-model")
    assert code == 4
    assert record["status"] == "MODEL_DRIFT"
    assert runner.main(["review-report"]) == 0
    kilo = json.loads(capsys.readouterr().out)["providers"]["kilo"]
    assert kilo["attempts"] == 1
    assert kilo["valid_scored_cases"] == 0
    assert kilo["model_drift"] == 1
    assert kilo["coverage"] == 0.0


def test_e2e_review_non_zero_cost_is_not_scored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _temp_runs(monkeypatch, tmp_path)
    content = _findings((8, "SECRET_LOGGING", "P1"))
    code, record, _ = _probe(monkeypatch, capsys, fixture="review_r06", content=content, cost=0.01)
    assert code == 4
    assert record["status"] == "UNEXPECTED_BILLING"
    assert runner.main(["review-report"]) == 0
    kilo = json.loads(capsys.readouterr().out)["providers"]["kilo"]
    assert kilo["attempts"] == 1
    assert kilo["valid_scored_cases"] == 0
    assert kilo["billing_or_tool_failures"] == 1
    assert kilo["coverage"] == 0.0


def _numbered(fixture_id: str) -> dict[int, str]:
    fixture = fixtures.require_fixture(fixture_id)
    assert fixture.prompt is not None
    numbered: dict[int, str] = {}
    for line in fixture.prompt.splitlines():
        prefix, separator, code = line.partition(": ")
        if separator and prefix.isdigit():
            numbered[int(prefix)] = code
    return numbered


def test_r08_lease_epoch_must_match_exactly_before_commit() -> None:
    code = _numbered("review_r08")
    assert "worker_epoch != run.lease_epoch" in code[2]
    assert code[3].strip() == 'return "STALE"'
    commit_lines = [line for line, text in code.items() if "run.committed = True" in text]
    assert commit_lines == [4]
    for line, text in code.items():
        if "lease_epoch" in text and line != 2:
            assert "worker_epoch" not in text
    assert "< run.lease_epoch" not in "\n".join(code.values())
    assert "> run.lease_epoch" not in "\n".join(code.values())
    assert REVIEW_KEY["review_r08"] == ()


def test_r06_lease_check_is_correct_and_only_secret_logging_is_gold() -> None:
    code = _numbered("review_r06")
    assert "worker_epoch != run.lease_epoch" in code[2]
    assert code[3].strip() == 'return "STALE"'
    assert [line for line, text in code.items() if "run.committed = True" in text] == [4]
    assert 'headers["Authorization"]' in code[7]
    assert "print(token)" in code[8]
    key = REVIEW_KEY["review_r06"]
    assert len(key) == 1
    assert (key[0].line, key[0].rule_id, key[0].severity) == (8, "SECRET_LOGGING", "P1")
    assert "STALE_LEASE" not in {defect.rule_id for defect in key}


def test_r03_has_unvalidated_commit_and_automatic_retry() -> None:
    code = _numbered("review_r03")
    assert "current_epoch" in code[1]
    assert "worker_epoch" in code[1]
    assert "store.commit(result)" in code[2]
    assert "current_epoch" not in code[2]
    assert "worker_epoch" not in code[2]
    assert "==" not in code[2]
    assert "!=" not in code[2]
    assert "except TimeoutError:" in code[7]
    assert code[8].strip() == "return send()"
    key = {defect.line: defect.rule_id for defect in REVIEW_KEY["review_r03"]}
    assert key == {2: "STALE_LEASE", 8: "UNSAFE_RETRY"}
    prompt = fixtures.require_fixture("review_r03").prompt
    assert prompt is not None
    assert "current_epoch equals worker_epoch" in prompt
    assert "no idempotency key" in prompt
    assert "outcome is unknown" in prompt
    assert "must be rejected" not in prompt
    assert "must not be retried" not in prompt
    assert "do not retry" not in prompt.lower()


def test_r03_fixed_eight_line_snippet_is_exact() -> None:
    expected = """def commit_result(current_epoch, worker_epoch, store, result):
    store.commit(result)

def request_once(send):
    try:
        return send()
    except TimeoutError:
        return send()"""
    assert expected == fixtures.REVIEW_R03_CODE
    assert len(fixtures.REVIEW_R03_CODE.splitlines()) == 8


def test_r01_secret_premise_is_explicit() -> None:
    code = _numbered("review_r01")
    assert "items[len(items)]" in code[2]
    assert 'headers["Authorization"]' in code[4]
    assert 'print("token=" + token)' in code[5]
    prompt = fixtures.require_fixture("review_r01").prompt
    assert prompt is not None
    assert "shared log" in prompt
    assert "secret Authorization token" in prompt
    key = {(defect.line, defect.rule_id) for defect in REVIEW_KEY["review_r01"]}
    assert key == {(2, "INDEX_OUT_OF_RANGE"), (5, "SECRET_LOGGING")}


_GOLD_LINE_SNIPPETS = {
    ("review_r01", 2): "items[len(items)]",
    ("review_r01", 5): 'print("token=" + token)',
    ("review_r02", 2): "SELECT * FROM users WHERE name = ",
    ("review_r02", 6): "return age > 18",
    ("review_r03", 2): "store.commit(result)",
    ("review_r03", 8): "return send()",
    ("review_r06", 8): "print(token)",
    ("review_r07", 5): "return send()",
}


def test_each_gold_line_contains_the_expected_defect_code() -> None:
    assert set(_GOLD_LINE_SNIPPETS) == {
        (fixture_id, defect.line) for fixture_id, key in REVIEW_KEY.items() for defect in key
    }
    for (fixture_id, line), snippet in _GOLD_LINE_SNIPPETS.items():
        code = _numbered(fixture_id)
        assert snippet in code[line]


def test_clean_cases_have_no_defect_patterns() -> None:
    for fixture_id in ("review_r04", "review_r05", "review_r08"):
        assert REVIEW_KEY[fixture_id] == ()
        code = _numbered(fixture_id)
        text = "\n".join(code.values())
        assert "items[len(items)]" not in text
        assert "WHERE name = '" not in text
        assert 'print("token=" + token)' not in text
        assert "print(token)" not in text
        assert "return age > 18" not in text
        assert "store.commit(result)" not in text
        assert "run.commit(worker_epoch)" not in text
        except_lines = [line for line, value in code.items() if "except TimeoutError:" in value]
        for line in except_lines:
            following = code.get(line + 1, "")
            assert "send()" not in following


def test_gold_findings_are_not_false_positives() -> None:
    for fixture_id, key in REVIEW_KEY.items():
        findings = tuple(review.ReviewFinding(defect.line, defect.rule_id, defect.severity) for defect in key)
        score = review.score_review(fixture_id, findings)
        assert (score.tp, score.fp, score.fn) == (len(key), 0, 0)
        assert score.severity_matches == len(key)


def test_report_reads_probe_rows_written_by_review_tasks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _temp_runs(monkeypatch, tmp_path)
    _probe(
        monkeypatch,
        capsys,
        fixture="review_r01",
        content=_findings((2, "INDEX_OUT_OF_RANGE", "P2"), (5, "SECRET_LOGGING", "P1")),
    )
    assert runner.main(["report"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["records"] == 1
    kilo = report["providers"]["kilo"]
    assert kilo["attempts"] == 1
    assert kilo["evaluated"] == 1
    assert kilo["pass"] == 1
    assert kilo["local_preflight_skips"] == 0
