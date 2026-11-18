from __future__ import annotations

import hashlib
import io
import time

import pytest
from aiagent_sandbox.binary_transfer import TransferError, encode_chunk, receive_chunks

KEY = b"k" * 32
BINDING = b"b" * 32


def receive(wire: bytes, payload: bytes, *, binding: bytes = BINDING, active: bool = True) -> bytes:
    sink = io.BytesIO()
    receive_chunks(
        io.BytesIO(wire),
        sink,
        key=KEY,
        binding=binding,
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        deadline=time.monotonic() + 5,
        active=lambda: active,
    )
    return sink.getvalue()


def test_authenticated_chunk_round_trip() -> None:
    chunks = [b"a" * 65536, b"hello"]
    wire = b"".join(encode_chunk(KEY, BINDING, i, chunk) for i, chunk in enumerate(chunks))
    assert receive(wire, b"".join(chunks)) == b"".join(chunks)


def test_rejects_wrong_order_duplicate_and_disconnect() -> None:
    first = encode_chunk(KEY, BINDING, 0, b"a")
    second = encode_chunk(KEY, BINDING, 1, b"b")
    for wire in (second + first, first + first, (first + second)[:-1]):
        with pytest.raises(TransferError):
            receive(wire, b"ab")


def test_rejects_tampering_scope_digest_and_revocation() -> None:
    wire = encode_chunk(KEY, BINDING, 0, b"payload")
    for altered, payload, binding, active in (
        (wire[:-1] + bytes([wire[-1] ^ 1]), b"payload", BINDING, True),
        (wire, b"payload", b"x" * 32, True),
        (wire, b"xxxxxxx", BINDING, True),
        (wire, b"payload", BINDING, False),
    ):
        with pytest.raises(TransferError):
            receive(altered, payload, binding=binding, active=active)


def test_expired_transfer_rejected() -> None:
    with pytest.raises(TransferError):
        receive_chunks(
            io.BytesIO(encode_chunk(KEY, BINDING, 0, b"a")),
            io.BytesIO(),
            key=KEY,
            binding=BINDING,
            expected_size=1,
            expected_sha256=hashlib.sha256(b"a").hexdigest(),
            deadline=time.monotonic() - 1,
            active=lambda: True,
        )


def test_async_transfer_and_deadline() -> None:
    import asyncio

    from aiagent_sandbox.binary_transfer import receive_async

    async def exercise() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(encode_chunk(KEY, BINDING, 0, b"source"))
        sink = io.BytesIO()
        await receive_async(
            reader,
            sink,
            key=KEY,
            binding=BINDING,
            expected_size=6,
            expected_sha256=hashlib.sha256(b"source").hexdigest(),
            deadline=time.monotonic() + 1,
            active=lambda: True,
        )
        assert sink.getvalue() == b"source"
        with pytest.raises(TransferError, match="expired"):
            await receive_async(
                asyncio.StreamReader(),
                io.BytesIO(),
                key=KEY,
                binding=BINDING,
                expected_size=6,
                expected_sha256=hashlib.sha256(b"source").hexdigest(),
                deadline=time.monotonic() + 0.05,
                active=lambda: True,
            )

    asyncio.run(exercise())
