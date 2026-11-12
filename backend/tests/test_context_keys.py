"""Key-ring loading, redaction, and fail-closed configuration tests."""

from __future__ import annotations

import json

import pytest
from aiagent_core import config
from aiagent_gateways.context_keys import KeyRing, KeyRingError
from aiagent_gateways.servers import load_key_ring


def test_key_ring_rotation_file_is_redacted_and_active_is_required(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_key, new_key = b"o" * 32, b"n" * 32
    path = tmp_path / "artifact-keys.json"
    path.write_text(
        json.dumps(
            {
                "active": "new-id",
                "keys": {"old-id": old_key.hex(), "new-id": new_key.hex()},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIAGENT_PROFILE", "production")
    monkeypatch.setenv("AIAGENT_CONTEXT_ARTIFACT_KEYS_FILE", str(path))
    ring = load_key_ring()
    assert ring.active == "new-id"
    assert ring.cipher("old-id") and ring.cipher("new-id")
    assert old_key.hex() not in repr(ring) and new_key.hex() not in repr(ring)
    assert "new-id" in repr(ring)


def test_missing_production_key_ring_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIAGENT_PROFILE", "production")
    monkeypatch.delenv("AIAGENT_CONTEXT_ARTIFACT_KEYS_FILE", raising=False)
    monkeypatch.delenv("AIAGENT_CONTEXT_ARTIFACT_KEY", raising=False)
    with pytest.raises(config.ConfigError, match="KEYS_FILE is required"):
        load_key_ring()


def test_key_ring_rejects_bad_active_key_id_and_length() -> None:
    with pytest.raises(KeyRingError, match="active key id"):
        KeyRing({"one": b"k" * 32}, "missing")
    with pytest.raises(KeyRingError, match="256-bit"):
        KeyRing({"one": b"k" * 31}, "one")
    with pytest.raises(KeyRingError, match="256-bit"):
        KeyRing.from_json(json.dumps({"active": "one", "keys": {"one": "00" * 31}}))
