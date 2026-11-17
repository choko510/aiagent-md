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
from .catalog import ProviderSpec, validate_spec

MAX_REQUEST_BYTES: Final = 4096
MAX_RESPONSE_BYTES: Final = 131072
OVERALL_TIMEOUT_SECONDS: Final = 25.0
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
        timeout=httpx.Timeout(20.0, connect=5.0, read=15.0, write=5.0, pool=5.0),
        transport=transport,
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _is_json_content_type(value: str | None) -> bool:
    if value is None:
        return False
    media_type = value.split(";")[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


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


async def post_json(spec: ProviderSpec, body: dict[str, object]) -> TransportResult:
    """POST one fixed body to the spec's fixed endpoint. Retries: none, ever."""
    validate_spec(spec)
    try:
        encoded = encode_body(body)
    except LocalInputError as exc:
        return TransportResult(status="LOCAL_INPUT_INVALID", reason_code=exc.reason_code)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    started = time.monotonic()
    try:
        async with asyncio.timeout(OVERALL_TIMEOUT_SECONDS):
            client = _new_client()
            async with client:
                async with client.stream("POST", spec.endpoint, content=encoded, headers=headers) as response:
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
                    received = bytearray()
                    async for chunk in response.aiter_bytes():
                        received.extend(chunk)
                        if len(received) > MAX_RESPONSE_BYTES:
                            return TransportResult(
                                status="SAFETY_BLOCKED",
                                reason_code="BODY_TOO_LARGE",
                                http_status=http_status,
                                elapsed_ms=_elapsed_ms(started),
                            )
                    return TransportResult(
                        status="OK",
                        reason_code="OK",
                        http_status=http_status,
                        body=bytes(received),
                        elapsed_ms=_elapsed_ms(started),
                    )
    except TimeoutError:
        return TransportResult(
            status="TIMEOUT",
            reason_code="UNKNOWN_OUTCOME",
            elapsed_ms=_elapsed_ms(started),
            unknown_outcome=True,
        )
    except httpx.TimeoutException:
        return TransportResult(
            status="TIMEOUT",
            reason_code="UNKNOWN_OUTCOME",
            elapsed_ms=_elapsed_ms(started),
            unknown_outcome=True,
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
    "TransportResult",
    "TransportStatus",
    "encode_body",
    "post_json",
]
