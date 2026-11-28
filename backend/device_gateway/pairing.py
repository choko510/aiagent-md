"""PostgreSQL pairing core, isolated until API/RLS roles and wire contracts are integrated.

No method dispatches tools or grants authority. The API adapter MUST pass the
existing authenticated Scope, never values copied from the request body.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

import psycopg
from nacl.bindings import crypto_core_ed25519_is_valid_point
from nacl.exceptions import BadSignatureError, CryptoError
from nacl.signing import VerifyKey
from psycopg.rows import dict_row

TTL_MS = 120_000
MAX_FAILURES = 5
DOMAIN = b"aiagent-device-pairing-proof-candidate-v1"


class AuthenticatedScope(Protocol):
    tenant_id: str
    user_id: str


class PairingRejected(Exception):
    """Intentionally does not reveal whether an ID, owner or secret existed."""


@dataclass(frozen=True, slots=True)
class Bootstrap:
    pairing_id: uuid.UUID
    tenant_id: str
    owner_user_id: str
    identity_id: uuid.UUID
    device_id: uuid.UUID
    revocation_generation: int
    nonce: bytes = field(repr=False)
    token: bytes = field(repr=False)
    issued_ms: int
    expires_ms: int
    server_origin: str


@dataclass(frozen=True, slots=True)
class DeviceCode:
    code: str = field(repr=False)
    public_key_sha256: str
    binding_digest: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class PendingPairing:
    pairing_id: uuid.UUID
    identity_id: uuid.UUID
    device_id: uuid.UUID
    server_origin: str
    public_key_sha256: str
    binding_digest: bytes
    expires_ms: int


@dataclass(frozen=True, slots=True)
class PairingStatus:
    """Owner-scoped reconciliation evidence; never contains a bootstrap token or code."""

    state: str
    identity_id: uuid.UUID
    device_id: uuid.UUID
    public_key_sha256: str | None
    binding_digest: bytes | None


def pairing_proof_bytes(
    pairing_id: uuid.UUID, binding_digest: bytes, public_key: bytes, nonce: bytes, issued_ms: int, expires_ms: int
) -> bytes:
    """Candidate binary signature input shared with Rust; not a JSON wire contract."""
    if (
        not isinstance(pairing_id, uuid.UUID)
        or pairing_id.int == 0
        or not all(isinstance(v, bytes) for v in (binding_digest, public_key, nonce))
        or type(issued_ms) is not int
        or type(expires_ms) is not int
        or len(binding_digest) != 32
        or len(public_key) != 32
        or len(nonce) != 32
        or nonce == bytes(32)
        or not 0 <= issued_ms < expires_ms < 2**63
        or expires_ms - issued_ms > TTL_MS
    ):
        raise PairingRejected()
    return (
        DOMAIN
        + pairing_id.bytes
        + binding_digest
        + public_key
        + nonce
        + issued_ms.to_bytes(8, "big")
        + expires_ms.to_bytes(8, "big")
    )


def pairing_proof_v2(
    pairing_id: uuid.UUID,
    binding_digest: bytes,
    public_key: bytes,
    nonce: bytes,
    issued_ms: int,
    expires_ms: int,
    tenant: str,
    owner: str,
    identity: str,
    device: str,
    origin: str,
    revocation_generation: int,
) -> bytes:
    """Bind server-verifiable bootstrap fields; the opaque local digest is not SID attestation."""
    legacy = pairing_proof_bytes(pairing_id, binding_digest, public_key, nonce, issued_ms, expires_ms)
    if type(revocation_generation) is not int or not 0 <= revocation_generation < 2**63:
        raise PairingRejected()
    parts = (tenant, owner, identity, device, origin)
    if any(not isinstance(part, str) or not part or len(part.encode("utf-8")) > 512 for part in parts):
        raise PairingRejected()
    transcript = bytearray(b"aiagent-device-pairing-proof-candidate-v2")
    transcript.extend(legacy[len(DOMAIN) :])
    for part in parts:
        encoded = part.encode("utf-8")
        transcript.extend(len(encoded).to_bytes(8, "big"))
        transcript.extend(encoded)
    transcript.extend(revocation_generation.to_bytes(8, "big"))
    return bytes(transcript)


def server_binding_digest(
    commitment: bytes, tenant: str, owner: str, identity: str, device: str, origin: str, generation: int
) -> bytes:
    if not isinstance(commitment, bytes) or len(commitment) != 32 or commitment == bytes(32):
        raise PairingRejected()
    if type(generation) is not int or not 0 <= generation < 2**63:
        raise PairingRejected()
    digest = hashlib.sha256(b"aiagent-device-server-binding-v1" + commitment)
    for part in (tenant, owner, identity, device, origin):
        if not isinstance(part, str) or not part or len(part.encode("utf-8")) > 512:
            raise PairingRejected()
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    digest.update(generation.to_bytes(8, "big"))
    return digest.digest()


class PairingService:
    def __init__(self, dsn: str, server_origin: str, code_key: bytes) -> None:
        origin = urlsplit(server_origin)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or origin.path not in ("", "/")
            or origin.query
            or origin.fragment
            or len(server_origin) > 512
            or len(code_key) != 32
        ):
            raise ValueError("invalid gateway configuration")
        try:
            port = origin.port
        except ValueError as exc:
            raise ValueError("invalid gateway configuration") from exc
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("invalid gateway configuration")
        host = origin.hostname
        if not host.isascii() or any(c.isspace() for c in server_origin):
            raise ValueError("invalid gateway configuration")
        try:
            address = ipaddress.ip_address(host)
            canonical_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
        except ValueError:
            if not re.fullmatch(
                r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", host
            ):
                raise ValueError("invalid gateway configuration") from None
            canonical_host = host
        canonical = f"https://{canonical_host}" + (f":{port}" if port not in (None, 443) else "")
        if server_origin not in (canonical, canonical + "/"):
            raise ValueError("gateway origin must be canonical")
        # CVE-2025-69277: older libsodium builds may accept mixed-order points.
        # Test the linked implementation, including when PyNaCl uses system sodium.
        mixed_order = bytes([0x95]) + bytes([0x99]) * 31
        if crypto_core_ed25519_is_valid_point(bytes(32)) or crypto_core_ed25519_is_valid_point(mixed_order):
            raise RuntimeError("UNSAFE_ED25519_BACKEND")
        self._dsn = dsn
        self._origin = canonical
        self._code_key = code_key

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row, connect_timeout=5)

    @staticmethod
    def _scope(conn: psycopg.Connection, tenant: str, owner: str) -> None:
        if not tenant or len(tenant) > 256 or not owner or len(owner) > 256:
            raise PairingRejected()
        conn.execute("SELECT set_config('lock_timeout', '2s', true)")
        conn.execute("SELECT set_config('statement_timeout', '5s', true)")
        conn.execute("SELECT set_config('aiagent.tenant_id', %s, true)", (tenant,))
        conn.execute("SELECT set_config('aiagent.owner_id', %s, true)", (owner,))

    @staticmethod
    def _now(conn: psycopg.Connection) -> int:
        return int(
            conn.execute("SELECT floor(extract(epoch from clock_timestamp()) * 1000)::bigint AS n").fetchone()["n"]
        )

    def _code_digest(self, request_id: uuid.UUID, code: str) -> bytes:
        return hmac.digest(self._code_key, b"device-pair-code-v1" + request_id.bytes + code.encode("ascii"), "sha256")

    def begin(self, scope: AuthenticatedScope) -> Bootstrap:
        if not scope.tenant_id or not scope.user_id or len(scope.tenant_id) > 256 or len(scope.user_id) > 256:
            raise PairingRejected()
        request_id, identity_id, device_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        token, nonce = secrets.token_bytes(32), secrets.token_bytes(32)
        with self._connect() as conn, conn.transaction():
            self._scope(conn, scope.tenant_id, scope.user_id)
            now = self._now(conn)
            conn.execute(
                "INSERT INTO device_gateway.pairing_requests "
                "(id, tenant_id, owner_id, identity_id, device_id, origin, token_digest, nonce, issued_ms, expires_ms, state) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'WAITING_DEVICE')",
                (
                    request_id,
                    scope.tenant_id,
                    scope.user_id,
                    identity_id,
                    device_id,
                    self._origin,
                    hashlib.sha256(token).digest(),
                    nonce,
                    now,
                    now + TTL_MS,
                ),
            )
        return Bootstrap(
            request_id,
            scope.tenant_id,
            scope.user_id,
            identity_id,
            device_id,
            0,
            nonce,
            token,
            now,
            now + TTL_MS,
            self._origin,
        )

    @staticmethod
    def _fail(conn: psycopg.Connection, request_id: uuid.UUID) -> None:
        conn.execute(
            "UPDATE device_gateway.pairing_requests SET failures=failures+1, "
            "state=CASE WHEN failures+1 >= %s THEN 'LOCKED' ELSE state END WHERE id=%s",
            (MAX_FAILURES, request_id),
        )

    def attach(
        self,
        tenant_hint: str,
        owner_hint: str,
        request_id: uuid.UUID,
        token: bytes,
        public_key: bytes,
        binding_digest: bytes,
        signature: bytes,
        local_commitment: bytes = b"",
    ) -> DeviceCode:
        accepted: DeviceCode | None = None
        with self._connect() as conn, conn.transaction():
            self._scope(conn, tenant_hint, owner_hint)
            row = conn.execute(
                "SELECT * FROM device_gateway.pairing_requests WHERE id=%s AND tenant_id=%s AND owner_id=%s FOR UPDATE",
                (request_id, tenant_hint, owner_hint),
            ).fetchone()
            now = self._now(conn)
            if row is not None and row["state"] == "WAITING_DEVICE" and row["issued_ms"] <= now < row["expires_ms"]:
                valid = (
                    isinstance(token, bytes)
                    and len(token) == 32
                    and hmac.compare_digest(hashlib.sha256(token).digest(), row["token_digest"])
                )
                try:
                    if not valid or not isinstance(signature, bytes) or len(signature) != 64:
                        raise PairingRejected()
                    expected_binding = server_binding_digest(
                        local_commitment,
                        row["tenant_id"],
                        row["owner_id"],
                        str(row["identity_id"]),
                        str(row["device_id"]),
                        row["origin"],
                        0,
                    )
                    if not hmac.compare_digest(binding_digest, expected_binding):
                        raise PairingRejected()
                    payload = pairing_proof_v2(
                        request_id,
                        binding_digest,
                        public_key,
                        row["nonce"],
                        row["issued_ms"],
                        row["expires_ms"],
                        row["tenant_id"],
                        row["owner_id"],
                        str(row["identity_id"]),
                        str(row["device_id"]),
                        row["origin"],
                        0,
                    )
                    if not crypto_core_ed25519_is_valid_point(public_key):
                        raise PairingRejected()
                    VerifyKey(public_key).verify(payload, signature)
                except (ValueError, BadSignatureError, CryptoError, PairingRejected):
                    self._fail(conn, request_id)
                else:
                    code = f"{secrets.randbelow(100_000_000):08d}"
                    conn.execute(
                        "UPDATE device_gateway.pairing_requests SET state='WAITING_APPROVAL', "
                        "public_key=%s, binding_digest=%s, code_digest=%s WHERE id=%s",
                        (public_key, binding_digest, self._code_digest(request_id, code), request_id),
                    )
                    accepted = DeviceCode(code, hashlib.sha256(public_key).hexdigest(), binding_digest)
        if accepted is None:
            raise PairingRejected()
        return accepted

    def approve(
        self,
        scope: AuthenticatedScope,
        request_id: uuid.UUID,
        code: str,
        expected_key_sha256: str,
        expected_binding: bytes,
    ) -> None:
        accepted = False
        with self._connect() as conn, conn.transaction():
            self._scope(conn, scope.tenant_id, scope.user_id)
            row = conn.execute(
                "SELECT * FROM device_gateway.pairing_requests WHERE id=%s AND tenant_id=%s AND owner_id=%s FOR UPDATE",
                (request_id, scope.tenant_id, scope.user_id),
            ).fetchone()
            now = self._now(conn)
            if row is not None and row["state"] == "WAITING_APPROVAL" and row["issued_ms"] <= now < row["expires_ms"]:
                valid_code = isinstance(code, str) and len(code) == 8 and code.isascii() and code.isdigit()
                valid = (
                    valid_code
                    and isinstance(expected_binding, bytes)
                    and len(expected_binding) == 32
                    and isinstance(expected_key_sha256, str)
                    and len(expected_key_sha256) == 64
                    and expected_key_sha256.isascii()
                    and hmac.compare_digest(self._code_digest(request_id, code), row["code_digest"])
                    and hmac.compare_digest(expected_binding, row["binding_digest"])
                    and hmac.compare_digest(expected_key_sha256, hashlib.sha256(row["public_key"]).hexdigest())
                )
                if valid:
                    conn.execute(
                        "UPDATE device_gateway.pairing_requests SET state='APPROVED', code_digest=NULL WHERE id=%s",
                        (request_id,),
                    )
                    accepted = True
                else:
                    self._fail(conn, request_id)
        if not accepted:
            raise PairingRejected()

    def inspect(self, scope: AuthenticatedScope, request_id: uuid.UUID) -> PendingPairing:
        """Owner-only confirmation data, excluding bootstrap token and code."""
        with self._connect() as conn, conn.transaction():
            self._scope(conn, scope.tenant_id, scope.user_id)
            row = conn.execute(
                "SELECT * FROM device_gateway.pairing_requests WHERE id=%s AND tenant_id=%s AND owner_id=%s",
                (request_id, scope.tenant_id, scope.user_id),
            ).fetchone()
            now = self._now(conn)
            if row is None or row["state"] != "WAITING_APPROVAL" or not row["issued_ms"] <= now < row["expires_ms"]:
                raise PairingRejected()
            return PendingPairing(
                row["id"],
                row["identity_id"],
                row["device_id"],
                row["origin"],
                hashlib.sha256(row["public_key"]).hexdigest(),
                row["binding_digest"],
                row["expires_ms"],
            )

    def status(self, scope: AuthenticatedScope, request_id: uuid.UUID) -> PairingStatus:
        """Reconcile an uncertain response using independently authenticated owner scope.

        An expired pending request is not reported as approved. A consumed result is
        historical evidence, not a live session or authorization to operate a device.
        """
        with self._connect() as conn, conn.transaction():
            self._scope(conn, scope.tenant_id, scope.user_id)
            row = conn.execute(
                "SELECT * FROM device_gateway.pairing_requests WHERE id=%s AND tenant_id=%s AND owner_id=%s",
                (request_id, scope.tenant_id, scope.user_id),
            ).fetchone()
            now = self._now(conn)
            if row is None or now < row["issued_ms"]:
                raise PairingRejected()
            state = row["state"]
            if state != "CONSUMED" and now >= row["expires_ms"]:
                state = "EXPIRED"
            public_key = row["public_key"]
            return PairingStatus(
                state,
                row["identity_id"],
                row["device_id"],
                hashlib.sha256(public_key).hexdigest() if public_key is not None else None,
                row["binding_digest"],
            )

    def consume(
        self, tenant_hint: str, owner_hint: str, request_id: uuid.UUID, token: bytes
    ) -> tuple[uuid.UUID, uuid.UUID]:
        result = None
        with self._connect() as conn, conn.transaction():
            self._scope(conn, tenant_hint, owner_hint)
            row = conn.execute(
                "SELECT * FROM device_gateway.pairing_requests WHERE id=%s AND tenant_id=%s AND owner_id=%s FOR UPDATE",
                (request_id, tenant_hint, owner_hint),
            ).fetchone()
            now = self._now(conn)
            if row is not None and row["state"] == "APPROVED" and row["issued_ms"] <= now < row["expires_ms"]:
                if (
                    isinstance(token, bytes)
                    and len(token) == 32
                    and hmac.compare_digest(hashlib.sha256(token).digest(), row["token_digest"])
                ):
                    conn.execute(
                        "INSERT INTO device_gateway.device_identities "
                        "(id, tenant_id, owner_id, device_id, origin, public_key, binding_digest) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (
                            row["identity_id"],
                            row["tenant_id"],
                            row["owner_id"],
                            row["device_id"],
                            row["origin"],
                            row["public_key"],
                            row["binding_digest"],
                        ),
                    )
                    conn.execute(
                        "UPDATE device_gateway.pairing_requests SET state='CONSUMED' WHERE id=%s", (request_id,)
                    )
                    result = (row["identity_id"], row["device_id"])
                else:
                    self._fail(conn, request_id)
        if result is None:
            raise PairingRejected()
        return result
