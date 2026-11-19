"""Trusted adapter to the canonical, signature-verified command receipt store."""

from __future__ import annotations

import time

from .execution_lifecycle import ExecutionLifecycle


class CommandReceiptReader:
    """Bind receipt lookup to one Gateway lifecycle and a protected verifier key."""

    def __init__(self, lifecycle: ExecutionLifecycle, receipt_key: bytes, *, timeout_seconds: float = 5.0) -> None:
        if len(receipt_key) < 32 or not 0 < timeout_seconds <= 30:
            raise ValueError("invalid command receipt reader configuration")
        self._lifecycle, self._key, self._timeout = lifecycle, receipt_key, timeout_seconds

    def __call__(self, execution_id: str, receipt_id: str) -> tuple[dict[str, object], str]:
        return self._lifecycle.fetch_command_receipt(
            execution_id, receipt_id, self._key, deadline=time.monotonic() + self._timeout
        )
