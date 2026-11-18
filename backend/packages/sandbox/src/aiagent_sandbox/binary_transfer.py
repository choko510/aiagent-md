"""Bounded authenticated binary frames for an already authorized execution session.

The caller must authenticate and authorize the session and enforce lease revocation.
These frames do not create authority or make a received projection executable.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import struct
import time
from collections.abc import Callable, Iterable
from typing import BinaryIO

MAX_CHUNK = 64 * 1024
MAX_SOURCE = 128 * 1024 * 1024
_HEADER = struct.Struct(">QI")
_TAG_SIZE = 32
_DOMAIN = b"aiagent-projection-frame-v1\x00"


class TransferError(Exception):
    """A transfer failed authentication, bounds, ordering, or completeness."""


def _tag(key: bytes, binding: bytes, header: bytes, data: bytes) -> bytes:
    return hmac.digest(key, _DOMAIN + binding + header + data, "sha256")


def encode_chunk(key: bytes, binding: bytes, sequence: int, data: bytes) -> bytes:
    """Encode one chunk; binding is a trusted 32-byte digest of the session."""
    if len(key) < 32 or len(binding) != 32 or not 0 <= sequence < 2**64 or not 0 < len(data) <= MAX_CHUNK:
        raise TransferError("invalid frame")
    header = _HEADER.pack(sequence, len(data))
    return header + data + _tag(key, binding, header, data)


def _read_exact(stream: BinaryIO, size: int, deadline: float, active: Callable[[], bool]) -> bytes:
    result = bytearray()
    while len(result) < size:
        if time.monotonic() >= deadline or not active():
            raise TransferError("transfer expired or revoked")
        data = stream.read(size - len(result))
        if time.monotonic() >= deadline or not active():
            raise TransferError("transfer expired or revoked")
        if not isinstance(data, bytes) or not data or len(data) > size - len(result):
            raise TransferError("incomplete transfer")
        result.extend(data)
    return bytes(result)


def receive_chunks(
    stream: BinaryIO,
    sink: BinaryIO,
    *,
    key: bytes,
    binding: bytes,
    expected_size: int,
    expected_sha256: str,
    deadline: float,
    active: Callable[[], bool],
) -> None:
    """Receive into a quarantined sink; caller must discard it on every failure.

    The transport must impose a read timeout no later than deadline. The sink is
    not published here; it remains untrusted until projection proof validation.
    """
    if (
        len(key) < 32
        or len(binding) != 32
        or type(expected_size) is not int
        or not 0 < expected_size <= MAX_SOURCE
        or len(expected_sha256) != 64
        or any(c not in "0123456789abcdef" for c in expected_sha256)
    ):
        raise TransferError("invalid transfer parameters")
    digest = hashlib.sha256()
    received = 0
    sequence = 0
    while received < expected_size:
        header = _read_exact(stream, _HEADER.size, deadline, active)
        frame_sequence, size = _HEADER.unpack(header)
        if frame_sequence != sequence or not 0 < size <= min(MAX_CHUNK, expected_size - received):
            raise TransferError("invalid frame sequence or size")
        data = _read_exact(stream, size, deadline, active)
        tag = _read_exact(stream, _TAG_SIZE, deadline, active)
        if not hmac.compare_digest(tag, _tag(key, binding, header, data)):
            raise TransferError("frame authentication failed")
        if time.monotonic() >= deadline or not active():
            raise TransferError("transfer expired or revoked")
        written = sink.write(data)
        if written != len(data):
            raise TransferError("incomplete write")
        digest.update(data)
        received += size
        sequence += 1
    if not hmac.compare_digest(digest.hexdigest(), expected_sha256):
        raise TransferError("transfer digest mismatch")
    if time.monotonic() >= deadline or not active():
        raise TransferError("transfer expired or revoked")


def encode_chunks(key: bytes, binding: bytes, chunks: Iterable[bytes]) -> Iterable[bytes]:
    for sequence, chunk in enumerate(chunks):
        yield encode_chunk(key, binding, sequence, chunk)


async def receive_async(
    stream: asyncio.StreamReader,
    sink: BinaryIO,
    *,
    key: bytes,
    binding: bytes,
    expected_size: int,
    expected_sha256: str,
    deadline: float,
    active: Callable[[], bool],
) -> None:
    """Receive binary frames without blocking the supervisor event loop.

    Authentication and execution authorization are the caller's responsibility.
    On any error the caller must close and discard the quarantined sink.
    """
    if (
        len(key) < 32
        or len(binding) != 32
        or type(expected_size) is not int
        or not 0 < expected_size <= MAX_SOURCE
        or len(expected_sha256) != 64
        or any(c not in "0123456789abcdef" for c in expected_sha256)
    ):
        raise TransferError("invalid transfer parameters")

    async def read_exact(size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not active():
                raise TransferError("transfer expired or revoked")
            try:
                block = await asyncio.wait_for(stream.read(size - len(result)), timeout=min(remaining, 0.1))
            except TimeoutError:
                continue
            if not block:
                raise TransferError("incomplete transfer")
            result.extend(block)
        return bytes(result)

    digest = hashlib.sha256()
    received = 0
    sequence = 0
    while received < expected_size:
        header = await read_exact(_HEADER.size)
        frame_sequence, size = _HEADER.unpack(header)
        if frame_sequence != sequence or not 0 < size <= min(MAX_CHUNK, expected_size - received):
            raise TransferError("invalid frame sequence or size")
        data = await read_exact(size)
        tag = await read_exact(_TAG_SIZE)
        if not hmac.compare_digest(tag, _tag(key, binding, header, data)):
            raise TransferError("frame authentication failed")
        if time.monotonic() >= deadline or not active():
            raise TransferError("transfer expired or revoked")
        if sink.write(data) != len(data):
            raise TransferError("incomplete write")
        digest.update(data)
        received += size
        sequence += 1
    if not hmac.compare_digest(digest.hexdigest(), expected_sha256):
        raise TransferError("transfer digest mismatch")
    if time.monotonic() >= deadline or not active():
        raise TransferError("transfer expired or revoked")
