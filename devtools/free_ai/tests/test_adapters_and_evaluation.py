"""FA-T18..T24, T26..T28 and T36: normalization, scoring, and non-execution. Offline."""

from __future__ import annotations

import argparse
import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import httpx
import pytest

from devtools.free_ai import adapters, catalog, fixtures, runner, transport

_REAL_NEW_CLIENT = transport._new_client
_KILO = catalog.require_spec("kilo")
_OVH = catalog.require_spec("ovh")
_JEV = catalog.require_spec("jev_legacy")
_POLLINATIONS = catalog.require_spec("pollinations_legacy")

_KILO_MODEL = "nvidia/nemotron-3.5-super-vl:free"


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


def _probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    provider: str = "kilo",
    fixture: str = "jp_hello",
) -> tuple[runner.ProbeOutcome, list[httpx.Request], Path]:
    runs = _temp_runs(monkeypatch, tmp_path)
    requests = _mock_transport(monkeypatch, handler)
    args = argparse.Namespace(
        provider=provider,
        fixture=fixture,
        ack_synthetic_only=True,
        ack_external_request=True,
        allow_legacy=False,
    )
    return runner._probe(args), requests, runs


def _json_response(body: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, headers={"content-type": "application/json"}, content=json.dumps(body).encode())


def _transport_result(body: bytes) -> transport.TransportResult:
    return transport.TransportResult(status="OK", reason_code="OK", http_status=200, body=body, elapsed_ms=1)


def _evaluate(spec: catalog.ProviderSpec, fixture_id: str, body: bytes) -> runner.Evaluation:
    return runner._evaluate(spec, fixtures.require_fixture(fixture_id), _transport_result(body))


def test_fa_t18_chat_jp_hello_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    outcome, requests, runs = _probe(
        monkeypatch,
        tmp_path,
        lambda _request: _json_response(
            {
                "model": _KILO_MODEL,
                "choices": [{"message": {"role": "assistant", "content": "こんにちは"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                "cost": 0,
            }
        ),
    )
    record = outcome.record
    assert outcome.exit_code == 0
    assert record.status == "PASS"
    assert record.reason_code == "OK"
    assert record.model_requested == _KILO_MODEL
    assert record.model_observed == _KILO_MODEL
    assert record.fixture_id == "jp_hello"
    assert record.http_status == 200
    assert record.input_tokens == 10
    assert record.output_tokens == 2
    assert record.cost_reported_usd == 0.0
    assert len(requests) == 1
    assert requests[0].content == transport.encode_body(fixtures.build_payload(_KILO, "jp_hello"))
    assert (runs / "probes.jsonl").exists()


def test_fa_t19_chat_arithmetic_requires_exactly_four(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    outcome, _requests, _runs = _probe(
        monkeypatch,
        tmp_path,
        lambda _request: _json_response(
            {
                "model": "gpt-oss-120b",
                "choices": [{"message": {"role": "assistant", "content": "4"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 15, "completion_tokens": 3},
            }
        ),
        provider="ovh",
        fixture="arithmetic_2plus2",
    )
    assert outcome.exit_code == 0
    assert outcome.record.status == "PASS"
    assert outcome.record.reason_code == "COST_UNKNOWN"
    assert outcome.record.cost_reported_usd is None

    explained = _evaluate(
        _OVH,
        "arithmetic_2plus2",
        json.dumps(
            {
                "model": "gpt-oss-120b",
                "choices": [{"message": {"content": "2+2 equals 4"}}],
            }
        ).encode(),
    )
    assert explained.status == "CONTENT_MISMATCH"
    assert explained.reason_code == "OUTPUT_MISMATCH"
    assert explained.exit_code == 4


def test_fa_t20_reasoning_only_answer_is_not_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret_reasoning = "内部推論のダミー " * 50
    outcome, _requests, runs = _probe(
        monkeypatch,
        tmp_path,
        lambda _request: _json_response(
            {
                "model": _KILO_MODEL,
                "choices": [{"message": {"role": "assistant", "content": "", "reasoning": secret_reasoning}}],
            }
        ),
    )
    assert outcome.record.status == "FORMAT_MISMATCH"
    assert outcome.exit_code == 4
    assert outcome.record.model_observed is None
    log = (runs / "probes.jsonl").read_text(encoding="utf-8")
    assert "内部推論のダミー" not in log
    assert "reasoning" not in log
    capsys.readouterr()


def test_fa_t21_tool_calls_are_never_executed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    marker = tmp_path / "pwned.txt"
    command = f"touch {marker}"
    outcome, _requests, runs = _probe(
        monkeypatch,
        tmp_path,
        lambda _request: _json_response(
            {
                "model": _KILO_MODEL,
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "4",
                            "tool_calls": [
                                {"id": "1", "type": "function", "function": {"name": "shell", "arguments": command}}
                            ],
                        }
                    }
                ],
            }
        ),
        fixture="arithmetic_2plus2",
    )
    assert outcome.record.status == "UNEXPECTED_TOOL_OUTPUT"
    assert outcome.record.reason_code == "PROHIBITED_TOOL"
    assert outcome.exit_code == 4
    assert not marker.exists()
    assert command not in (runs / "probes.jsonl").read_text(encoding="utf-8")


def test_fa_t22_model_drift_is_rejected_for_official_services(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    outcome, _requests, _runs = _probe(
        monkeypatch,
        tmp_path,
        lambda _request: _json_response(
            {"model": "some-other-model", "choices": [{"message": {"content": "こんにちは"}}], "cost": 0}
        ),
    )
    assert outcome.record.status == "MODEL_DRIFT"
    assert outcome.record.reason_code == "MODEL_CHANGED"
    assert outcome.record.model_observed == "some-other-model"
    assert outcome.exit_code == 4

    alias = _evaluate(
        _POLLINATIONS,
        "jp_hello",
        json.dumps({"model": "openai-fast-2026-01", "choices": [{"message": {"content": "こんにちは"}}]}).encode(),
    )
    assert alias.status == "PASS"
    assert alias.normalized is not None
    assert alias.normalized.model_observed == "openai-fast-2026-01"


def test_fa_t23_system_one_noul_passes() -> None:
    body = (
        b'{"model":"jev-1.13-free","answers":{"refund":{"type":"noul","noul":0.93}},'
        b'"usage":{"input_tokens":30,"output_tokens":4},"cost":0}'
    )
    evaluation = _evaluate(_JEV, "refund_noul", body)
    assert evaluation.status == "PASS"
    assert evaluation.reason_code == "OK"
    assert evaluation.exit_code == 0
    assert evaluation.normalized is not None
    assert evaluation.normalized.noul == pytest.approx(0.93)
    assert evaluation.normalized.input_tokens == 30
    assert evaluation.normalized.output_tokens == 4

    low = _evaluate(_JEV, "refund_noul", b'{"answers":{"refund":{"type":"noul","noul":0.49}}}')
    assert low.status == "CONTENT_MISMATCH"


@pytest.mark.parametrize(
    "body",
    [
        b'{"answers":{"refund":{"type":"noul","noul":-0.01}}}',
        b'{"answers":{"refund":{"type":"noul","noul":1.01}}}',
        b'{"answers":{"refund":{"type":"noul","noul":"1"}}}',
        b'{"answers":{"refund":{"type":"noul","noul":NaN}}}',
        b'{"answers":{"refund":{"type":"noul"}}}',
        b'{"answers":{"refund":{"type":"bool","noul":1}}}',
        b'{"answers":{}}',
    ],
)
def test_fa_t24_invalid_noul_is_a_format_mismatch(body: bytes) -> None:
    evaluation = _evaluate(_JEV, "refund_noul", body)
    assert evaluation.status == "FORMAT_MISMATCH"
    assert evaluation.exit_code == 4
    assert evaluation.normalized is None


@pytest.mark.parametrize(
    ("spec", "fixture_id", "body"),
    [
        (_KILO, "jp_hello", '{"choices":[]}'),
        (_KILO, "jp_hello", '{"choices":[{"message":null}]}'),
        (_KILO, "jp_hello", '{"choices":[{"message":{"content":null}}]}'),
        (_KILO, "jp_hello", '{"choices":[{"message":{"content":""}}]}'),
        (_KILO, "jp_hello", '{"choices":[{"message":{"content":"こんにちは"}}],"usage":{"prompt_tokens":-1}}'),
        (_KILO, "jp_hello", '{"choices":[{"message":{"content":"こんにちは"}}],"cost":"0"}'),
        (_KILO, "jp_hello", '{"choices":[{"message":{"content":"こんにちは"}}],"cost":true}'),
        (_KILO, "jp_hello", b'{"choices":[{"message":{"content":"\xff"}}]}'),
        (_POLLINATIONS, "jp_hello", "{}"),
    ],
)
def test_fa_t20_t26_malformed_responses_are_rejected(
    spec: catalog.ProviderSpec, fixture_id: str, body: str | bytes
) -> None:
    raw = body.encode("utf-8") if isinstance(body, str) else body
    evaluation = _evaluate(spec, fixture_id, raw)
    assert evaluation.status == "FORMAT_MISMATCH"
    assert evaluation.exit_code == 4


def test_fa_t26_duplicate_keys_and_deep_nesting_are_rejected() -> None:
    with pytest.raises(adapters.ResponseFormatError) as duplicate:
        adapters.decode_object(b'{"choices":[],"choices":[{"message":{"content":"4"}}]}')
    assert duplicate.value.reason_code == "INVALID_JSON"

    with pytest.raises(adapters.ResponseFormatError) as nested:
        adapters.decode_object(b'{"a":' + b"[" * 20 + b"]" * 20 + b"}")
    assert nested.value.reason_code == "INVALID_SCHEMA"

    with pytest.raises(adapters.ResponseFormatError) as finite:
        adapters.decode_object(b'{"n":Infinity}')
    assert finite.value.reason_code == "INVALID_JSON"

    with pytest.raises(adapters.ResponseFormatError) as not_object:
        adapters.decode_object(b"[1,2,3]")
    assert not_object.value.reason_code == "INVALID_SCHEMA"


def test_fa_t27_missing_usage_and_cost_are_null_not_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    outcome, _requests, _runs = _probe(
        monkeypatch,
        tmp_path,
        lambda _request: _json_response({"model": _KILO_MODEL, "choices": [{"message": {"content": "こんにちは"}}]}),
    )
    record = outcome.record
    assert record.status == "PASS"
    assert record.reason_code == "COST_UNKNOWN"
    assert record.input_tokens is None
    assert record.output_tokens is None
    assert record.cost_reported_usd is None


def test_fa_t28_non_zero_cost_is_unexpected_billing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    outcome, _requests, _runs = _probe(
        monkeypatch,
        tmp_path,
        lambda _request: _json_response(
            {"model": _KILO_MODEL, "choices": [{"message": {"content": "こんにちは"}}], "cost": 1}
        ),
    )
    assert outcome.record.status == "UNEXPECTED_BILLING"
    assert outcome.record.cost_reported_usd == 1.0
    assert outcome.exit_code == 4


def test_fa_t36_control_characters_and_long_reasoning_are_not_stored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    poisoned = "こんにちは\x07SECRET_MARKER"
    reasoning = "SECRET_REASONING " * 200
    hidden = tmp_path / "hidden.txt"
    hidden.write_text("do not leak", encoding="utf-8")

    outcome, _requests, runs = _probe(
        monkeypatch,
        tmp_path,
        lambda _request: _json_response(
            {
                "model": _KILO_MODEL,
                "choices": [
                    {"message": {"content": poisoned, "reasoning_content": reasoning, "tool_calls": []}},
                ],
            }
        ),
    )
    assert outcome.record.status == "CONTENT_MISMATCH"
    log = (runs / "probes.jsonl").read_text(encoding="utf-8")
    for secret in ("SECRET_MARKER", "SECRET_REASONING", "do not leak", "content", "reasoning"):
        assert secret not in log
    printed = json.dumps(outcome.record.as_dict(), ensure_ascii=False)
    assert "SECRET_MARKER" not in printed
    assert "SECRET_REASONING" not in printed
    assert "\x07" not in printed
    capsys.readouterr()


def test_model_absent_is_not_pass_even_with_the_expected_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outcome, _requests, _runs = _probe(
        monkeypatch,
        tmp_path,
        lambda _request: _json_response({"choices": [{"message": {"content": "こんにちは"}}]}),
    )
    assert outcome.record.status == "FORMAT_MISMATCH"
    assert outcome.record.reason_code == "INVALID_SCHEMA"
    assert outcome.record.model_observed is None
    assert outcome.exit_code == 4

    evaluation = _evaluate(
        _KILO,
        "jp_hello",
        json.dumps({"model": None, "choices": [{"message": {"content": "こんにちは"}}]}).encode(),
    )
    assert evaluation.status == "FORMAT_MISMATCH"
    assert evaluation.normalized is not None
    assert evaluation.normalized.model_observed is None


@pytest.mark.parametrize(
    "model",
    [
        "nvidia/nemotron-3.5-super-vl:free\nSECRET_LINE",
        "nvidia/nemotron-3.5-super-vl:free\x00tail",
        "nvidia/nemotron-3.5-super-vl:free\x1b[31mred",
        "x" * 5000,
        123,
        None,
        ["nvidia/nemotron-3.5-super-vl:free"],
    ],
)
def test_unsafe_model_ids_are_dropped_not_recorded(model: object) -> None:
    body = json.dumps({"model": model, "choices": [{"message": {"content": "こんにちは"}}]}).encode()
    evaluation = _evaluate(_KILO, "jp_hello", body)
    assert evaluation.status == "FORMAT_MISMATCH"
    assert evaluation.exit_code == 4
    assert evaluation.normalized is not None
    assert evaluation.normalized.model_observed is None


def test_unsafe_model_never_reaches_stdout_or_the_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    _mock_transport(
        monkeypatch,
        lambda _request: _json_response(
            {
                "model": "nvidia/nemotron-3.5-super-vl:free\nSECRET_LINE",
                "choices": [{"message": {"content": "こんにちは"}}],
            }
        ),
    )
    code = runner.main(
        [
            "probe",
            "--provider",
            "kilo",
            "--fixture",
            "jp_hello",
            "--ack-synthetic-only",
            "--ack-external-request",
        ]
    )
    printed = capsys.readouterr().out
    assert code == 4
    assert "SECRET_LINE" not in printed
    assert "\x1b" not in printed
    assert "\x00" not in printed
    assert printed.count("\n") == 1

    log = (runs / "probes.jsonl").read_text(encoding="utf-8")
    assert "SECRET_LINE" not in log
    assert log.count("\n") == 1
    record = json.loads(log.splitlines()[0])
    assert record["model_observed"] is None
    assert record["status"] == "FORMAT_MISMATCH"


_HUGE_INTEGER = int("9" * 309)


@pytest.mark.parametrize(
    ("spec", "fixture_id", "body"),
    [
        (
            _KILO,
            "jp_hello",
            {
                "model": _KILO_MODEL,
                "choices": [{"message": {"content": "こんにちは"}}],
                "cost": _HUGE_INTEGER,
            },
        ),
        (
            _JEV,
            "refund_noul",
            {"model": "jev-1.13-free", "answers": {"refund": {"type": "noul", "noul": _HUGE_INTEGER}}},
        ),
    ],
)
def test_absurd_integer_cost_or_noul_is_a_format_mismatch(
    spec: catalog.ProviderSpec, fixture_id: str, body: dict[str, object]
) -> None:
    evaluation = _evaluate(spec, fixture_id, json.dumps(body).encode())
    assert evaluation.status == "FORMAT_MISMATCH"
    assert evaluation.reason_code == "INVALID_SCHEMA"
    assert evaluation.exit_code == 4
    assert evaluation.normalized is None


def test_absurd_integer_cost_does_not_leak_the_conversion_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    _mock_transport(
        monkeypatch,
        lambda _request: _json_response(
            {
                "model": _KILO_MODEL,
                "choices": [{"message": {"content": "こんにちは"}}],
                "cost": _HUGE_INTEGER,
            }
        ),
    )
    code = runner.main(
        [
            "probe",
            "--provider",
            "kilo",
            "--fixture",
            "jp_hello",
            "--ack-synthetic-only",
            "--ack-external-request",
        ]
    )
    printed = capsys.readouterr().out
    assert code == 4
    for forbidden in ("Overflow", "int too large", "traceback", "Error"):
        assert forbidden not in printed
    log = (runs / "probes.jsonl").read_text(encoding="utf-8")
    assert "Overflow" not in log
    assert json.loads(log.splitlines()[0])["status"] == "FORMAT_MISMATCH"


def _deep_document(depth: int = 20_000) -> bytes:
    return ("[" * depth + "]" * depth).encode()


def test_deeply_nested_json_is_a_format_mismatch_not_a_crash() -> None:
    body = _deep_document()
    assert len(body) < transport.MAX_RESPONSE_BYTES
    with pytest.raises(adapters.ResponseFormatError) as deep:
        adapters.decode_object(body)
    assert deep.value.reason_code == "INVALID_JSON"

    evaluation = _evaluate(_KILO, "jp_hello", body)
    assert evaluation.status == "FORMAT_MISMATCH"
    assert evaluation.reason_code == "INVALID_JSON"
    assert evaluation.exit_code == 4
    assert evaluation.normalized is None


def test_deeply_nested_response_is_rejected_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    body = _deep_document()
    _mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(200, headers={"content-type": "application/json"}, content=body),
    )
    code = runner.main(
        [
            "probe",
            "--provider",
            "kilo",
            "--fixture",
            "jp_hello",
            "--ack-synthetic-only",
            "--ack-external-request",
        ]
    )
    printed = capsys.readouterr().out
    assert code == 4
    assert "FORMAT_MISMATCH" in printed
    for forbidden in ("RecursionError", "maximum recursion", "Traceback"):
        assert forbidden not in printed
    log = (runs / "probes.jsonl").read_text(encoding="utf-8")
    assert json.loads(log.splitlines()[0])["status"] == "FORMAT_MISMATCH"
    assert "RecursionError" not in log
