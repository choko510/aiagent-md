"""UUIDv7 (RFC 9562) generation and validation. Only trusted server code generates public IDs."""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from datetime import UTC, datetime

_UUID7 = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_lock = threading.Lock()
_last_ms = 0
_seq = 0


def uuid7() -> str:
    """Monotonic within a process: 48-bit ms timestamp, 12-bit counter in rand_a, 62 random bits."""
    global _last_ms, _seq
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms <= _last_ms:
            _seq += 1
            if _seq > 0xFFF:  # counter exhausted inside one ms: borrow the next ms
                _last_ms += 1
                _seq = 0
            ms = _last_ms
        else:
            _last_ms, _seq = ms, int.from_bytes(os.urandom(2), "big") & 0x3FF
        seq = _seq
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | seq << 64 | 0b10 << 62 | rand_b
    return str(uuid.UUID(int=value))


def is_uuid7(value: object) -> bool:
    return type(value) is str and bool(_UUID7.match(value))


def require_uuid7(value: object, what: str = "id") -> str:
    if not is_uuid7(value):
        raise ValueError(f"{what} is not a canonical UUIDv7")
    assert isinstance(value, str)
    return value


def utc_instant(dt: datetime | None = None) -> str:
    """Canonical UtcInstant: YYYY-MM-DDTHH:MM:SS.mmmZ."""
    dt = (dt or datetime.now(UTC)).astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
