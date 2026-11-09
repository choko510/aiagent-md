"""Authenticated local RPC wire format (pure; transport lives in aiagent_core.rpc and aiagent_sandbox).

Frame = 4-byte big-endian length + canonical JSON. Every request names its caller identity and is MACed with a key
that only that identity and the server hold (one key per caller identity, never shared between processes). The
server also binds the peer's OS identity (SO_PEERCRED uid on Unix sockets) when configured. Responses are MACed with
the same key over the request nonce so a caller can detect a spoofed endpoint. Replay: timestamp window + nonce cache.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
import time
from collections import OrderedDict
from typing import Any

MAX_FRAME = 4 * 1024 * 1024
CLOCK_SKEW_MS = 30_000
PROTOCOL = 1


class RpcAuthError(Exception):
    """Authentication failed. The message is a fixed code; never echo request content."""


class RpcError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def encode_frame(obj: dict[str, Any]) -> bytes:
    data = _canon(obj)
    if len(data) > MAX_FRAME:
        raise RpcError("FRAME_TOO_LARGE")
    return struct.pack(">I", len(data)) + data


def decode_length(header: bytes) -> int:
    (n,) = struct.unpack(">I", header)
    if n > MAX_FRAME:
        raise RpcError("FRAME_TOO_LARGE")
    return n


def _mac(key: bytes, obj: dict[str, Any]) -> str:
    return hmac.new(key, _canon(obj), hashlib.sha256).hexdigest()


def sign_request(key: bytes, caller: str, method: str, body: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    env = {
        "v": PROTOCOL,
        "caller": caller,
        "ts": now_ms if now_ms is not None else int(time.time() * 1000),
        "nonce": os.urandom(16).hex(),
        "method": method,
        "body": body,
    }
    env["mac"] = _mac(key, env)
    return env


def sign_response(key: bytes, nonce: str, frame: dict[str, Any]) -> dict[str, Any]:
    out = {**frame, "re": nonce}
    out["mac"] = _mac(key, out)
    return out


def verify_response(key: bytes, nonce: str, frame: dict[str, Any]) -> dict[str, Any]:
    mac = frame.get("mac")
    unsigned = {k: v for k, v in frame.items() if k != "mac"}
    if type(mac) is not str or unsigned.get("re") != nonce or not hmac.compare_digest(mac, _mac(key, unsigned)):
        raise RpcAuthError("RESPONSE_AUTH")
    return unsigned


class NonceCache:
    def __init__(self, size: int = 100_000) -> None:
        self.size = size
        self._seen: OrderedDict[str, int] = OrderedDict()

    def check_and_add(self, nonce: str, ts: int) -> bool:
        now = int(time.time() * 1000)
        while self._seen and next(iter(self._seen.values())) < now - 2 * CLOCK_SKEW_MS:
            self._seen.popitem(last=False)
        if nonce in self._seen:
            return False
        self._seen[nonce] = ts
        if len(self._seen) > self.size:
            self._seen.popitem(last=False)
        return True


def verify_request(
    keys: dict[str, bytes], env: Any, nonces: NonceCache, *, now_ms: int | None = None
) -> tuple[str, str, dict[str, Any], str]:
    """Return (caller, method, body, nonce) or raise RpcAuthError with a fixed code."""
    if not isinstance(env, dict) or env.get("v") != PROTOCOL:
        raise RpcAuthError("BAD_ENVELOPE")
    caller, method, body, nonce, ts, mac = (env.get(k) for k in ("caller", "method", "body", "nonce", "ts", "mac"))
    if type(caller) is not str or type(method) is not str or not isinstance(body, dict):
        raise RpcAuthError("BAD_ENVELOPE")
    if type(nonce) is not str or len(nonce) != 32 or type(ts) is not int or type(mac) is not str:
        raise RpcAuthError("BAD_ENVELOPE")
    key = keys.get(caller)
    if key is None:
        raise RpcAuthError("UNKNOWN_CALLER")
    unsigned = {k: v for k, v in env.items() if k != "mac"}
    if set(unsigned) != {"v", "caller", "ts", "nonce", "method", "body"}:
        raise RpcAuthError("BAD_ENVELOPE")
    if not hmac.compare_digest(mac, _mac(key, unsigned)):
        raise RpcAuthError("BAD_MAC")
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    if abs(now - ts) > CLOCK_SKEW_MS:
        raise RpcAuthError("STALE_REQUEST")
    if not nonces.check_and_add(nonce, ts):
        raise RpcAuthError("REPLAY")
    return caller, method, body, nonce
