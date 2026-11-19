"""FA-T08..T17, T25 and T34: the single egress boundary. Fully offline."""

from __future__ import annotations

import asyncio
import dataclasses
import gzip
import inspect
import json
import socket
from collections.abc import AsyncIterator, Callable
from typing import Any, NoReturn

import httpcore
import httpx
import pytest
from httpcore._backends.mock import AsyncMockBackend

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


class _TimeoutStream(httpx.AsyncByteStream):
    """Yield zero or more chunks, then raise a selected timeout."""

    def __init__(self, chunks: tuple[bytes, ...], error: Exception) -> None:
        self._chunks = chunks
        self._error = error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        raise self._error

    async def aclose(self) -> None:
        return None


class _SleepingStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        await asyncio.sleep(0.2)
        yield b'{"ok":true}'

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
        dataclasses.replace(
            catalog.require_spec("kilo"),
            endpoint="https://api.kilo.ai/api/gateway/chat/completions",
        ),
        dataclasses.replace(catalog.require_spec("kilo"), endpoint="http://ai-gateway.kilo.ai/api/v1/chat/completions"),
        dataclasses.replace(catalog.require_spec("kilo"), endpoint="https://evil.example/chat"),
        dataclasses.replace(
            catalog.require_spec("kilo"),
            endpoint="https://ai-gateway.kilo.ai.evil.example/api/v1/chat/completions",
        ),
        dataclasses.replace(catalog.require_spec("kilo"), endpoint="https://ai-gateway.kilo.ai/api/v1/other"),
        dataclasses.replace(
            catalog.require_spec("kilo"),
            endpoint="https://ai-gateway.kilo.ai/api/v1/chat/completions/extra",
        ),
        dataclasses.replace(
            catalog.require_spec("kilo"), endpoint="https://ai-gateway.kilo.ai:8443/api/v1/chat/completions"
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
    assert str(requests[0].url) == "https://ai-gateway.kilo.ai/api/v1/chat/completions"


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


def test_kilo_uses_only_the_frozen_gateway_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _mock_transport(monkeypatch, lambda _request: _json_ok())
    spec = catalog.require_spec("kilo")
    result = asyncio.run(transport.post_json(spec, _KILO_FIXTURE))
    assert result.status == "OK"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.scheme == "https"
    assert request.url.host == "ai-gateway.kilo.ai"
    assert request.url.path == "/api/v1/chat/completions"
    assert request.url.query == b""
    assert request.url.port in (None, 443)
    assert json.loads(request.content) == fixtures.build_payload(spec, _KILO_FIXTURE)
    assert json.loads(request.content)["model"] == "nvidia/nemotron-3.5-super-vl:free"


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


def test_all_providers_use_the_15_second_read_timeout() -> None:
    async def inspect() -> tuple[float | None, float | None, float | None, float | None]:
        client = transport._new_client()
        try:
            timeout = client.timeout
            return (timeout.connect, timeout.read, timeout.write, timeout.pool)
        finally:
            await client.aclose()

    assert asyncio.run(inspect()) == (5.0, 15.0, 5.0, 5.0)
    assert transport.OVERALL_TIMEOUT_SECONDS == 25.0


def test_post_json_uses_the_same_fixed_client_for_all_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[float | None] = []

    def recording() -> httpx.AsyncClient:
        client = _REAL_NEW_CLIENT(httpx.MockTransport(lambda _request: _json_ok()))
        observed.append(client.timeout.read)
        return client

    monkeypatch.setattr(transport, "_new_client", recording)
    for provider_id in ("kilo", "ovh"):
        result = asyncio.run(transport.post_json(catalog.require_spec(provider_id), _KILO_FIXTURE))
        assert result.status == "OK"
    assert observed == [15.0, 15.0]


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


@pytest.mark.parametrize(
    ("error_type", "expected_kind"),
    [
        (httpx.ConnectTimeout, "CONNECT_TIMEOUT"),
        (httpx.ReadTimeout, "READ_TIMEOUT"),
        (httpx.WriteTimeout, "WRITE_TIMEOUT"),
        (httpx.PoolTimeout, "POOL_TIMEOUT"),
        (httpx.TimeoutException, "OTHER_TIMEOUT"),
    ],
)
def test_timeout_kind_is_classified_without_retaining_the_message(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[httpx.TimeoutException],
    expected_kind: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error_type("provider secret must not be retained", request=request)

    requests = _mock_transport(monkeypatch, handler)
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.status == "TIMEOUT"
    assert result.reason_code == "UNKNOWN_OUTCOME"
    assert result.http_status is None
    assert result.unknown_outcome is True
    assert result.timeout_diagnostic is not None
    assert result.timeout_diagnostic.timeout_kind == expected_kind
    assert result.timeout_diagnostic.timeout_phase == "BEFORE_HEADERS"
    assert result.timeout_diagnostic.body_chunk_received is False
    assert len(requests) == 1


def test_trace_collector_keeps_only_allowlisted_phase_events() -> None:
    collector = transport._TraceCollector(0.0)
    asyncio.run(collector.callback("connection.connect_tcp.started", {"secret": "must not be retained"}))
    asyncio.run(collector.callback("connection.connect_tcp.complete", {}))
    asyncio.run(collector.callback("connection.connect_tcp.failed", {}))
    asyncio.run(collector.callback("unknown.event", {"secret": "ignored"}))
    timing = collector.snapshot()

    assert timing.observed is True
    assert [(event.phase, event.state) for event in timing.events] == [
        ("CONNECT", "STARTED"),
        ("CONNECT", "COMPLETE"),
        ("CONNECT", "FAILED"),
    ]
    assert all(event.elapsed_ms >= 0 for event in timing.events)
    assert "secret" not in repr(timing)


def test_trace_collector_observes_the_real_httpcore_event_sequence() -> None:
    async def run() -> transport.TraceTiming:
        collector = transport._TraceCollector(0.0)
        response_bytes = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
        pool = httpcore.AsyncConnectionPool(
            network_backend=AsyncMockBackend([response_bytes]),
            http1=True,
            http2=False,
        )
        request = httpcore.Request(
            method=b"POST",
            url=httpcore.URL("https://example.test/"),
            headers=[
                (b"host", b"example.test"),
                (b"content-type", b"application/json"),
                (b"content-length", b"2"),
            ],
            content=b"{}",
            extensions={"trace": collector.callback},
        )
        try:
            response = await pool.handle_async_request(request)
            assert await response.aread() == b"{}"
        finally:
            await pool.aclose()
        return collector.snapshot()

    timing = asyncio.run(run())
    assert timing.observed is True
    assert [(event.phase, event.state) for event in timing.events] == [
        ("CONNECT", "STARTED"),
        ("CONNECT", "COMPLETE"),
        ("TLS", "STARTED"),
        ("TLS", "COMPLETE"),
        ("REQUEST_HEADERS", "STARTED"),
        ("REQUEST_HEADERS", "COMPLETE"),
        ("REQUEST_BODY", "STARTED"),
        ("REQUEST_BODY", "COMPLETE"),
        ("RESPONSE_HEADERS", "STARTED"),
        ("RESPONSE_HEADERS", "COMPLETE"),
        ("RESPONSE_BODY", "STARTED"),
        ("RESPONSE_BODY", "COMPLETE"),
    ]


def test_trace_collector_callback_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    collector = transport._TraceCollector(0.0)

    def broken(_started: float) -> int:
        raise RuntimeError("observation failed")

    monkeypatch.setattr(transport, "_elapsed_ms", broken)
    asyncio.run(collector.callback("connection.connect_tcp.started", {"secret": "ignored"}))
    assert collector.snapshot().observed is False


def test_trace_callback_failure_does_not_change_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        trace = request.extensions["trace"]
        await trace("connection.connect_tcp.started", {"secret": "ignored"})
        raise httpx.ReadTimeout("provider secret must not be retained", request=request)

    class BrokenTraceEvent:
        def __init__(self, **_kwargs: object) -> None:
            raise RuntimeError("observation failed")

    monkeypatch.setattr(transport, "TraceEvent", BrokenTraceEvent)
    monkeypatch.setattr(transport, "_new_client", lambda: _REAL_NEW_CLIENT(httpx.MockTransport(handler)))
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.status == "TIMEOUT"
    assert result.reason_code == "UNKNOWN_OUTCOME"
    assert result.trace_timing is not None
    assert result.trace_timing.observed is False
    assert result.trace_timing.events == ()
    assert len(attempts) == 1


def test_read_timeout_before_headers_has_before_headers_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider secret must not be retained", request=request)

    _mock_transport(monkeypatch, handler)
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.timeout_diagnostic is not None
    assert result.timeout_diagnostic.timeout_kind == "READ_TIMEOUT"
    assert result.timeout_diagnostic.timeout_phase == "BEFORE_HEADERS"
    assert result.timeout_diagnostic.body_chunk_received is False


def test_read_timeout_after_headers_before_body_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_TimeoutStream((), httpx.ReadTimeout("provider secret must not be retained", request=request)),
        )

    _mock_transport(monkeypatch, handler)
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.timeout_diagnostic is not None
    assert result.timeout_diagnostic.timeout_kind == "READ_TIMEOUT"
    assert result.timeout_diagnostic.timeout_phase == "BODY_RECEIVING"
    assert result.timeout_diagnostic.body_chunk_received is False


def test_read_timeout_after_a_body_chunk_marks_chunk_received(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_TimeoutStream(
                (b'{"ok":',), httpx.ReadTimeout("provider secret must not be retained", request=request)
            ),
        )

    _mock_transport(monkeypatch, handler)
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.timeout_diagnostic is not None
    assert result.timeout_diagnostic.timeout_kind == "READ_TIMEOUT"
    assert result.timeout_diagnostic.timeout_phase == "BODY_RECEIVING"
    assert result.timeout_diagnostic.body_chunk_received is True


def test_overall_timeout_is_distinct_from_httpx_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport, "OVERALL_TIMEOUT_SECONDS", 0.01)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_SleepingStream(),
        )

    requests = _mock_transport(monkeypatch, handler)
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.status == "TIMEOUT"
    assert result.reason_code == "UNKNOWN_OUTCOME"
    assert result.unknown_outcome is True
    assert result.timeout_diagnostic is not None
    assert result.timeout_diagnostic.timeout_kind == "OVERALL_TIMEOUT"
    assert result.timeout_diagnostic.timeout_phase == "BODY_RECEIVING"
    assert result.timeout_diagnostic.body_chunk_received is False
    assert len(requests) == 1


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
    assert requests[0].url.host == "ai-gateway.kilo.ai"


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
        socket.create_connection(("ai-gateway.kilo.ai", 443), timeout=0.1)
    _mock_transport(monkeypatch, lambda _request: _json_ok())
    result = asyncio.run(transport.post_json(catalog.require_spec("kilo"), _KILO_FIXTURE))
    assert result.status == "OK"
