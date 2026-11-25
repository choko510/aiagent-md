"""Stable, non-secret failure codes for ingestion (15-01 §5).

Messages returned to clients are codes and bounded details. Parser tracebacks,
file names and document text never leave this module through an exception.
"""

from __future__ import annotations

from typing import Final

# The closed set of MmiParserError values from the canonical draft. Adding a
# value is a contract review, not a local decision.
PARSER_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "MIME_MISMATCH",
        "AMBIGUOUS_FORMAT",
        "MAX_INPUT",
        "EXPANSION_LIMIT",
        "PIXEL_LIMIT",
        "PAGE_LIMIT",
        "ROW_LIMIT",
        "CELL_LIMIT",
        "PARSE_TIMEOUT",
        "PARSER_CRASH",
        "BAD_ARCHIVE",
        "BAD_XML",
        "PASSWORD_REQUIRED",
        "UNSUPPORTED_FORMAT",
        "UNSUPPORTED_ACTIVE_CONTENT",
        "EXTERNAL_REFERENCE",
        "INVALID_ENCODING",
        "SOURCE_MISMATCH",
        "POLICY_DENIED",
        "OWNER_REVOKED",
        "STALE_LEASE",
        "QUARANTINED",
        "MODEL_BUDGET",
        "CANCELLED",
    }
)

# Ingestion-lifecycle codes are deliberately separate from MmiParserError so a
# 404/409 on an upload cannot be mistaken for a parser verdict.
UPLOAD_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "UPLOAD_NOT_FOUND",
        "UPLOAD_EXPIRED",
        "UPLOAD_ABORTED",
        "UPLOAD_COMPLETE",
        "UPLOAD_OFFSET_MISMATCH",
        "UPLOAD_TOO_LARGE",
        "UPLOAD_CHECKSUM_MISMATCH",
        "UPLOAD_SIZE_MISMATCH",
        "UPLOAD_OWNER_MISMATCH",
        "UPLOAD_IDEMPOTENCY_CONFLICT",
    }
)

_HTTP_STATUS: Final[dict[str, int]] = {
    "MIME_MISMATCH": 415,
    "UNSUPPORTED_FORMAT": 415,
    "QUARANTINED": 422,
    "POLICY_DENIED": 403,
    "OWNER_REVOKED": 410,
    "STALE_LEASE": 409,
    "SOURCE_MISMATCH": 409,
    "CANCELLED": 409,
    "MAX_INPUT": 413,
    "MODEL_BUDGET": 402,
}


class MmiError(Exception):
    """A bounded, classified ingestion failure.

    `detail` is a short machine-safe explanation (never file content or a raw
    library message). `retryable` is only true for failures that the §8 fault
    matrix allows to be retried against the same immutable source.
    """

    def __init__(
        self,
        code: str,
        detail: str = "",
        *,
        retryable: bool = False,
        http_status: int | None = None,
    ) -> None:
        if code not in PARSER_ERROR_CODES and code not in UPLOAD_ERROR_CODES and code != "INTERNAL":
            raise ValueError(f"unknown ingestion error code: {code}")
        super().__init__(code)
        self.code = code
        self.detail = detail[:500]
        self.retryable = retryable and code in {"PARSE_TIMEOUT", "PARSER_CRASH", "BAD_ARCHIVE", "STALE_LEASE"}
        self.http_status = http_status if http_status is not None else _HTTP_STATUS.get(code, 422)

    def to_dict(self) -> dict[str, object]:
        return {"errorCode": self.code, "detail": self.detail, "retryable": self.retryable}

    def __repr__(self) -> str:  # pragma: no cover - formatting only
        return f"MmiError({self.code!r}, detail={self.detail!r})"
