"""Direct-TLS device connection candidate. No Tool dispatch or production routing."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import uuid
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .connections import ConnectionChannel, ConnectionRejected, ConnectionService
from .pairing import PairingRejected, PairingService

MAX_BYTES = 8192
SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "device-client/contracts/device-connection-v1.schema.json").read_text(
        encoding="utf-8"
    )
)
Draft202012Validator.check_schema(SCHEMA)
VALIDATOR = Draft202012Validator(SCHEMA)
PAIRING_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "device-client/contracts/device-pairing-v1.schema.json").read_text(
        encoding="utf-8"
    )
)
Draft202012Validator.check_schema(PAIRING_SCHEMA)
PAIRING_VALIDATOR = Draft202012Validator(PAIRING_SCHEMA)
_LOGGER = logging.getLogger("aiagent.device-wss")
_LOGGER.setLevel(logging.WARNING)


class InvalidFrame(Exception):
    pass


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidFrame()
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise InvalidFrame()


def decode_frame(raw: str | bytes, expected: str, *, pairing: bool = False) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_BYTES:
        raise InvalidFrame()
    try:
        data = json.loads(raw, object_pairs_hook=_unique, parse_constant=_invalid_constant)
    except (ValueError, RecursionError) as exc:
        raise InvalidFrame() from exc
    validator = PAIRING_VALIDATOR if pairing else VALIDATOR
    if not isinstance(data, dict) or data.get("kind") != expected or not validator.is_valid(data):
        raise InvalidFrame()
    if any(isinstance(value, str) and any(ord(ch) < 32 or ord(ch) == 127 for ch in value) for value in data.values()):
        raise InvalidFrame()
    for key in ("generation", "sequence", "issuedMs", "expiresMs"):
        if key in data and int(data[key]) > 2**63 - 1:
            raise InvalidFrame()
    return data


async def _read(socket: ServerConnection, expected: str) -> dict[str, Any]:
    return decode_frame(await asyncio.wait_for(socket.recv(), timeout=15), expected)


async def _send(socket: ServerConnection, payload: dict[str, Any]) -> None:
    # The same schema governs outgoing frames; never emit secrets via diagnostics.
    if not VALIDATOR.is_valid(payload):
        raise InvalidFrame()
    await asyncio.wait_for(socket.send(json.dumps(payload, separators=(",", ":"))), timeout=15)


class DeviceWss:
    def __init__(self, service: ConnectionService, pairing: PairingService | None = None) -> None:
        self._service = service
        self._pairing = pairing

    async def _pair(self, socket: ServerConnection) -> None:
        if self._pairing is None:
            raise InvalidFrame()
        frame = decode_frame(await asyncio.wait_for(socket.recv(), timeout=15), "ATTACH", pairing=True)
        tenant, owner = frame["tenantId"], frame["ownerId"]
        request_id = uuid.UUID(frame["pairingId"])
        token = bytes.fromhex(frame["token"])
        code = await asyncio.to_thread(
            self._pairing.attach,
            tenant,
            owner,
            request_id,
            token,
            bytes.fromhex(frame["publicKey"]),
            bytes.fromhex(frame["bindingDigest"]),
            bytes.fromhex(frame["signature"]),
        )
        del frame
        response = {
            "version": "1.0",
            "kind": "CODE",
            "code": code.code,
            "publicKeySha256": code.public_key_sha256,
            "bindingDigest": code.binding_digest.hex(),
        }
        if not PAIRING_VALIDATOR.is_valid(response):
            raise InvalidFrame()
        await asyncio.wait_for(socket.send(json.dumps(response, separators=(",", ":"))), timeout=15)
        # The authenticated owner must approve separately; the socket cannot do so.
        decode_frame(await asyncio.wait_for(socket.recv(), timeout=120), "CONSUME", pairing=True)
        identity_id, device_id = await asyncio.to_thread(self._pairing.consume, tenant, owner, request_id, token)
        response = {"version": "1.0", "kind": "PAIRED", "identityId": str(identity_id), "deviceId": str(device_id)}
        if not PAIRING_VALIDATOR.is_valid(response):
            raise InvalidFrame()
        await asyncio.wait_for(socket.send(json.dumps(response, separators=(",", ":"))), timeout=15)
        await socket.close(code=1000)

    async def handler(self, socket: ServerConnection) -> None:
        channel = ConnectionChannel()
        tenant = owner = None
        identity = None
        try:
            if (
                socket.transport.get_extra_info("ssl_object") is None
                or socket.subprotocol != "aiagent-device.v1"
                or socket.request is None
            ):
                raise InvalidFrame()
            if socket.request.path == "/v1/device/pair":
                await self._pair(socket)
                return
            if socket.request.path != "/v1/device/connect":
                raise InvalidFrame()
            hello = await _read(socket, "HELLO")
            tenant, owner, identity = hello["tenantId"], hello["ownerId"], uuid.UUID(hello["identityId"])
            challenge = await asyncio.to_thread(self._service.challenge, tenant, owner, identity, channel)
            await _send(
                socket,
                {
                    "version": "1.1",
                    "kind": "CHALLENGE",
                    "nonce": challenge.nonce.hex(),
                    "channelDigest": challenge.channel_digest.hex(),
                    "generation": str(challenge.generation),
                    "issuedMs": str(challenge.issued_ms),
                    "expiresMs": str(challenge.expires_ms),
                },
            )
            proof = await _read(socket, "PROOF")
            session = await asyncio.to_thread(
                self._service.authenticate, tenant, owner, identity, channel, bytes.fromhex(proof["signature"])
            )
            await _send(
                socket,
                {
                    "version": "1.1",
                    "kind": "ACCEPTED",
                    "generation": str(session.generation),
                    "expiresMs": str(session.expires_ms),
                },
            )
            sequence = 0
            while True:
                heartbeat = await _read(socket, "HEARTBEAT")
                next_sequence = int(heartbeat["sequence"])
                if int(heartbeat["generation"]) != session.generation or next_sequence != sequence + 1:
                    raise InvalidFrame()
                await asyncio.to_thread(
                    self._service.signed_heartbeat,
                    tenant,
                    owner,
                    identity,
                    channel,
                    session.generation,
                    session.token,
                    next_sequence,
                    bytes.fromhex(heartbeat["signature"]),
                )
                sequence = next_sequence
                await _send(
                    socket,
                    {
                        "version": "1.1",
                        "kind": "HEARTBEAT_ACK",
                        "generation": str(session.generation),
                        "sequence": str(sequence),
                    },
                )
        except (InvalidFrame, PairingRejected, ConnectionRejected, ConnectionClosed, TimeoutError, ValueError):
            await socket.close(code=1008)
        finally:
            if tenant is not None and owner is not None and identity is not None:
                try:
                    await asyncio.to_thread(self._service.disconnect, tenant, owner, identity, channel)
                except (PairingRejected, ConnectionRejected):
                    pass

    def listen(self, host: str, port: int, tls: ssl.SSLContext, *, development_only: bool = False) -> Any:
        """Direct TLS on loopback only until production gateway gates are met."""
        if development_only is not True or host not in ("127.0.0.1", "::1"):
            raise ValueError("development-only loopback required")
        if tls.protocol != ssl.PROTOCOL_TLS_SERVER or tls.minimum_version < ssl.TLSVersion.TLSv1_2:
            raise ValueError("direct TLS server context required")
        return serve(
            self.handler,
            host,
            port,
            ssl=tls,
            origins=[None],
            subprotocols=["aiagent-device.v1"],
            compression=None,
            max_size=MAX_BYTES,
            max_queue=4,
            open_timeout=10,
            server_header=None,
            logger=_LOGGER,
        )
