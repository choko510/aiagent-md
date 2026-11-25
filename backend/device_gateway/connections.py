"""Durable device connection challenges; not a WSS session or Tool authority.

This isolated store requires a trusted, independently managed restore generation.
It is not mounted in an API and does not enforce channel binding yet.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass, field

from nacl.bindings import crypto_core_ed25519_is_valid_point
from nacl.exceptions import BadSignatureError, CryptoError
from nacl.signing import VerifyKey

from .pairing import AuthenticatedScope, PairingService

DOMAIN = b"aiagent-device-connect-proof-candidate-v1"
CHALLENGE_TTL_MS = 60_000
SESSION_TTL_MS = 120_000
INT64_MAX = 2**63 - 1


class ConnectionRejected(Exception):
    """Do not reveal whether the account, device, token or signature existed."""


@dataclass(frozen=True, slots=True)
class Challenge:
    nonce: bytes = field(repr=False)
    generation: int
    issued_ms: int
    expires_ms: int


@dataclass(frozen=True, slots=True)
class Session:
    token: bytes = field(repr=False)
    generation: int
    expires_ms: int


def connection_proof_bytes(
    binding_digest: bytes, public_key: bytes, nonce: bytes, generation: int, issued_ms: int, expires_ms: int
) -> bytes:
    """Candidate Rust-compatible input; not the canonical wire schema."""
    if (
        not all(isinstance(v, bytes) and len(v) == 32 for v in (binding_digest, public_key, nonce))
        or nonce == bytes(32)
        or any(type(v) is not int for v in (generation, issued_ms, expires_ms))
        or not 0 < generation <= INT64_MAX
        or not 0 <= issued_ms < expires_ms <= INT64_MAX
        or expires_ms - issued_ms > CHALLENGE_TTL_MS
    ):
        raise ConnectionRejected()
    return (
        DOMAIN
        + binding_digest
        + public_key
        + nonce
        + generation.to_bytes(8, "big")
        + issued_ms.to_bytes(8, "big")
        + expires_ms.to_bytes(8, "big")
    )


class ConnectionService:
    def __init__(self, pairing: PairingService, restore_incarnation: int) -> None:
        if type(restore_incarnation) is not int or not 0 <= restore_incarnation <= INT64_MAX:
            raise ValueError("invalid restore incarnation")
        self._pairing = pairing
        self._restore = restore_incarnation

    def challenge(self, tenant_hint: str, owner_hint: str, identity_id: uuid.UUID) -> Challenge:
        result = None
        with self._pairing._connect() as conn, conn.transaction():
            self._pairing._scope(conn, tenant_hint, owner_hint)
            identity = conn.execute(
                "SELECT * FROM device_gateway.device_identities WHERE id=%s AND tenant_id=%s AND owner_id=%s FOR UPDATE",
                (identity_id, tenant_hint, owner_hint),
            ).fetchone()
            now = self._pairing._now(conn)
            if (
                identity is not None
                and not identity["revoked"]
                and identity["connection_generation"] < INT64_MAX
                and identity["authority_incarnation"] <= self._restore
            ):
                if identity["authority_incarnation"] < self._restore:
                    conn.execute(
                        "UPDATE device_gateway.device_identities SET authority_incarnation=%s WHERE id=%s",
                        (self._restore, identity_id),
                    )
                    conn.execute(
                        "UPDATE device_gateway.connection_sessions SET valid=false WHERE identity_id=%s", (identity_id,)
                    )
                    conn.execute(
                        "UPDATE device_gateway.connection_challenges SET used=true WHERE identity_id=%s", (identity_id,)
                    )
                next_generation = identity["connection_generation"] + 1
                pending = conn.execute(
                    "SELECT * FROM device_gateway.connection_challenges WHERE identity_id=%s FOR UPDATE", (identity_id,)
                ).fetchone()
                if pending is not None and pending["issued_ms"] > now:
                    # Do not resurrect a challenge after a wall-clock rollback.
                    result = None
                elif (
                    pending is not None
                    and not pending["used"]
                    and pending["expires_ms"] > now
                    and pending["generation"] == next_generation
                    and pending["restore_incarnation"] == self._restore
                    and pending["revocation_generation"] == identity["revocation_generation"]
                ):
                    result = Challenge(
                        pending["nonce"], pending["generation"], pending["issued_ms"], pending["expires_ms"]
                    )
                else:
                    nonce = secrets.token_bytes(32)
                    conn.execute(
                        "INSERT INTO device_gateway.connection_challenges "
                        "(tenant_id, owner_id, identity_id, nonce, generation, issued_ms, expires_ms, "
                        "restore_incarnation, revocation_generation, used) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,false) "
                        "ON CONFLICT(identity_id) DO UPDATE SET nonce=excluded.nonce, generation=excluded.generation, "
                        "issued_ms=excluded.issued_ms, expires_ms=excluded.expires_ms, "
                        "restore_incarnation=excluded.restore_incarnation, "
                        "revocation_generation=excluded.revocation_generation, used=false",
                        (
                            tenant_hint,
                            owner_hint,
                            identity_id,
                            nonce,
                            next_generation,
                            now,
                            now + CHALLENGE_TTL_MS,
                            self._restore,
                            identity["revocation_generation"],
                        ),
                    )
                    result = Challenge(nonce, next_generation, now, now + CHALLENGE_TTL_MS)
        if result is None:
            raise ConnectionRejected()
        return result

    def authenticate(self, tenant_hint: str, owner_hint: str, identity_id: uuid.UUID, signature: bytes) -> Session:
        result = None
        with self._pairing._connect() as conn, conn.transaction():
            self._pairing._scope(conn, tenant_hint, owner_hint)
            identity = conn.execute(
                "SELECT * FROM device_gateway.device_identities WHERE id=%s AND tenant_id=%s AND owner_id=%s FOR UPDATE",
                (identity_id, tenant_hint, owner_hint),
            ).fetchone()
            challenge = conn.execute(
                "SELECT * FROM device_gateway.connection_challenges WHERE identity_id=%s FOR UPDATE", (identity_id,)
            ).fetchone()
            now = self._pairing._now(conn)
            if (
                identity is not None
                and challenge is not None
                and not identity["revoked"]
                and identity["authority_incarnation"] == self._restore
                and not challenge["used"]
                and challenge["issued_ms"] <= now
                and challenge["expires_ms"] > now
                and challenge["generation"] == identity["connection_generation"] + 1
                and challenge["restore_incarnation"] == self._restore
                and challenge["revocation_generation"] == identity["revocation_generation"]
            ):
                try:
                    if not isinstance(signature, bytes) or len(signature) != 64:
                        raise ConnectionRejected()
                    public = identity["public_key"]
                    if not crypto_core_ed25519_is_valid_point(public):
                        raise ConnectionRejected()
                    message = connection_proof_bytes(
                        identity["binding_digest"],
                        public,
                        challenge["nonce"],
                        challenge["generation"],
                        challenge["issued_ms"],
                        challenge["expires_ms"],
                    )
                    VerifyKey(public).verify(message, signature)
                except (ValueError, BadSignatureError, CryptoError, ConnectionRejected):
                    pass
                else:
                    token = secrets.token_bytes(32)
                    conn.execute(
                        "UPDATE device_gateway.connection_challenges SET used=true WHERE identity_id=%s", (identity_id,)
                    )
                    conn.execute(
                        "UPDATE device_gateway.device_identities SET connection_generation=%s WHERE id=%s",
                        (challenge["generation"], identity_id),
                    )
                    conn.execute(
                        "INSERT INTO device_gateway.connection_sessions "
                        "(tenant_id, owner_id, identity_id, generation, token_digest, expires_ms, last_heartbeat_ms, "
                        "restore_incarnation, revocation_generation, valid) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,true) "
                        "ON CONFLICT(identity_id) DO UPDATE SET generation=excluded.generation, "
                        "token_digest=excluded.token_digest, expires_ms=excluded.expires_ms, "
                        "last_heartbeat_ms=excluded.last_heartbeat_ms, restore_incarnation=excluded.restore_incarnation, "
                        "revocation_generation=excluded.revocation_generation, valid=true",
                        (
                            tenant_hint,
                            owner_hint,
                            identity_id,
                            challenge["generation"],
                            hashlib.sha256(token).digest(),
                            now + SESSION_TTL_MS,
                            now,
                            self._restore,
                            identity["revocation_generation"],
                        ),
                    )
                    result = Session(token, challenge["generation"], now + SESSION_TTL_MS)
        if result is None:
            raise ConnectionRejected()
        return result

    def heartbeat(
        self, tenant_hint: str, owner_hint: str, identity_id: uuid.UUID, generation: int, token: bytes
    ) -> None:
        accepted = False
        with self._pairing._connect() as conn, conn.transaction():
            self._pairing._scope(conn, tenant_hint, owner_hint)
            row = conn.execute(
                "SELECT i.revoked, i.revocation_generation AS current_revocation, i.connection_generation, i.authority_incarnation, "
                "s.* FROM device_gateway.device_identities i JOIN device_gateway.connection_sessions s "
                "ON (i.tenant_id,i.owner_id,i.id)=(s.tenant_id,s.owner_id,s.identity_id) "
                "WHERE i.id=%s AND i.tenant_id=%s AND i.owner_id=%s FOR UPDATE OF i,s",
                (identity_id, tenant_hint, owner_hint),
            ).fetchone()
            now = self._pairing._now(conn)
            if (
                row is not None
                and type(generation) is int
                and 0 < generation <= INT64_MAX
                and isinstance(token, bytes)
                and len(token) == 32
                and row["valid"]
                and not row["revoked"]
                and row["authority_incarnation"] == self._restore
                and row["last_heartbeat_ms"] <= now
                and row["expires_ms"] > now
                and row["generation"] == generation == row["connection_generation"]
                and row["restore_incarnation"] == self._restore
                and row["revocation_generation"] == row["current_revocation"]
                and hmac.compare_digest(hashlib.sha256(token).digest(), row["token_digest"])
            ):
                conn.execute(
                    "UPDATE device_gateway.connection_sessions SET last_heartbeat_ms=%s WHERE identity_id=%s",
                    (now, identity_id),
                )
                accepted = True
        if not accepted:
            raise ConnectionRejected()

    def revoke(self, scope: AuthenticatedScope, identity_id: uuid.UUID) -> None:
        accepted = False
        with self._pairing._connect() as conn, conn.transaction():
            self._pairing._scope(conn, scope.tenant_id, scope.user_id)
            row = conn.execute(
                "SELECT revoked, revocation_generation FROM device_gateway.device_identities "
                "WHERE id=%s AND tenant_id=%s AND owner_id=%s FOR UPDATE",
                (identity_id, scope.tenant_id, scope.user_id),
            ).fetchone()
            if row is not None:
                if not row["revoked"]:
                    if row["revocation_generation"] == INT64_MAX:
                        raise ConnectionRejected()
                    conn.execute(
                        "UPDATE device_gateway.device_identities SET revoked=true, "
                        "revocation_generation=revocation_generation+1 WHERE id=%s",
                        (identity_id,),
                    )
                    conn.execute(
                        "UPDATE device_gateway.connection_sessions SET valid=false WHERE identity_id=%s", (identity_id,)
                    )
                    conn.execute(
                        "UPDATE device_gateway.connection_challenges SET used=true WHERE identity_id=%s", (identity_id,)
                    )
                accepted = True
        if not accepted:
            raise ConnectionRejected()
