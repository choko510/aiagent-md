"""FA-T08..T17, T25 and T34: the single egress boundary. Fully offline."""

from __future__ import annotations

import asyncio
import dataclasses
import gzip
import inspect
import socket
from collections.abc import AsyncIterator, Callable
from typing import Any, NoReturn

import httpx
import pytest

from devtools.free_ai import catalog, fixtures, transport

_REAL_NEW_CLIENT = transport._new_client
_KILO_FIXTURE = "jp_hello"


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


class _GuardedStream(httpx.AsyncByteStream):
    """A response stream that records whether the transport started to read it."""

    def __init__(self, payload: bytes = b"{}", *, raise_on_iterate: bool = True) -> None:
        self._payload = payload
        self._raise = raise_on_iterate
        self.iterated = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.iterated = True
        if self._raise:
            raise AssertionError("a compressed body must not be decoded")
        yield self._payload

    async def aclose(self) -> None:
        return None


def _json_ok(content: str = "こんにちは", model: str = "nvidia/nemotron-3.5-super-vl:free") -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json={"model": model, "choices": [{"message": {"role": "assistant", "content": content}}]},
    )


def test_fa_t08_unexpected_endpoint_or_spec_never_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("request must not be sent")

    monkeypatch.setattr(transport, "_new_client", lambda: _REAL_NEW_CLIENT(httpx.MockTransport(handler)))
    killed = [
        dataclasses.replace(catalog.require_spec("kilo"), endpoint="http://api.kilo.ai/api/gateway/chat/completions"),
        dataclasses.replace(catalog.require_spec("kilo"), endpoint="https://evil.example/chat"),
        dataclasses.replace(catalog.require_spec("kilo"), endpoint="https://api.kilo.ai/chat"),
        dataclasses.replace(
            catalog.require_spec("kilo"), endpoint="https://api.kilo.ai:8443/api/gateway/chat/completions"
        ),
    ]
    for spec in killed:
        with pytest.raises(catalog.CatalogError):
            asyncio.run(transport.post_json(spec, _KILO_FIXTURE))


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_fa_t09_redirects_are_refused(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    requests = _mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(status, headers={"location": "https://evil.example/next"}),
    )
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.status == "SAFETY_BLOCKED"
    assert result.reason_code == "UNEXPECTED_REDIRECT"
    assert result.http_status == status
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.kilo.ai/api/gateway/chat/completions"


def test_fa_t10_no_credentials_and_no_injected_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _mock_transport(monkeypatch, lambda _request: _json_ok())
    monkeypatch.setenv("HTTP_PROXY", "http://user:pass@127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pass@127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")
    monkeypatch.setenv("AIAGENT_AUTHORIZATION", "Bearer leaked")
    monkeypatch.setenv("GITHUB_TOKEN", "gho_leaked")
    monkeypatch.setenv("COOKIE", "session=leaked")

    spec = catalog.require_spec("kilo")
    result = asyncio.run(transport.post_json(spec, _KILO_FIXTURE))
    assert result.status == "OK"
    assert len(requests) == 1
    request = requests[0]
    lowered = {name.lower(): value for name, value in request.headers.items()}
    for forbidden in ("authorization", "proxy-authorization", "cookie", "referer", "x-api-key"):
        assert forbidden not in lowered
    assert lowered["user-agent"] == transport.USER_AGENT
    assert lowered["accept"] == "application/json"
    assert lowered["content-type"] == "application/json"
    assert lowered["accept-encoding"] == "identity"
    assert request.content == transport.encode_body(fixtures.build_payload(spec, _KILO_FIXTURE))
    assert "leaked" not in repr(request.headers)


def test_fa_t11_proxy_environment_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)

    async def inspect_client() -> tuple[bool, bool, float | None, float | None]:
        client = transport._new_client()
        try:
            return (
                client.trust_env,
                client.follow_redirects,
                client.timeout.connect,
                client.timeout.read,
            )
        finally:
            await client.aclose()

    trust_env, follow_redirects, connect_timeout, read_timeout = asyncio.run(inspect_client())
    assert trust_env is False
    assert follow_redirects is False
    assert connect_timeout == 5.0
    assert read_timeout == 15.0


def test_new_client_constructor_is_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    real_client = httpx.AsyncClient

    def fake_client(**kwargs: Any) -> httpx.AsyncClient:
        captured.update(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    client = transport._new_client()
    try:
        assert captured["trust_env"] is False
        assert captured["follow_redirects"] is False
        assert captured["verify"] is True
        assert captured["http2"] is False
        limits = captured["limits"]
        assert isinstance(limits, httpx.Limits)
        assert limits.max_connections == 1
        assert limits.max_keepalive_connections == 0
        timeout = captured["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (5.0, 15.0, 5.0, 5.0)
    finally:
        asyncio.run(client.aclose())


def test_fa_t12_oversized_request_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("oversized request must not be sent")

    monkeypatch.setattr(transport, "_new_client", lambda: _REAL_NEW_CLIENT(httpx.MockTransport(handler)))
    with pytest.raises(transport.LocalInputError) as oversized:
        transport.encode_body({"model": "nvidia/nemotron-3.5-super-vl:free", "text": "a" * 5000})
    assert oversized.value.reason_code == "BODY_TOO_LARGE"


def test_post_json_accepts_only_a_fixture_id(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no arbitrary body may reach the network")

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(transport, "_new_client", lambda: _REAL_NEW_CLIENT(httpx.MockTransport(recording)))
    spec = catalog.require_spec("kilo")
    assert list(inspect.signature(transport.post_json).parameters) == ["spec", "fixture_id"]

    wrong_arguments: tuple[object, ...] = ({"choices": []}, b'{"model":"x"}', None, 4)
    for wrong in wrong_arguments:
        result = asyncio.run(transport.post_json(spec, wrong))  # type: ignore[arg-type]
        assert result.status == "LOCAL_INPUT_INVALID"
        assert result.reason_code == "INVALID_SCHEMA"
    for unknown in ("refund_noul", "not-a-fixture"):
        result = asyncio.run(transport.post_json(spec, unknown))
        assert result.status == "LOCAL_INPUT_INVALID"
    assert requests == []


@pytest.mark.parametrize("provider", ["jev_legacy", "pollinations_legacy"])
def test_legacy_services_are_refused_at_the_egress_boundary(monkeypatch: pytest.MonkeyPatch, provider: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("a disabled legacy endpoint must never receive a request")

    monkeypatch.setattr(transport, "_new_client", lambda: _REAL_NEW_CLIENT(httpx.MockTransport(handler)))
    spec = catalog.require_spec(provider)
    fixture_id = "refund_noul" if provider == "jev_legacy" else "jp_hello"
    result = asyncio.run(transport.post_json(spec, fixture_id))
    assert result.status == "SAFETY_BLOCKED"
    assert result.reason_code == "LEGACY_DISABLED"
    assert result.http_status is None


def test_fa_t13_oversized_response_is_detected_and_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"x" * (transport.MAX_RESPONSE_BYTES + 1)
    requests = _mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(200, headers={"content-type": "application/json"}, content=payload),
    )
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.status == "SAFETY_BLOCKED"
    assert result.reason_code == "BODY_TOO_LARGE"
    assert result.body is None
    assert len(requests) == 1


def test_single_chunk_over_the_limit_is_rejected_before_copying(monkeypatch: pytest.MonkeyPatch) -> None:
    # A mis-declared content-length must not bypass the decoded-size cap: the chunk is
    # refused before it is appended, so no oversized buffer is ever built.
    payload = b"y" * (transport.MAX_RESPONSE_BYTES + 1)
    _mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "content-length": str(transport.MAX_RESPONSE_BYTES),
            },
            content=payload,
        ),
    )
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.status == "SAFETY_BLOCKED"
    assert result.reason_code == "BODY_TOO_LARGE"
    assert result.body is None


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "zstd", "compress", "GZIP"])
def test_compressed_responses_are_refused_before_decoding(monkeypatch: pytest.MonkeyPatch, encoding: str) -> None:
    stream = _GuardedStream()
    _mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": encoding},
            stream=stream,
        ),
    )
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.status == "FORMAT_MISMATCH"
    assert result.reason_code == "INVALID_JSON"
    assert result.body is None
    assert stream.iterated is False


def test_identity_content_encoding_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b'{"model":"nvidia/nemotron-3.5-super-vl:free","choices":[{"message":{"content":"4"}}]}'
    _mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "identity"},
            content=body,
        ),
    )
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.status == "OK"
    assert result.body == body


def test_compression_bomb_is_rejected_without_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    bomb = gzip.compress(b"\x00" * (transport.MAX_RESPONSE_BYTES * 4))
    stream = _GuardedStream(bomb, raise_on_iterate=False)
    _mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            stream=stream,
        ),
    )
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.status == "FORMAT_MISMATCH"
    assert result.reason_code == "INVALID_JSON"
    assert result.body is None
    assert stream.iterated is False


def test_corrupt_encoded_body_is_a_format_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            content=b"not-a-gzip-frame",
        ),
    )
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.status == "FORMAT_MISMATCH"
    assert result.reason_code == "INVALID_JSON"
    assert result.body is None


def test_fa_t14_transport_failures_are_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    def connect_failure(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        transport,
        "_new_client",
        lambda: _REAL_NEW_CLIENT(httpx.MockTransport(connect_failure)),
    )
    failed = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert failed.status == "NETWORK_ERROR"
    assert failed.reason_code == "UNKNOWN_OUTCOME"
    assert failed.unknown_outcome is True
    assert failed.http_status is None

    attempts: list[httpx.Request] = []

    def read_timeout(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ReadTimeout("timed out", request=request)

    monkeypatch.setattr(transport, "_new_client", lambda: _REAL_NEW_CLIENT(httpx.MockTransport(read_timeout)))
    timed_out = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert timed_out.status == "TIMEOUT"
    assert timed_out.reason_code == "UNKNOWN_OUTCOME"
    assert len(attempts) == 1


@pytest.mark.parametrize("status", [401, 402, 403, 404, 410])
def test_fa_t15_access_restricted(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    requests = _mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(status, headers={"content-type": "application/json"}, json={"error": "no"}),
    )
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.status == "ACCESS_RESTRICTED"
    assert result.reason_code == "AUTH_REQUIRED"
    assert result.http_status == status
    assert result.body is None
    assert len(requests) == 1


def test_fa_t16_rate_limited_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(429, headers={"content-type": "application/json", "retry-after": "60"}),
    )
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.status == "RATE_LIMITED"
    assert result.reason_code == "RATE_LIMIT"
    assert result.http_status == 429
    assert len(requests) == 1


@pytest.mark.parametrize("status", [500, 503])
def test_fa_t17_remote_error_without_fallback(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    requests = _mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(status, headers={"content-type": "application/json"}, json={"error": "boom"}),
    )
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.status == "REMOTE_ERROR"
    assert result.reason_code == "REMOTE_FAILURE"
    assert result.http_status == status
    assert len(requests) == 1
    assert requests[0].url.host == "api.kilo.ai"


def test_fa_t25_http_status_wins_over_200_like_body(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _mock_transport(monkeypatch, lambda _request: _json_ok(model="gpt-oss-120b"))
    ok = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert ok.status == "OK"
    assert len(requests) == 1

    bad = _mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(
            500,
            headers={"content-type": "application/json"},
            json={"choices": [{"message": {"content": "こんにちは"}}]},
        ),
    )
    failed = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert failed.status == "REMOTE_ERROR"
    assert failed.body is None
    assert len(bad) == 1


def test_fa_t25_non_json_content_type_is_a_format_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html></html>"),
    )
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.status == "FORMAT_MISMATCH"
    assert result.reason_code == "INVALID_JSON"
    assert result.body is None


def test_fa_t34_socket_guard_is_effective(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="offline test attempted"):
        socket.create_connection(("api.kilo.ai", 443), timeout=0.1)
    _mock_transport(monkeypatch, lambda _request: _json_ok())
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.status == "OK"
