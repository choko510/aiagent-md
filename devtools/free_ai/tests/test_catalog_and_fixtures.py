"""FA-T01..T03 plus fixture and catalog invariants. Fully offline."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import socket
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import httpx
import pytest

from devtools.free_ai import catalog, fixtures, runner, transport

_REAL_NEW_CLIENT = transport._new_client


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


_EXPECTED_CATALOG: dict[str, tuple[str, str, str, str, str, int, int]] = {
    "kilo": (
        "https://api.kilo.ai/api/gateway/chat/completions",
        "nvidia/nemotron-3.5-super-vl:free",
        "chat_completions",
        "ELIGIBLE",
        "official_anonymous",
        180,
        16,
    ),
    "ovh": (
        "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
        "gpt-oss-120b",
        "chat_completions",
        "ELIGIBLE",
        "official_anonymous",
        65,
        12,
    ),
    "jev_legacy": (
        "https://opencode.ai/zen/v1/systemone",
        "jev-1.13-free",
        "system_one",
        "EXPERIMENTAL_DISABLED",
        "user_observed_legacy",
        300,
        2,
    ),
    "pollinations_legacy": (
        "https://text.pollinations.ai/openai",
        "openai-fast",
        "chat_completions",
        "EXPERIMENTAL_DISABLED",
        "user_observed_legacy",
        300,
        2,
    ),
}


def test_fa_t01_catalog_is_exact_and_frozen() -> None:
    assert set(catalog.PROVIDERS) == set(_EXPECTED_CATALOG)
    assert catalog.ALL_PROVIDER_IDS == ("kilo", "ovh", "jev_legacy", "pollinations_legacy")
    for provider_id, expected in _EXPECTED_CATALOG.items():
        spec = catalog.require_spec(provider_id)
        assert (
            spec.endpoint,
            spec.model_id,
            spec.wire_kind,
            spec.eligibility,
            spec.evidence_class,
            spec.min_interval_seconds,
            spec.max_calls_per_local_day,
        ) == expected
        assert spec.endpoint.startswith("https://")
        assert spec.endpoint_host in spec.endpoint
        catalog.validate_spec(spec)
    with pytest.raises(TypeError):
        catalog.PROVIDERS["injected"] = catalog.require_spec("kilo")  # type: ignore[index]
    with pytest.raises(TypeError):
        catalog.LEGACY_ANONYMOUS_ALLOWED["jev_legacy"] = True  # type: ignore[index]


def test_fa_t02_unknown_provider_and_tampered_spec_are_rejected_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert catalog.get_spec("not-a-provider") is None
    with pytest.raises(catalog.CatalogError):
        catalog.require_spec("not-a-provider")

    tampered = [
        dataclasses.replace(catalog.require_spec("kilo"), endpoint="http://api.kilo.ai/x"),
        dataclasses.replace(catalog.require_spec("kilo"), endpoint="https://evil.example/chat"),
        dataclasses.replace(catalog.require_spec("kilo"), endpoint="https://127.0.0.1/chat"),
        dataclasses.replace(catalog.require_spec("kilo"), endpoint="https://api.kilo.ai:8443/chat"),
        dataclasses.replace(catalog.require_spec("kilo"), model_id="some-paid-model"),
        dataclasses.replace(catalog.require_spec("kilo"), min_interval_seconds=0),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent for a tampered spec")

    monkeypatch.setattr(transport, "_new_client", lambda: _REAL_NEW_CLIENT(httpx.MockTransport(handler)))
    for spec in tampered:
        with pytest.raises(catalog.CatalogError):
            catalog.validate_spec(spec)
        with pytest.raises(catalog.CatalogError):
            asyncio.run(transport.post_json(spec, "jp_hello"))

    monkeypatch.setenv("KILO_API_BASE", "https://evil.example")
    monkeypatch.setenv("AIAGENT_FREE_AI_MODEL", "paid/model")
    assert catalog.require_spec("kilo").endpoint == _EXPECTED_CATALOG["kilo"][0]
    assert catalog.require_spec("kilo").model_id == _EXPECTED_CATALOG["kilo"][1]


def test_fa_t02_endpoint_shape_rules() -> None:
    base = catalog.require_spec("kilo")
    rejected = [
        "http://api.kilo.ai/api/gateway/chat/completions",
        "https://user:pw@api.kilo.ai/api/gateway/chat/completions",
        "https://api.kilo.ai/api/gateway/chat/completions?x=1",
        "https://api.kilo.ai/api/gateway/chat/completions#frag",
        "https://api.kilo.ai:443/api/gateway/chat/completions",
        "https://localhost/api/gateway/chat/completions",
        "https://[::1]/api/gateway/chat/completions",
        "https://api.kilo.ai/",
    ]
    for endpoint in rejected:
        with pytest.raises(catalog.CatalogError):
            catalog.validate_spec(dataclasses.replace(base, endpoint=endpoint))


def test_fa_t03_list_dry_run_help_are_offline_and_side_effect_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _temp_runs(monkeypatch, tmp_path)
    assert runner.main(["list"]) == 0
    listing = capsys.readouterr().out
    assert "kilo" in listing and "unverified" in listing and "LEGACY_DISABLED" in listing
    assert "無料確定" not in listing
    assert runner.main(["dry-run"]) == 0
    dry = capsys.readouterr().out
    assert "network_sent=0" in dry
    assert "bytes=" in dry
    assert "destination=https://api.kilo.ai/api/gateway/chat/completions" in dry
    assert not runs.exists()

    with pytest.raises(SystemExit) as help_exit:
        runner.main(["--help"])
    assert help_exit.value.code == 0
    with pytest.raises(SystemExit) as version_exit:
        runner.main(["--version"])
    assert version_exit.value.code == 0
    assert not runs.exists()
    capsys.readouterr()


def test_fixtures_are_exact_constants_and_wire_kind_restricted() -> None:
    kilo = catalog.require_spec("kilo")
    ovh = catalog.require_spec("ovh")
    jev = catalog.require_spec("jev_legacy")

    chat_fixtures = ("jp_hello", "arithmetic_2plus2", *fixtures.DEV_FIXTURE_IDS)
    assert fixtures.allowed_fixtures(kilo) == chat_fixtures
    assert fixtures.allowed_fixtures(ovh) == chat_fixtures
    assert fixtures.allowed_fixtures(jev) == ("refund_noul",)
    assert fixtures.allowed_fixtures(catalog.require_spec("pollinations_legacy")) == (
        "jp_hello",
        "arithmetic_2plus2",
    )

    assert fixtures.build_payload(kilo, "jp_hello") == {
        "model": "nvidia/nemotron-3.5-super-vl:free",
        "messages": [{"role": "user", "content": fixtures.JP_HELLO_PROMPT}],
        "stream": False,
    }
    assert fixtures.build_payload(ovh, "arithmetic_2plus2") == {
        "model": "gpt-oss-120b",
        "messages": [{"role": "user", "content": fixtures.ARITHMETIC_PROMPT}],
        "stream": False,
    }
    assert fixtures.build_payload(jev, "refund_noul") == {
        "model": "jev-1.13-free",
        "state": fixtures.REFUND_STATE,
        "questions": {"refund": {"type": "noul", "instructions": fixtures.REFUND_INSTRUCTIONS}},
    }

    with pytest.raises(fixtures.FixtureError):
        fixtures.build_payload(jev, "jp_hello")
    with pytest.raises(fixtures.FixtureError):
        fixtures.build_payload(kilo, "refund_noul")
    with pytest.raises(fixtures.FixtureError):
        fixtures.build_payload(kilo, "not-a-fixture")

    assert list(inspect.signature(fixtures.build_payload).parameters) == ["spec", "fixture_id"]
    for spec in catalog.catalog_specs():
        for fixture_id in fixtures.allowed_fixtures(spec):
            size = len(transport.encode_body(fixtures.build_payload(spec, fixture_id)))
            assert 0 < size <= transport.MAX_REQUEST_BYTES

    assert fixtures.get_fixture("jp_hello") is not None
    assert fixtures.get_fixture("nope") is None
