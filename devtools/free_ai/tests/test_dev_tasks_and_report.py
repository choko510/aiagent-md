"""STEP 2: synthetic development tasks, deterministic scoring, and the offline report."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import httpx
import pytest

from devtools.free_ai import catalog, fixtures, runner, transport

_REAL_NEW_CLIENT = transport._new_client
_KILO = catalog.require_spec("kilo")
_OVH = catalog.require_spec("ovh")
_KILO_MODEL = "nvidia/nemotron-3.5-super-vl:free"
_OVH_MODEL = "gpt-oss-120b"

_DEV_EXPECTATIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "dev_t01": ("INDEX_OUT_OF_RANGE", ("INDEX_OUT_OF_RANGE", "TYPE_ERROR", "NO_BUG")),
    "dev_t02": ("SQL_INJECTION", ("SQL_INJECTION", "MEMORY_LEAK", "NO_BUG")),
    "dev_t03": ("DO_NOT_RETRY_AUTOMATICALLY", ("RETRY_IMMEDIATELY", "DO_NOT_RETRY_AUTOMATICALLY")),
    "dev_t04": ("SECRET_LOGGING", ("SECRET_LOGGING", "SQL_INJECTION", "NO_BUG")),
    "dev_t05": ("REJECT_STALE", ("COMMIT", "REJECT_STALE")),
    "dev_t06": ("BOUNDARY_BUG", ("BOUNDARY_BUG", "TYPE_ERROR", "NO_BUG")),
}


def _blocked(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError("offline test attempted to open a network socket")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)


def _mock_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(transport, "_new_client", lambda: _REAL_NEW_CLIENT(httpx.MockTransport(recording)))
    return requests


def _temp_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    runs = tmp_path / ".runs"
    monkeypatch.setattr(runner, "RUNS_DIR", runs)
    monkeypatch.setattr(runner, "QUOTA_FILE", runs / "quota.json")
    monkeypatch.setattr(runner, "LOCK_FILE", runs / "lock")
    monkeypatch.setattr(runner, "LOG_FILE", runs / "probes.jsonl")
    return runs


def _probe_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    provider: str = "kilo",
    fixture: str = "dev_t01",
) -> runner.ProbeOutcome:
    _temp_runs(monkeypatch, tmp_path)
    _mock_transport(monkeypatch, handler)
    args = argparse.Namespace(
        provider=provider,
        fixture=fixture,
        ack_synthetic_only=True,
        ack_external_request=True,
        allow_legacy=False,
    )
    return runner._probe(args)


def _chat_response(content: object, *, model: object = _KILO_MODEL, cost: object = 0) -> dict[str, object]:
    body: dict[str, object] = {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }
    if cost is not None:
        body["cost"] = cost
    return body


def _chat_text(content: object, *, model: object = _KILO_MODEL, cost: object = 0) -> bytes:
    return json.dumps({"model": model, "choices": [{"message": {"content": content}}], "cost": cost}).encode()


def test_dev_task_ids_answers_and_allowed_sets_are_fixed() -> None:
    assert tuple(_DEV_EXPECTATIONS) == fixtures.DEV_FIXTURE_IDS
    for fixture_id, (expected, allowed) in _DEV_EXPECTATIONS.items():
        fixture = fixtures.require_fixture(fixture_id)
        assert fixture.wire_kind == "chat_completions"
        assert fixture.expected_answer == expected
        assert fixture.allowed_answers == allowed
        assert expected in allowed
        assert fixture.expected_text is None
        assert fixture.noul_threshold is None
        assert fixture.prompt is not None
        assert "Allowed answers: " + ", ".join(allowed) in fixture.prompt
        assert "Reply with exactly one identifier" in fixture.prompt


def test_dev_tasks_are_built_only_from_synthetic_constants() -> None:
    prompts = {fixture_id: fixtures.require_fixture(fixture_id).prompt for fixture_id in fixtures.DEV_FIXTURE_IDS}
    assert prompts["dev_t01"] is not None and fixtures.DEV_T01_CODE in prompts["dev_t01"]
    assert prompts["dev_t02"] is not None and fixtures.DEV_T02_CODE in prompts["dev_t02"]
    assert prompts["dev_t04"] is not None and fixtures.DEV_T04_CODE in prompts["dev_t04"]
    assert prompts["dev_t06"] is not None and fixtures.DEV_T06_CODE in prompts["dev_t06"]
    assert "leaseEpoch of a Run is 5" in (prompts["dev_t05"] or "")
    assert "leaseEpoch 4" in (prompts["dev_t05"] or "")
    assert "leaseEpoch" in (prompts["dev_t03"] or "") or "connection" in (prompts["dev_t03"] or "")

    for fixture_id in fixtures.DEV_FIXTURE_IDS:
        prompt = fixtures.require_fixture(fixture_id).prompt or ""
        for forbidden in ("http://", "https://", "/home/", "C:", "@gmail", "password="):
            assert forbidden not in prompt
    assert "synthetic-example" in (prompts["dev_t04"] or "")

    assert list(inspect.signature(fixtures.build_payload).parameters) == ["spec", "fixture_id"]
    source = inspect.getsource(fixtures)
    for forbidden in ("input(", "sys.stdin", "os.environ", "subprocess", "urlopen", "requests."):
        assert forbidden not in source


def test_dev_tasks_are_allowed_only_for_the_official_chat_providers() -> None:
    for fixture_id in fixtures.DEV_FIXTURE_IDS:
        assert fixture_id in fixtures.allowed_fixtures(_KILO)
        assert fixture_id in fixtures.allowed_fixtures(_OVH)
        assert fixture_id not in fixtures.allowed_fixtures(catalog.require_spec("jev_legacy"))
        assert fixture_id not in fixtures.allowed_fixtures(catalog.require_spec("pollinations_legacy"))
        with pytest.raises(fixtures.FixtureError):
            fixtures.build_payload(catalog.require_spec("jev_legacy"), fixture_id)
        with pytest.raises(fixtures.FixtureError):
            fixtures.build_payload(catalog.require_spec("pollinations_legacy"), fixture_id)


@pytest.mark.parametrize(("fixture_id", "expected"), list(_DEV_EXPECTATIONS.items()))
def test_each_dev_task_passes_with_its_expected_identifier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixture_id: str, expected: tuple[str, tuple[str, ...]]
) -> None:
    correct = expected[0]
    outcome = _probe_outcome(
        monkeypatch,
        tmp_path,
        lambda _request: httpx.Response(200, headers={"content-type": "application/json"}, content=_chat_text(correct)),
        fixture=fixture_id,
    )
    assert outcome.record.status == "PASS"
    assert outcome.exit_code == 0
    assert outcome.record.model_observed == _KILO_MODEL


@pytest.mark.parametrize(("fixture_id", "expected"), list(_DEV_EXPECTATIONS.items()))
def test_each_dev_task_rejects_other_allowed_identifiers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixture_id: str, expected: tuple[str, tuple[str, ...]]
) -> None:
    correct, allowed = expected
    wrong = next(candidate for candidate in allowed if candidate != correct)
    outcome = _probe_outcome(
        monkeypatch,
        tmp_path,
        lambda _request: httpx.Response(200, headers={"content-type": "application/json"}, content=_chat_text(wrong)),
        fixture=fixture_id,
    )
    assert outcome.record.status == "CONTENT_MISMATCH"
    assert outcome.exit_code == 4


@pytest.mark.parametrize("answer", ["BANANA", "INDEX_OUT_OF_RANGE because off by one", "index_out_of_range", ""])
def test_non_allowed_or_prose_answers_are_a_format_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answer: str
) -> None:
    outcome = _probe_outcome(
        monkeypatch,
        tmp_path,
        lambda _request: httpx.Response(200, headers={"content-type": "application/json"}, content=_chat_text(answer)),
    )
    assert outcome.record.status == "FORMAT_MISMATCH"
    assert outcome.exit_code == 4


def test_missing_model_is_not_pass_for_a_dev_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    body = {"choices": [{"message": {"content": "INDEX_OUT_OF_RANGE"}}]}
    outcome = _probe_outcome(
        monkeypatch,
        tmp_path,
        lambda _request: httpx.Response(
            200, headers={"content-type": "application/json"}, content=json.dumps(body).encode()
        ),
    )
    assert outcome.record.status == "FORMAT_MISMATCH"
    assert outcome.record.model_observed is None
    assert outcome.exit_code == 4


def test_tool_calls_are_never_executed_for_a_dev_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    marker = tmp_path / "executed.txt"
    command = f"touch {marker}"
    body = {
        "model": _KILO_MODEL,
        "choices": [
            {
                "message": {
                    "content": "INDEX_OUT_OF_RANGE",
                    "tool_calls": [
                        {"id": "1", "type": "function", "function": {"name": "shell", "arguments": command}}
                    ],
                }
            }
        ],
    }
    outcome = _probe_outcome(
        monkeypatch,
        tmp_path,
        lambda _request: httpx.Response(
            200, headers={"content-type": "application/json"}, content=json.dumps(body).encode()
        ),
    )
    assert outcome.record.status == "UNEXPECTED_TOOL_OUTPUT"
    assert outcome.exit_code == 4
    assert not marker.exists()


def test_non_zero_cost_is_not_pass_for_a_dev_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    outcome = _probe_outcome(
        monkeypatch,
        tmp_path,
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(_chat_response("INDEX_OUT_OF_RANGE", cost=0.25)).encode(),
        ),
    )
    assert outcome.record.status == "UNEXPECTED_BILLING"
    assert outcome.exit_code == 4
    assert outcome.record.cost_reported_usd == 0.25


def test_dev_fixture_is_sent_only_through_the_fixed_transport_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(
            200, headers={"content-type": "application/json"}, content=_chat_text("INDEX_OUT_OF_RANGE")
        ),
    )
    assert list(inspect.signature(transport.post_json).parameters) == ["spec", "fixture_id"]
    result = asyncio.run(transport.post_json(_KILO, "dev_t01"))
    assert result.status == "OK"
    assert len(requests) == 1
    expected_body = transport.encode_body(fixtures.build_payload(_KILO, "dev_t01"))
    assert requests[0].content == expected_body
    assert b"last_item(items)" in expected_body
    assert b"Allowed answers: INDEX_OUT_OF_RANGE, TYPE_ERROR, NO_BUG" in expected_body


def _record(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": 1,
        "provider_id": "kilo",
        "model_requested": _KILO_MODEL,
        "model_observed": _KILO_MODEL,
        "fixture_id": "dev_t01",
        "observed_at_utc": "2026-11-17T00:00:00Z",
        "http_status": 200,
        "status": "PASS",
        "reason_code": "OK",
        "elapsed_ms": 100,
        "input_tokens": 10,
        "output_tokens": 2,
        "cost_reported_usd": 0.0,
    }
    base.update(overrides)
    return base


def _run_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    content: str,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    runs = _temp_runs(monkeypatch, tmp_path)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "probes.jsonl").write_text(content, encoding="utf-8")
    code = runner.main(["report"])
    return code, capsys.readouterr().out


def test_report_missing_or_empty_log_is_an_empty_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    assert runner.main(["report"]) == 0
    missing = json.loads(capsys.readouterr().out)
    assert missing["records"] == 0
    assert missing["log_path"] == runner.REPORT_LOG_PATH
    assert missing["providers"]["kilo"]["evaluated"] == 0
    assert missing["providers"]["kilo"]["pass_rate"] is None
    assert not runs.exists()

    code, out = _run_report(monkeypatch, tmp_path, "", capsys)
    assert code == 0
    assert json.loads(out)["records"] == 0


def test_report_separates_quality_from_transport_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = [
        _record(),
        _record(
            status="CONTENT_MISMATCH",
            reason_code="OUTPUT_MISMATCH",
            elapsed_ms=300,
            input_tokens=10,
            cost_reported_usd=None,
        ),
        _record(
            status="NETWORK_ERROR",
            reason_code="UNKNOWN_OUTCOME",
            http_status=None,
            model_observed=None,
            elapsed_ms=None,
            input_tokens=None,
            output_tokens=None,
            cost_reported_usd=None,
        ),
        _record(
            provider_id="ovh",
            model_requested=_OVH_MODEL,
            model_observed=_OVH_MODEL,
            status="PASS",
            reason_code="COST_UNKNOWN",
            elapsed_ms=200,
            input_tokens=5,
            output_tokens=1,
            cost_reported_usd=None,
        ),
        _record(
            provider_id="ovh",
            model_requested=_OVH_MODEL,
            model_observed=None,
            status="TIMEOUT",
            reason_code="UNKNOWN_OUTCOME",
            http_status=None,
            elapsed_ms=50,
            input_tokens=None,
            output_tokens=None,
            cost_reported_usd=None,
        ),
        _record(
            status="RATE_LIMITED",
            reason_code="RATE_LIMIT",
            http_status=None,
            model_observed=None,
            elapsed_ms=None,
            input_tokens=None,
            output_tokens=None,
            cost_reported_usd=None,
        ),
        _record(
            provider_id="jev_legacy",
            model_requested="jev-1.13-free",
            fixture_id="refund_noul",
            model_observed=None,
            status="UNVERIFIED_TERMS",
            reason_code="LEGACY_DISABLED",
            http_status=None,
            elapsed_ms=None,
            input_tokens=None,
            output_tokens=None,
            cost_reported_usd=None,
        ),
    ]
    code, out = _run_report(monkeypatch, tmp_path, "\n".join(json.dumps(line) for line in lines) + "\n", capsys)
    assert code == 0
    report = json.loads(out)
    assert report["records"] == 7
    kilo = report["providers"]["kilo"]
    assert kilo["attempts"] == 4
    assert kilo["evaluated"] == 2
    assert kilo["pass"] == 1
    assert kilo["content_mismatch"] == 1
    assert kilo["pass_rate"] == 0.5
    assert kilo["communication_failures"] == 1
    assert kilo["local_preflight_skips"] == 1
    assert kilo["median_elapsed_ms"] == 200.0
    assert kilo["input_tokens"] == 20
    assert kilo["output_tokens"] == 4
    assert kilo["token_records"] == 2
    assert kilo["cost_unreported"] == 3

    ovh = report["providers"]["ovh"]
    assert ovh["attempts"] == 2
    assert ovh["evaluated"] == 1
    assert ovh["pass"] == 1
    assert ovh["pass_rate"] == 1.0
    assert ovh["communication_failures"] == 1
    assert ovh["median_elapsed_ms"] == 125.0
    assert ovh["cost_unreported"] == 2

    assert report["providers"]["jev_legacy"]["attempts"] == 1
    assert report["providers"]["jev_legacy"]["evaluated"] == 0
    assert report["providers"]["jev_legacy"]["communication_failures"] == 0
    assert report["providers"]["jev_legacy"]["local_preflight_skips"] == 1
    assert report["providers"]["pollinations_legacy"]["attempts"] == 0
    assert report["providers"]["pollinations_legacy"]["pass_rate"] is None


_INVALID_LOGS = [
    "{not json",
    json.dumps(_record()) + "\n" + json.dumps(_record(), separators=(",", ":"))[:-1] + ',"status":"PASS"}',
    json.dumps(_record(cost_reported_usd=float("nan"))),
    json.dumps(_record(elapsed_ms=-1)),
    json.dumps({**_record(), "unexpected": 1}),
    json.dumps({key: value for key, value in _record().items() if key != "status"}),
    json.dumps(_record(status="NOT_A_STATUS")),
    json.dumps(_record(reason_code="NOT_A_REASON")),
    json.dumps(_record(provider_id="not-a-provider")),
    json.dumps(_record(fixture_id="not-a-fixture")),
    json.dumps(_record(elapsed_ms="100")),
    json.dumps(_record(cost_reported_usd=int("9" * 309))),
    json.dumps(_record()) + "\n\n" + json.dumps(_record()),
]


@pytest.mark.parametrize("content", _INVALID_LOGS)
def test_report_rejects_invalid_logs_with_exit_five(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    content: str,
) -> None:
    code, out = _run_report(monkeypatch, tmp_path, content, capsys)
    assert code == 5
    parsed = json.loads(out)
    assert parsed == {"schema_version": 1, "status": "INVALID_LOG", "reason_code": "INVALID_SCHEMA"}
    assert "not json" not in out


def test_report_rejects_non_finite_numbers_in_raw_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _record()
    raw = json.dumps(base).replace('"cost_reported_usd": 0.0', '"cost_reported_usd": Infinity')
    code, out = _run_report(monkeypatch, tmp_path, raw, capsys)
    assert code == 5
    assert "INVALID_LOG" in out


def test_report_enforces_the_byte_and_line_limits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    line = json.dumps(_record(), separators=(",", ":"))
    oversized = (line + "\n") * (runner.REPORT_MAX_BYTES // len(line) + 2)
    assert len(oversized.encode("utf-8")) > runner.REPORT_MAX_BYTES
    code, out = _run_report(monkeypatch, tmp_path, oversized, capsys)
    assert code == 5
    assert "INVALID_LOG" in out

    monkeypatch.setattr(runner, "REPORT_MAX_BYTES", 50_000_000)
    too_many = (line + "\n") * (runner.REPORT_MAX_LINES + 1)
    code, out = _run_report(monkeypatch, tmp_path, too_many, capsys)
    assert code == 5
    assert "INVALID_LOG" in out


def test_report_output_contains_no_prompt_response_or_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _run_report(monkeypatch, tmp_path, json.dumps(_record()) + "\n", capsys)
    assert code == 0
    for forbidden in ("Allowed answers", "last_item", "INDEX_OUT_OF_RANGE", "SYNTHETIC", "Authorization"):
        assert forbidden not in out


def test_failed_probe_output_stays_out_of_the_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    _mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(_chat_response("INDEX_OUT_OF_RANGE")).encode(),
        ),
    )
    namespace = argparse.Namespace(
        provider="kilo",
        fixture="dev_t01",
        ack_synthetic_only=True,
        ack_external_request=True,
        allow_legacy=False,
    )
    assert runner._probe(namespace).exit_code == 0
    capsys.readouterr()

    assert runner.main(["report"]) == 0
    out = capsys.readouterr().out
    report = json.loads(out)
    assert report["providers"]["kilo"]["pass"] == 1
    assert "Allowed answers" not in out
    assert "INDEX_OUT_OF_RANGE" not in out
    assert (runs / "probes.jsonl").exists()
    log = (runs / "probes.jsonl").read_text(encoding="utf-8")
    assert "Allowed answers" not in log
    assert "last_item" not in log
    assert "INDEX_OUT_OF_RANGE" not in log
    assert len(json.loads(log.splitlines()[0])) == 13


def test_report_accepts_valid_legacy_records() -> None:
    jev = _record(
        provider_id="jev_legacy",
        model_requested="jev-1.13-free",
        fixture_id="refund_noul",
        model_observed=None,
        status="PASS",
        reason_code="OK",
        http_status=200,
        cost_reported_usd=0.0,
    )
    alias = _record(
        provider_id="pollinations_legacy",
        model_requested="openai-fast",
        fixture_id="jp_hello",
        model_observed="openai-fast-2026-01",
        status="PASS",
        reason_code="OK",
        http_status=200,
        cost_reported_usd=0.0,
    )
    for record in (jev, alias):
        validated = runner._validated_record(json.dumps(record))
        assert validated["provider_id"] == record["provider_id"]


_SEMANTIC_INVALID = [
    _record(cost_reported_usd=0.5),
    _record(model_observed="some-other-model"),
    _record(status="MODEL_DRIFT", reason_code="MODEL_CHANGED", model_observed=_KILO_MODEL),
    _record(
        status="MODEL_DRIFT",
        reason_code="MODEL_CHANGED",
        provider_id="jev_legacy",
        model_requested="jev-1.13-free",
        fixture_id="refund_noul",
        model_observed="other",
    ),
    _record(status="UNEXPECTED_BILLING", reason_code="REMOTE_FAILURE", cost_reported_usd=0.0),
    _record(status="UNEXPECTED_BILLING", reason_code="REMOTE_FAILURE", cost_reported_usd=None),
    _record(reason_code="OUTPUT_MISMATCH"),
    _record(provider_id="jev_legacy", model_requested="jev-1.13-free", fixture_id="dev_t01"),
    _record(provider_id="pollinations_legacy", model_requested="openai-fast", fixture_id="dev_t02"),
    _record(http_status=429),
    _record(status="ACCESS_RESTRICTED", reason_code="AUTH_REQUIRED", http_status=200),
    _record(status="NETWORK_ERROR", reason_code="UNKNOWN_OUTCOME", http_status=200),
    _record(model_requested="some-other-model"),
    _record(status="SAFETY_BLOCKED", reason_code="UNEXPECTED_REDIRECT", http_status=200),
    _record(status="SAFETY_BLOCKED", reason_code="BODY_TOO_LARGE", http_status=None),
    _record(status="PASS", reason_code="COST_UNKNOWN", cost_reported_usd=0.0),
    _record(status="PASS", reason_code="OK", cost_reported_usd=None),
    _record(status="PASS", http_status=None),
    _record(status="RATE_LIMITED", reason_code="RATE_LIMIT", http_status=503),
    _record(status="REMOTE_ERROR", reason_code="REMOTE_FAILURE", http_status=200),
    _record(status="CONTENT_MISMATCH", reason_code="OUTPUT_MISMATCH", model_observed=None),
    _record(status="FORMAT_MISMATCH", reason_code="INVALID_JSON", cost_reported_usd=0.25),
]


@pytest.mark.parametrize("record", _SEMANTIC_INVALID)
def test_report_rejects_semantically_impossible_records(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    record: dict[str, object],
) -> None:
    code, out = _run_report(monkeypatch, tmp_path, json.dumps(record) + "\n", capsys)
    assert code == 5
    expected = {"schema_version": 1, "status": "INVALID_LOG", "reason_code": "INVALID_SCHEMA"}
    assert json.loads(out) == expected


@pytest.mark.parametrize(
    "record",
    [
        _record(
            status="SAFETY_BLOCKED",
            reason_code="UNEXPECTED_REDIRECT",
            http_status=302,
            model_observed=None,
            cost_reported_usd=None,
        ),
        _record(
            status="SAFETY_BLOCKED",
            reason_code="BODY_TOO_LARGE",
            http_status=200,
            model_observed=None,
            cost_reported_usd=None,
        ),
        _record(
            provider_id="ovh",
            model_requested=_OVH_MODEL,
            model_observed=None,
            status="REMOTE_ERROR",
            reason_code="REMOTE_FAILURE",
            http_status=503,
            cost_reported_usd=None,
        ),
        _record(
            status="FORMAT_MISMATCH",
            reason_code="INVALID_JSON",
            http_status=None,
            model_observed=None,
            cost_reported_usd=None,
        ),
    ],
)
def test_report_accepts_valid_transport_failure_shapes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    record: dict[str, object],
) -> None:
    code, out = _run_report(monkeypatch, tmp_path, json.dumps(record) + "\n", capsys)
    assert code == 0
    assert json.loads(out)["records"] == 1
