"""STEP 3: synthetic code-review tasks, strict JSON parsing and offline scoring."""

from __future__ import annotations

import ast
import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import httpx
import pytest

from devtools.free_ai import catalog, fixtures, review, runner, transport
from devtools.free_ai.review_key import (
    MAX_REVIEW_FINDINGS,
    MAX_REVIEW_LINE,
    REVIEW_FIXTURE_IDS,
    REVIEW_KEY,
    REVIEW_KINDS,
    REVIEW_SEVERITIES,
)

_REAL_NEW_CLIENT = transport._new_client
_KILO_MODEL = "nvidia/nemotron-3.5-super-vl:free"
_OVH_MODEL = "gpt-oss-120b"


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


def _probe_argv(*extra: str, provider: str, fixture: str) -> list[str]:
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


def _ok(content: str, *, model: str = _KILO_MODEL) -> Callable[[httpx.Request], httpx.Response]:
    payload = _chat_body(content, model=model)
    return lambda _request: httpx.Response(200, headers={"content-type": "application/json"}, content=payload)


def _findings(*items: tuple[int, str, str]) -> str:
    return json.dumps(
        {"findings": [{"line": line, "kind": kind, "severity": severity} for line, kind, severity in items]}
    )


def _last(out: str) -> dict[str, object]:
    lines = [line for line in out.splitlines() if line.startswith("{")]
    assert lines
    parsed = json.loads(lines[-1])
    assert isinstance(parsed, dict)
    return parsed


# --- task and answer-key structure -------------------------------------------------------


def test_review_tasks_and_key_are_complete_and_independent() -> None:
    assert REVIEW_FIXTURE_IDS == (
        "review_01",
        "review_02",
        "review_03",
        "review_04",
        "review_05",
        "review_06",
        "review_07",
        "review_08",
    )
    assert set(REVIEW_KEY) == set(REVIEW_FIXTURE_IDS)
    assert sum(len(key) for key in REVIEW_KEY.values()) == 9
    assert sum(1 for key in REVIEW_KEY.values() if not key) == 3

    for fixture_id, key in REVIEW_KEY.items():
        fixture = fixtures.require_fixture(fixture_id)
        assert fixture.review_task is True
        assert fixture.wire_kind == "chat_completions"
        assert fixture.prompt is not None
        assert fixture.expected_answer is None
        assert fixture.allowed_answers == ()
        lines = [line for line in fixture.prompt.splitlines() if line[:1].isdigit() and ": " in line]
        code_line_count = len([line for line in fixture.prompt.splitlines() if line and line[0].isdigit()])
        for defect in key:
            assert 1 <= defect.line <= code_line_count
            assert defect.kind in REVIEW_KINDS
            assert defect.severity in REVIEW_SEVERITIES
        assert lines
        # The prompt may show the JSON template, but never a concrete answer triple.
        assert '"line":<' in fixture.prompt
        assert '"line":2' not in fixture.prompt
        assert '"line": 2' not in fixture.prompt
        assert "ground truth" not in fixture.prompt.lower()
        assert "review_key" not in fixture.prompt


def test_fixtures_module_never_imports_the_answer_key() -> None:
    source = Path(fixtures.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any("review_key" in name for name in imported)


def test_review_fixtures_are_chat_only_and_never_legacy() -> None:
    kilo = catalog.require_spec("kilo")
    ovh = catalog.require_spec("ovh")
    for fixture_id in REVIEW_FIXTURE_IDS:
        assert fixture_id in fixtures.allowed_fixtures(kilo)
        assert fixture_id in fixtures.allowed_fixtures(ovh)
        assert fixture_id not in fixtures.allowed_fixtures(catalog.require_spec("jev_legacy"))
        assert fixture_id not in fixtures.allowed_fixtures(catalog.require_spec("pollinations_legacy"))


def test_review_prompt_payload_contains_only_the_fixed_prompt() -> None:
    kilo = catalog.require_spec("kilo")
    payload = fixtures.build_payload(kilo, "review_02")
    messages = payload["messages"]
    assert isinstance(messages, list) and len(messages) == 1
    assert messages[0]["content"] == fixtures.REVIEW_02_PROMPT


# --- strict parser -----------------------------------------------------------------------


def test_parser_accepts_empty_and_multi_findings() -> None:
    assert review.parse_review_findings('{"findings":[]}') == ()
    parsed = review.parse_review_findings(
        '{"findings":[{"line":2,"kind":"SQL_INJECTION","severity":"high"},'
        '{"line":3,"kind":"SECRET_LOGGING","severity":"high"}]}'
    )
    assert [(item.line, item.kind, item.severity) for item in parsed] == [
        (2, "SQL_INJECTION", "high"),
        (3, "SECRET_LOGGING", "high"),
    ]


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
        '{"findings":[{"line":2,"kind":"SQL_INJECTION"}]}',
        '{"findings":[{"line":2,"kind":"SQL_INJECTION","severity":"high","extra":1}]}',
        '{"findings":[{"line":true,"kind":"SQL_INJECTION","severity":"high"}]}',
        '{"findings":[{"line":"2","kind":"SQL_INJECTION","severity":"high"}]}',
        '{"findings":[{"line":0,"kind":"SQL_INJECTION","severity":"high"}]}',
        '{"findings":[{"line":-1,"kind":"SQL_INJECTION","severity":"high"}]}',
        f'{{"findings":[{{"line":{MAX_REVIEW_LINE + 1},"kind":"SQL_INJECTION","severity":"high"}}]}}',
        '{"findings":[{"line":2,"kind":"BUG","severity":"high"}]}',
        '{"findings":[{"line":2,"kind":"SQL_INJECTION","severity":"critical"}]}',
        '{"findings":[],"findings":[]}',
        '{"findings":[{"line":NaN,"kind":"SQL_INJECTION","severity":"high"}]}',
        "{"
        + '"findings":['
        + ",".join(['{"line":2,"kind":"SQL_INJECTION","severity":"high"}'] * (MAX_REVIEW_FINDINGS + 1))
        + "]}",
        '{"findings":[[]]}',
    ],
)
def test_parser_rejects_anything_but_the_exact_json_shape(content: str) -> None:
    with pytest.raises(review.ReviewFormatError) as excinfo:
        review.parse_review_findings(content)
    assert excinfo.value.reason_code in {"INVALID_JSON", "INVALID_SCHEMA"}


def test_parser_rejects_excessive_nesting() -> None:
    nested = '{"findings":[]}'[:-1] + ',"x":' + "[" * 20 + "]" * 20 + "}"
    with pytest.raises(review.ReviewFormatError):
        review.parse_review_findings(nested)


# --- scoring -----------------------------------------------------------------------------


def _score(fixture_id: str, *items: tuple[int, str, str]) -> review.ReviewScore:
    findings = tuple(review.ReviewFinding(*item) for item in items)  # type: ignore[arg-type]
    return review.score_review(fixture_id, findings)


def test_scoring_perfect_multi_defect_answer() -> None:
    score = _score(
        "review_07",
        (2, "MISSING_VALIDATION", "medium"),
        (4, "SQL_INJECTION", "high"),
        (7, "SECRET_LOGGING", "high"),
    )
    assert (score.tp, score.fp, score.fn) == (3, 0, 0)
    assert score.severity_matches == 3
    assert (score.precision, score.recall, score.f1) == (1.0, 1.0, 1.0)
    assert score.clean_task is False
    assert score.clean_pass is False


def test_scoring_location_or_kind_mismatch_counts_fp_and_fn() -> None:
    score = _score("review_01", (3, "INDEX_OUT_OF_RANGE", "high"))
    assert (score.tp, score.fp, score.fn) == (0, 1, 1)
    assert (score.precision, score.recall) == (0.0, 0.0)
    assert score.f1 == 0.0

    kind = _score("review_01", (2, "BOUNDARY_BUG", "high"))
    assert (kind.tp, kind.fp, kind.fn) == (0, 1, 1)


def test_scoring_duplicate_prediction_is_a_false_positive() -> None:
    score = _score("review_01", (2, "INDEX_OUT_OF_RANGE", "high"), (2, "INDEX_OUT_OF_RANGE", "high"))
    assert (score.tp, score.fp, score.fn) == (1, 1, 0)
    assert score.precision == 0.5
    assert score.recall == 1.0
    assert score.f1 == 0.6667


def test_scoring_severity_is_separate_from_tp() -> None:
    score = _score("review_04", (2, "BOUNDARY_BUG", "low"))
    assert (score.tp, score.fp, score.fn) == (1, 0, 0)
    assert score.severity_matches == 0
    assert score.precision == 1.0
    assert score.recall == 1.0


def test_scoring_clean_code_and_false_positives() -> None:
    clean = _score("review_03")
    assert (clean.tp, clean.fp, clean.fn) == (0, 0, 0)
    assert clean.clean_task is True
    assert clean.clean_pass is True
    assert (clean.precision, clean.recall, clean.f1) == (None, None, None)

    noisy = _score("review_03", (3, "MISSING_VALIDATION", "low"))
    assert (noisy.tp, noisy.fp, noisy.fn) == (0, 1, 0)
    assert noisy.clean_task is True
    assert noisy.clean_pass is False
    assert noisy.precision == 0.0
    assert noisy.f1 == 0.0


# --- E2E through the CLI -----------------------------------------------------------------


def _run_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    fixture: str,
    content: str,
    provider: str = "kilo",
    model: str = _KILO_MODEL,
) -> tuple[int, dict[str, object], list[httpx.Request], Path]:
    runs = _temp_runs(monkeypatch, tmp_path)
    requests = _mock_transport(monkeypatch, _ok(content, model=model))
    code = runner.main(_probe_argv(provider=provider, fixture=fixture))
    record = _last(capsys.readouterr().out)
    return code, record, requests, runs


def test_e2e_perfect_review_is_pass_and_aggregated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    content = _findings((2, "SQL_INJECTION", "high"), (3, "SECRET_LOGGING", "high"))
    code, record, requests, runs = _run_probe(monkeypatch, tmp_path, capsys, fixture="review_02", content=content)
    assert code == 0
    assert record["status"] == "PASS"
    assert record["reason_code"] == "OK"
    assert len(requests) == 1

    probe_lines = (runs / "probes.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(probe_lines) == 1
    assert len(json.loads(probe_lines[0])) == 13
    score_lines = (runs / "review_scores.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(score_lines) == 1
    score_record = json.loads(score_lines[0])
    assert list(score_record) == list(runner.REVIEW_SCORE_FIELDS)
    assert score_record["tp"] == 2
    assert score_record["fp"] == 0
    assert score_record["fn"] == 0
    assert score_record["severity_matches"] == 2

    assert runner.main(["review-report"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["records"] == 1
    kilo = report["providers"]["kilo"]
    assert kilo == {
        "tasks": 1,
        "perfect_tasks": 1,
        "clean_tasks": 0,
        "clean_passes": 0,
        "tp": 2,
        "fp": 0,
        "fn": 0,
        "precision": 1.0,
        "recall": 1.0,
        "f1": 1.0,
        "severity_matches": 2,
        "severity_accuracy": 1.0,
    }
    assert report["providers"]["ovh"]["tasks"] == 0
    for forbidden in ("last_item", "SELECT", "synthetic-token"):
        assert forbidden not in json.dumps(report)


def test_e2e_partial_review_is_content_mismatch_with_tp_fp_fn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, record, _, _ = _run_probe(
        monkeypatch,
        tmp_path,
        capsys,
        fixture="review_07",
        content=_findings((4, "SQL_INJECTION", "high")),
    )
    assert code == 4
    assert record["status"] == "CONTENT_MISMATCH"
    assert runner.main(["review-report"]) == 0
    report = json.loads(capsys.readouterr().out)
    kilo = report["providers"]["kilo"]
    assert (kilo["tp"], kilo["fp"], kilo["fn"]) == (1, 0, 2)
    assert kilo["precision"] == 1.0
    assert kilo["recall"] == 0.3333
    assert kilo["f1"] == 0.5
    assert kilo["perfect_tasks"] == 0


def test_e2e_wrong_location_is_false_positive_and_false_negative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, record, _, _ = _run_probe(
        monkeypatch,
        tmp_path,
        capsys,
        fixture="review_01",
        content=_findings((1, "INDEX_OUT_OF_RANGE", "high")),
    )
    assert code == 4
    assert record["status"] == "CONTENT_MISMATCH"
    assert runner.main(["review-report"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["providers"]["kilo"]["tp"] == 0
    assert report["providers"]["kilo"]["fp"] == 1
    assert report["providers"]["kilo"]["fn"] == 1


def test_e2e_clean_code_with_no_findings_is_clean_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, record, _, _ = _run_probe(monkeypatch, tmp_path, capsys, fixture="review_03", content=_findings())
    assert code == 0
    assert record["status"] == "PASS"
    assert runner.main(["review-report"]) == 0
    report = json.loads(capsys.readouterr().out)
    kilo = report["providers"]["kilo"]
    assert kilo["clean_tasks"] == 1
    assert kilo["clean_passes"] == 1
    assert kilo["precision"] is None
    assert kilo["recall"] is None


def test_e2e_clean_code_with_spurious_finding_is_fp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, record, _, _ = _run_probe(
        monkeypatch,
        tmp_path,
        capsys,
        fixture="review_05",
        content=_findings((4, "SQL_INJECTION", "high")),
    )
    assert code == 4
    assert record["status"] == "CONTENT_MISMATCH"
    assert runner.main(["review-report"]) == 0
    report = json.loads(capsys.readouterr().out)
    kilo = report["providers"]["kilo"]
    assert kilo["clean_tasks"] == 1
    assert kilo["clean_passes"] == 0
    assert kilo["fp"] == 1
    assert kilo["precision"] == 0.0


def test_e2e_malformed_review_answer_is_format_mismatch_without_score(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, record, _, runs = _run_probe(
        monkeypatch,
        tmp_path,
        capsys,
        fixture="review_01",
        content="The code has an off-by-one defect.",
    )
    assert code == 4
    assert record["status"] == "FORMAT_MISMATCH"
    assert record["reason_code"] == "INVALID_JSON"
    assert not (runs / "review_scores.jsonl").exists()
    assert runner.main(["review-report"]) == 0
    assert json.loads(capsys.readouterr().out)["records"] == 0


def test_e2e_review_metrics_are_per_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _temp_runs(monkeypatch, tmp_path)
    _mock_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=_chat_body(
                _findings((2, "INDEX_OUT_OF_RANGE", "high")),
                model=_OVH_MODEL if b"gpt-oss" in request.content else _KILO_MODEL,
            ),
        ),
    )
    assert runner.main(_probe_argv(provider="kilo", fixture="review_01")) == 0
    capsys.readouterr()
    assert runner.main(_probe_argv(provider="ovh", fixture="review_04")) == 4
    capsys.readouterr()

    assert runner.main(["review-report"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["providers"]["kilo"]["tasks"] == 1
    assert report["providers"]["kilo"]["f1"] == 1.0
    assert report["providers"]["ovh"]["tasks"] == 1
    assert report["providers"]["ovh"]["fn"] == 1
    assert report["providers"]["ovh"]["precision"] == 0.0


# --- review-report validation ------------------------------------------------------------


def _review_record(overrides: dict[str, object] | None = None) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 1,
        "provider_id": "kilo",
        "model_requested": _KILO_MODEL,
        "model_observed": _KILO_MODEL,
        "fixture_id": "review_02",
        "observed_at_utc": "2026-11-17T00:00:00Z",
        "status": "PASS",
        "tp": 2,
        "fp": 0,
        "fn": 0,
        "severity_matches": 2,
    }
    if overrides:
        record.update(overrides)
    return record


def _run_review_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    content: str,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    runs = _temp_runs(monkeypatch, tmp_path)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "review_scores.jsonl").write_text(content, encoding="utf-8")
    return runner.main(["review-report"]), capsys.readouterr().out


def test_review_report_missing_log_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _temp_runs(monkeypatch, tmp_path)
    assert runner.main(["review-report"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["records"] == 0
    assert report["log_path"] == runner.REVIEW_LOG_PATH
    assert report["providers"]["kilo"]["precision"] is None


_INVALID_REVIEW_LOGS = [
    "{not json",
    json.dumps(_review_record({**{"unexpected": 1}})),
    json.dumps(_review_record({"provider_id": "not-a-provider"})),
    json.dumps(_review_record({"provider_id": "jev_legacy", "model_requested": "jev-1.13-free"})),
    json.dumps(_review_record({"fixture_id": "review_99"})),
    json.dumps(_review_record({"fixture_id": "dev_t01"})),
    json.dumps(_review_record({"model_requested": "wrong-model"})),
    json.dumps(_review_record({"tp": 1, "fn": 0})),
    json.dumps(_review_record({"tp": 1, "fp": 0, "fn": 1})),
    json.dumps(_review_record({"severity_matches": 3})),
    json.dumps(_review_record({"tp": 2, "fp": 0, "fn": 0, "status": "CONTENT_MISMATCH"})),
    json.dumps(_review_record({"status": "PASS", "fp": 1, "tp": 1})),
    json.dumps(_review_record({"tp": True})),
    json.dumps(_review_record({"tp": -1})),
    json.dumps(_review_record({"schema_version": 2})),
    json.dumps(_review_record()) + "\n\n" + json.dumps(_review_record()),
]


@pytest.mark.parametrize("content", _INVALID_REVIEW_LOGS)
def test_review_report_rejects_invalid_score_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    content: str,
) -> None:
    code, out = _run_review_report(monkeypatch, tmp_path, content, capsys)
    assert code == 5
    assert json.loads(out) == {"schema_version": 1, "status": "INVALID_LOG", "reason_code": "INVALID_SCHEMA"}


def test_review_report_accepts_multiple_tasks_and_ignores_no_code_like_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    records = [
        _review_record(),
        _review_record(
            {
                "fixture_id": "review_03",
                "status": "PASS",
                "tp": 0,
                "fp": 0,
                "fn": 0,
                "severity_matches": 0,
            }
        ),
        _review_record(
            {
                "fixture_id": "review_07",
                "status": "CONTENT_MISMATCH",
                "tp": 1,
                "fp": 0,
                "fn": 2,
                "severity_matches": 1,
            }
        ),
    ]
    code, out = _run_review_report(
        monkeypatch, tmp_path, "\n".join(json.dumps(item) for item in records) + "\n", capsys
    )
    assert code == 0
    report = json.loads(out)
    assert report["records"] == 3
    kilo = report["providers"]["kilo"]
    assert kilo["tasks"] == 3
    assert kilo["perfect_tasks"] == 2
    assert kilo["clean_tasks"] == 1
    assert kilo["clean_passes"] == 1
    assert (kilo["tp"], kilo["fp"], kilo["fn"]) == (3, 0, 2)
    assert kilo["precision"] == 1.0
    assert kilo["recall"] == 0.6
    assert kilo["severity_accuracy"] == 1.0
    for forbidden in ("items[len(items)]", "SELECT", "Authorization", "ground"):
        assert forbidden not in out


def test_review_log_has_hard_limits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    too_many = "\n".join(json.dumps(_review_record()) for _ in range(5001)) + "\n"
    code, out = _run_review_report(monkeypatch, tmp_path, too_many, capsys)
    assert code == 5
    assert "INVALID_LOG" in out
