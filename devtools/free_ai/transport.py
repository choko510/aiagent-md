"""The single egress point of the dev lab: one fixed HTTPS POST per call, zero retries.

Only this module may open a network connection. It accepts a :class:`ProviderSpec`
that is byte-for-byte the frozen catalog entry, refuses redirects, ignores proxy
environment variables, caps request and response sizes, and never attaches
credentials. Provider responses are returned as raw bytes after the size check; the
typed, non-trusting decoding happens in :mod:`devtools.free_ai.adapters`.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Final, Literal

import httpx

from . import ReasonCode
from .catalog import LEGACY_ANONYMOUS_ALLOWED, ProviderSpec, validate_spec
from .fixtures import FixtureError, build_payload

MAX_REQUEST_BYTES: Final = 4096
MAX_RESPONSE_BYTES: Final = 131072
OVERALL_TIMEOUT_SECONDS: Final = 25.0
DEFAULT_TIMEOUT: Final = httpx.Timeout(20.0, connect=5.0, read=15.0, write=5.0, pool=5.0)
USER_AGENT: Final = "aiagent-md-dev-free-ai-lab/0.1"

#: Any 3xx is treated as an attempted redirect and is never followed.
REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})

#: Statuses a provider may report that mean "access is gated by authentication".
ACCESS_RESTRICTED_STATUSES: Final = frozenset({401, 402, 403, 404, 410})

TransportStatus = Literal[
    "OK",
    "ACCESS_RESTRICTED",
    "RATE_LIMITED",
    "REMOTE_ERROR",
    "NETWORK_ERROR",
    "TIMEOUT",
    "SAFETY_BLOCKED",
    "FORMAT_MISMATCH",
    "LOCAL_INPUT_INVALID",
]

TimeoutKind = Literal[
    "CONNECT_TIMEOUT",
    "READ_TIMEOUT",
    "WRITE_TIMEOUT",
    "POOL_TIMEOUT",
    "OVERALL_TIMEOUT",
    "OTHER_TIMEOUT",
]

TimeoutPhase = Literal[
    "BEFORE_HEADERS",
    "HEADERS_RECEIVED",
    "BODY_RECEIVING",
    "UNKNOWN",
]


@dataclass(frozen=True, slots=True)
class TimeoutDiagnostic:
    """Safe, internal facts about one timeout; never includes provider text."""

    timeout_kind: TimeoutKind
    timeout_phase: TimeoutPhase
    body_chunk_received: bool


class LocalInputError(ValueError):
    """The request body could not be encoded safely; nothing was sent."""

    def __init__(self, reason_code: ReasonCode) -> None:
        super().__init__("local request input is invalid")
        self.reason_code: ReasonCode = reason_code


@dataclass(frozen=True, slots=True)
class TransportResult:
    """Outcome of one attempted POST. ``body`` is untrusted raw bytes, never parsed here."""

    status: TransportStatus
    reason_code: ReasonCode
    http_status: int | None = None
    body: bytes | None = None
    elapsed_ms: int | None = None
    unknown_outcome: bool = False
    timeout_diagnostic: TimeoutDiagnostic | None = None


def encode_body(body: dict[str, object]) -> bytes:
    """Serialize the fixed fixture body with a hard 4096-byte cap.

    Used by ``dry-run`` for the exact byte count without any network side effect.
    """
    try:
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise LocalInputError("INVALID_SCHEMA") from exc
    if len(encoded) > MAX_REQUEST_BYTES:
        raise LocalInputError("BODY_TOO_LARGE")
    return encoded


def _new_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """Build the one allowed HTTP client shape.

    ``transport`` exists only so that offline tests can inject ``httpx.MockTransport``;
    it is never reachable from the CLI, from environment variables or from a provider.
    """
    return httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        verify=True,
        http2=False,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        timeout=DEFAULT_TIMEOUT,
        transport=transport,
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _classify_timeout(exc: httpx.TimeoutException) -> TimeoutKind:
    """Classify only the exception type; never retain its message."""
    if isinstance(exc, httpx.ConnectTimeout):
        return "CONNECT_TIMEOUT"
    if isinstance(exc, httpx.ReadTimeout):
        return "READ_TIMEOUT"
    if isinstance(exc, httpx.WriteTimeout):
        return "WRITE_TIMEOUT"
    if isinstance(exc, httpx.PoolTimeout):
        return "POOL_TIMEOUT"
    return "OTHER_TIMEOUT"


def _timeout_result(
    started: float,
    kind: TimeoutKind,
    phase: TimeoutPhase,
    body_chunk_received: bool,
) -> TransportResult:
    return TransportResult(
        status="TIMEOUT",
        reason_code="UNKNOWN_OUTCOME",
        elapsed_ms=_elapsed_ms(started),
        unknown_outcome=True,
        timeout_diagnostic=TimeoutDiagnostic(
            timeout_kind=kind,
            timeout_phase=phase,
            body_chunk_received=body_chunk_received,
        ),
    )


def _is_json_content_type(value: str | None) -> bool:
    if value is None:
        return False
    media_type = value.split(";")[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


#: Only an unencoded body is accepted. ``gzip``/``deflate``/``br`` and unknown encodings
#: are refused *before* any decoder is started, so a compression bomb never expands.
ALLOWED_CONTENT_ENCODINGS: Final = frozenset({"", "identity"})


def _is_allowed_content_encoding(value: str | None) -> bool:
    return value is None or value.strip().lower() in ALLOWED_CONTENT_ENCODINGS


def _map_http_status(http_status: int, started: float) -> TransportResult:
    if http_status in ACCESS_RESTRICTED_STATUSES:
        return TransportResult(
            status="ACCESS_RESTRICTED",
            reason_code="AUTH_REQUIRED",
            http_status=http_status,
            elapsed_ms=_elapsed_ms(started),
        )
    if http_status == 429:
        return TransportResult(
            status="RATE_LIMITED",
            reason_code="RATE_LIMIT",
            http_status=http_status,
            elapsed_ms=_elapsed_ms(started),
        )
    return TransportResult(
        status="REMOTE_ERROR",
        reason_code="REMOTE_FAILURE",
        http_status=http_status,
        elapsed_ms=_elapsed_ms(started),
    )


async def post_json(spec: ProviderSpec, fixture_id: str) -> TransportResult:
    """POST exactly one fixed synthetic fixture to the spec's fixed endpoint.

    The caller cannot supply a body: the payload is rebuilt from the frozen catalog and
    ``fixtures.py`` constants immediately before serialization. Legacy services are
    refused here as well as in the CLI, so no path can reach their endpoints while
    their anonymous terms are unconfirmed.
    """
    validate_spec(spec)
    if spec.is_legacy and not LEGACY_ANONYMOUS_ALLOWED.get(spec.provider_id, False):
        return TransportResult(status="SAFETY_BLOCKED", reason_code="LEGACY_DISABLED")
    if not isinstance(fixture_id, str):
        return TransportResult(status="LOCAL_INPUT_INVALID", reason_code="INVALID_SCHEMA")
    try:
        payload = build_payload(spec, fixture_id)
        encoded = encode_body(payload)
    except FixtureError:
        return TransportResult(status="LOCAL_INPUT_INVALID", reason_code="INVALID_SCHEMA")
    except LocalInputError as exc:
        return TransportResult(status="LOCAL_INPUT_INVALID", reason_code=exc.reason_code)

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    started = time.monotonic()
    phase: TimeoutPhase = "BEFORE_HEADERS"
    body_chunk_received = False
    try:
        async with asyncio.timeout(OVERALL_TIMEOUT_SECONDS):
            client = _new_client()
            async with client:
                async with client.stream("POST", spec.endpoint, content=encoded, headers=headers) as response:
                    phase = "HEADERS_RECEIVED"
                    http_status = response.status_code
                    if 300 <= http_status < 400:
                        return TransportResult(
                            status="SAFETY_BLOCKED",
                            reason_code="UNEXPECTED_REDIRECT",
                            http_status=http_status,
                            elapsed_ms=_elapsed_ms(started),
                        )
                    if http_status != 200:
                        return _map_http_status(http_status, started)
                    if not _is_json_content_type(response.headers.get("content-type")):
                        return TransportResult(
                            status="FORMAT_MISMATCH",
                            reason_code="INVALID_JSON",
                            http_status=http_status,
                            elapsed_ms=_elapsed_ms(started),
                        )
                    if not _is_allowed_content_encoding(response.headers.get("content-encoding")):
                        # Refuse compressed bodies before reading a single byte: no decoder
                        # is constructed and no compression bomb can expand.
                        return TransportResult(
                            status="FORMAT_MISMATCH",
                            reason_code="INVALID_JSON",
                            http_status=http_status,
                            elapsed_ms=_elapsed_ms(started),
                        )
                    declared_header = response.headers.get("content-length")
                    if declared_header is not None:
                        try:
                            declared = int(declared_header.strip())
                        except ValueError:
                            return TransportResult(
                                status="FORMAT_MISMATCH",
                                reason_code="INVALID_JSON",
                                http_status=http_status,
                                elapsed_ms=_elapsed_ms(started),
                            )
                        if declared < 0:
                            return TransportResult(
                                status="FORMAT_MISMATCH",
                                reason_code="INVALID_JSON",
                                http_status=http_status,
                                elapsed_ms=_elapsed_ms(started),
                            )
                        if declared > MAX_RESPONSE_BYTES:
                            return TransportResult(
                                status="SAFETY_BLOCKED",
                                reason_code="BODY_TOO_LARGE",
                                http_status=http_status,
                                elapsed_ms=_elapsed_ms(started),
                            )
                    received = bytearray()
                    phase = "BODY_RECEIVING"
                    async for chunk in response.aiter_bytes():
                        # Check the decoded size *before* appending so an oversized chunk
                        # is never copied, stored or parsed.
                        if len(received) + len(chunk) > MAX_RESPONSE_BYTES:
                            return TransportResult(
                                status="SAFETY_BLOCKED",
                                reason_code="BODY_TOO_LARGE",
                                http_status=http_status,
                                elapsed_ms=_elapsed_ms(started),
                            )
                        received.extend(chunk)
                        if chunk:
                            body_chunk_received = True
                    return TransportResult(
                        status="OK",
                        reason_code="OK",
                        http_status=http_status,
                        body=bytes(received),
                        elapsed_ms=_elapsed_ms(started),
                    )
    except TimeoutError:
        # asyncio.timeout() raises the built-in TimeoutError; this is the overall cap.
        return _timeout_result(started, "OVERALL_TIMEOUT", phase, body_chunk_received)
    except httpx.TimeoutException as exc:
        return _timeout_result(started, _classify_timeout(exc), phase, body_chunk_received)
    except httpx.DecodingError:
        # A corrupt or unsupported encoding is a response-format problem, not a retry.
        return TransportResult(
            status="FORMAT_MISMATCH",
            reason_code="INVALID_JSON",
            elapsed_ms=_elapsed_ms(started),
        )
    except httpx.TransportError:
        return TransportResult(
            status="NETWORK_ERROR",
            reason_code="UNKNOWN_OUTCOME",
            elapsed_ms=_elapsed_ms(started),
            unknown_outcome=True,
        )


__all__ = [
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "LocalInputError",
    "TimeoutDiagnostic",
    "TimeoutKind",
    "TimeoutPhase",
    "TransportResult",
    "TransportStatus",
    "encode_body",
    "post_json",
]
