"""Canonical JSON and digests used for request hashes, snapshot identities and approval binding."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def sha256_hex(obj: Any) -> str:
    """Full 64-hex SHA-256 of the canonical JSON form."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
