"""RFC 9562 UUIDv7 (millisecond time order + random tail) for local IDs."""

from __future__ import annotations

import os
import time
import uuid


def uuid7() -> str:
    millis = int(time.time() * 1000) & ((1 << 48) - 1)
    value = millis << 80
    value |= 0x7 << 76
    value |= int.from_bytes(os.urandom(10), "big") & ((1 << 74) - 1)
    value &= ~(0b11 << 62)
    value |= 0b10 << 62
    return str(uuid.UUID(int=value))
