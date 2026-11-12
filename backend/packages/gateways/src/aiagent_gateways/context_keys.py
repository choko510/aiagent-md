"""Context artifact key ring: key ids, rotation, legacy decryption and fail-closed key loss.

Keys never live in Git or in the database. Deployment mounts one JSON document:

    {"active": "2026-11", "keys": {"2026-10": "<64 hex>", "2026-11": "<64 hex>"}}

The active key encrypts new artifacts; every artifact row stores the key id used, and
reads select that key. Removing a key from the ring therefore makes its ciphertext
unreadable (KEY_UNAVAILABLE) instead of silently decrypting with another key.
The legacy single `AIAGENT_CONTEXT_ARTIFACT_KEY` input is accepted only outside
production and maps to key id `legacy` (also how pre-rotation rows are labelled).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_ID_RE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")
LEGACY_KEY_ID = "legacy"


class KeyRingError(ValueError):
    pass


def _decode_key(value: str) -> bytes:
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise KeyRingError("artifact key must be hex encoded") from exc
    if len(raw) != 32:
        raise KeyRingError("artifact keys must be 256-bit")
    return raw


@dataclass(frozen=True)
class KeyRing:
    keys: dict[str, bytes] = field(repr=False)
    active: str

    def __post_init__(self) -> None:
        if not KEY_ID_RE.fullmatch(self.active) or self.active not in self.keys:
            raise KeyRingError("active key id must name a key in the ring")
        for key_id, key in self.keys.items():
            if not KEY_ID_RE.fullmatch(key_id):
                raise KeyRingError("invalid artifact key id")
            if len(key) != 32:
                raise KeyRingError("artifact keys must be 256-bit")

    @classmethod
    def single(cls, key: bytes, key_id: str = LEGACY_KEY_ID) -> KeyRing:
        if len(key) != 32:
            raise KeyRingError("artifact keys must be 256-bit")
        return cls({key_id: key}, key_id)

    @classmethod
    def from_json(cls, text: str) -> KeyRing:
        try:
            doc = json.loads(text)
        except ValueError as exc:
            raise KeyRingError("artifact key ring is not valid JSON") from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("keys"), dict) or not isinstance(doc.get("active"), str):
            raise KeyRingError("artifact key ring needs active and keys")
        keys = {}
        for key_id, value in doc["keys"].items():
            if not isinstance(key_id, str) or not isinstance(value, str):
                raise KeyRingError("artifact key ring entries must be strings")
            keys[key_id] = _decode_key(value)
        if not keys:
            raise KeyRingError("artifact key ring is empty")
        return cls(keys, doc["active"])

    @classmethod
    def from_file(cls, path: str | Path) -> KeyRing:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def cipher(self, key_id: str) -> AESGCM:
        key = self.keys.get(key_id)
        if key is None:
            raise KeyError(key_id)
        return AESGCM(key)
