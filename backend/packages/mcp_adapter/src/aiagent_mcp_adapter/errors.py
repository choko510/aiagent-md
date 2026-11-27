"""Error taxonomy for the MCP adapter.

Every failure is classified once, at the transport/protocol boundary, and carries two independent facts:

* `retryable` - a bounded retry is semantically safe *and* no external effect can have happened.
* `effect_uncertain` - the request may have reached the server, so a non-read call must be recorded as
  UNKNOWN in the existing side-effect ledger and must never be replayed automatically.

The mapping to gateway/ledger states lives here (not in the SDK adapter) so the same classification is
used by discovery, the executor and tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    CONFIG_INVALID = "CONFIG_INVALID"
    ADAPTER_DISABLED = "ADAPTER_DISABLED"
    ENDPOINT_DENIED = "ENDPOINT_DENIED"
    EGRESS_DENIED = "EGRESS_DENIED"
    EGRESS_PRE_SEND_DENIED = "EGRESS_PRE_SEND_DENIED"
    CREDENTIAL_UNAVAILABLE = "CREDENTIAL_UNAVAILABLE"
    CREDENTIAL_REVOKED = "CREDENTIAL_REVOKED"
    CREDENTIAL_STALE = "CREDENTIAL_STALE"
    CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
    CONNECT_FAILED = "CONNECT_FAILED"
    TRANSPORT_TIMEOUT = "TRANSPORT_TIMEOUT"
    TRANSPORT_RESET = "TRANSPORT_RESET"
    HTTP_UNAUTHORIZED = "HTTP_UNAUTHORIZED"
    HTTP_FORBIDDEN = "HTTP_FORBIDDEN"
    HTTP_SESSION_LOST = "HTTP_SESSION_LOST"
    HTTP_RATE_LIMITED = "HTTP_RATE_LIMITED"
    HTTP_SERVER_ERROR = "HTTP_SERVER_ERROR"
    HTTP_UNEXPECTED_STATUS = "HTTP_UNEXPECTED_STATUS"
    INVALID_CONTENT_TYPE = "INVALID_CONTENT_TYPE"
    INVALID_JSONRPC = "INVALID_JSONRPC"
    INVALID_REQUEST_ID = "INVALID_REQUEST_ID"
    INVALID_NOTIFICATION = "INVALID_NOTIFICATION"
    PROTOCOL_VERSION_UNSUPPORTED = "PROTOCOL_VERSION_UNSUPPORTED"
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    SERVER_REQUEST_UNSUPPORTED = "SERVER_REQUEST_UNSUPPORTED"
    LIST_CHANGED_DURING_DISCOVERY = "LIST_CHANGED_DURING_DISCOVERY"
    CURSOR_LOOP = "CURSOR_LOOP"
    PAGE_LIMIT_EXCEEDED = "PAGE_LIMIT_EXCEEDED"
    TOOL_LIMIT_EXCEEDED = "TOOL_LIMIT_EXCEEDED"
    BYTES_LIMIT_EXCEEDED = "BYTES_LIMIT_EXCEEDED"
    DUPLICATE_TOOL_NAME = "DUPLICATE_TOOL_NAME"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    ARGUMENTS_INVALID = "ARGUMENTS_INVALID"
    OUTPUT_SCHEMA_MISMATCH = "OUTPUT_SCHEMA_MISMATCH"
    RESULT_UNSUPPORTED = "RESULT_UNSUPPORTED"
    RESULT_TRUNCATED = "RESULT_TRUNCATED"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    POLICY_DENIED = "POLICY_DENIED"
    STALE_TOOL_REF = "STALE_TOOL_REF"
    SNAPSHOT_EXPIRED = "SNAPSHOT_EXPIRED"
    SESSION_CLOSED = "SESSION_CLOSED"
    SESSION_FAILED = "SESSION_FAILED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SESSION_BINDING_MISMATCH = "SESSION_BINDING_MISMATCH"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    CANCELLED = "CANCELLED"
    TOOL_ERROR = "TOOL_ERROR"
    EFFECT_UNCERTAIN = "EFFECT_UNCERTAIN"


_EFFECT_UNCERTAIN = frozenset(
    {
        ErrorCode.TRANSPORT_TIMEOUT,
        ErrorCode.TRANSPORT_RESET,
        ErrorCode.HTTP_SESSION_LOST,
        ErrorCode.HTTP_UNAUTHORIZED,
        ErrorCode.HTTP_FORBIDDEN,
        ErrorCode.HTTP_RATE_LIMITED,
        ErrorCode.HTTP_SERVER_ERROR,
        ErrorCode.HTTP_UNEXPECTED_STATUS,
        ErrorCode.INVALID_JSONRPC,
        ErrorCode.INVALID_REQUEST_ID,
        ErrorCode.INVALID_CONTENT_TYPE,
        ErrorCode.OUTPUT_SCHEMA_MISMATCH,
        ErrorCode.RESULT_TRUNCATED,
        ErrorCode.EFFECT_UNCERTAIN,
        ErrorCode.CANCELLED,
    }
)

_RETRYABLE = frozenset(
    {
        # Only a failure proven to happen before any request byte can reach the proxy. A response
        # status (429/5xx) or a post-drain reset may still have produced an external effect.
        ErrorCode.CONNECT_TIMEOUT,
        ErrorCode.CONNECT_FAILED,
    }
)


@dataclass
class McpError(Exception):
    """A classified adapter error. `detail` is safe for logs (never a credential or raw body)."""

    code: ErrorCode
    detail: str = ""
    retryable: bool = False
    effect_uncertain: bool = False

    def __post_init__(self) -> None:
        # Derive the defaults from the taxonomy unless a boundary explicitly overrides them.
        if not self.retryable and self.code in _RETRYABLE:
            object.__setattr__(self, "retryable", True)
        if not self.effect_uncertain and self.code in _EFFECT_UNCERTAIN:
            object.__setattr__(self, "effect_uncertain", True)
        object.__setattr__(self, "detail", _safe_detail(self.detail))

    def __str__(self) -> str:
        return f"{self.code}:{self.detail}" if self.detail else str(self.code)

    def to_wire(self) -> dict[str, Any]:
        return {
            "code": str(self.code),
            "detail": self.detail,
            "retryable": self.retryable,
            "effectUncertain": self.effect_uncertain,
        }


_MAX_DETAIL = 200
_SECRET_MARKERS = ("authorization:", "proxy-authorization:", "cookie:", "set-cookie:", "bearer ", "token=")


def _safe_detail(detail: str) -> str:
    """Keep a short, single-line, secret-free diagnostic. Callers must pass message text, not raw frames."""
    if not detail:
        return ""
    flat = " ".join(str(detail).split())
    lowered = flat.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        return "[redacted]"
    return flat[:_MAX_DETAIL]


def ledger_effect(error: McpError, *, dispatched: bool, risk_class: str) -> str:
    """Map an error to the existing side-effect ledger vocabulary after the Gateway admitted the call.

    `dispatched` is True once the request may have left the process (permit obtained / HTTP request
    started). A read classified by the admin policy cannot write, so a failed read is FAILED; a
    dispatched write of unknown outcome is UNKNOWN.
    """
    if not dispatched:
        return "FAILED"
    if risk_class == "read":
        return "FAILED"
    if error.effect_uncertain or error.code in _EFFECT_UNCERTAIN:
        return "UNKNOWN"
    return "FAILED"


def classify_http_status(status: int) -> McpError:
    if status == 401:
        return McpError(ErrorCode.HTTP_UNAUTHORIZED, "http_401")
    if status == 403:
        return McpError(ErrorCode.HTTP_FORBIDDEN, "http_403")
    if status == 404:
        return McpError(ErrorCode.HTTP_SESSION_LOST, "http_404")
    if status == 429:
        return McpError(ErrorCode.HTTP_RATE_LIMITED, "http_429")
    if 500 <= status <= 599:
        return McpError(ErrorCode.HTTP_SERVER_ERROR, f"http_{status}")
    return McpError(ErrorCode.HTTP_UNEXPECTED_STATUS, f"http_{status}")
