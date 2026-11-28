"""Real PostgreSQL fixture tests; no production migration or HTTP route is installed."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import os
import secrets
import ssl
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from backend.device_gateway.connections import (
    ConnectionChannel,
    ConnectionRejected,
    ConnectionService,
    connection_proof_bytes,
)
from backend.device_gateway.pairing import PairingRejected, PairingService, pairing_proof_bytes
from backend.device_gateway.wss import DeviceWss, InvalidFrame, decode_frame
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.x509.oid import NameOID
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed


@dataclass(frozen=True)
class Scope:
    tenant_id: str
    user_id: str


def test_rust_and_python_share_pairing_proof_vector() -> None:
    vector = json.loads(
        (Path(__file__).resolve().parents[3] / "device-client/fixtures/pairing-proof-v1.json").read_text()
    )
    public = bytes.fromhex(vector["publicKeyHex"])
    payload = pairing_proof_bytes(
        uuid.UUID(hex=vector["pairingIdHex"]),
        bytes.fromhex(vector["bindingDigestHex"]),
        public,
        bytes.fromhex(vector["nonceHex"]),
        int(vector["issuedMs"]),
        int(vector["expiresMs"]),
    )
    Ed25519PublicKey.from_public_bytes(public).verify(bytes.fromhex(vector["signatureHex"]), payload)


def test_rust_and_python_share_signed_authority_vector() -> None:
    from nacl.signing import SigningKey

    vector = json.loads((Path(__file__).resolve().parents[3] / "device-client/fixtures/authority-v1.json").read_text())
    key = SigningKey(bytes.fromhex(vector["testSeedHex"]))
    assert bytes(key.verify_key).hex() == vector["publicKeyHex"]
    for name in ("grant", "permit"):
        raw = vector[f"{name}Raw"].encode("utf-8")
        assert json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() == raw
        assert key.sign(b"aiagent-device-authority-v1\0" + raw).signature.hex() == vector[f"{name}SignatureHex"]


def test_unpatched_ed25519_backend_cannot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backend.device_gateway.pairing.crypto_core_ed25519_is_valid_point", lambda _: True)
    with pytest.raises(RuntimeError, match="UNSAFE_ED25519_BACKEND"):
        PairingService("unused", "https://gateway.example", bytes(range(32)))


def test_rust_and_python_share_connection_proof_vector() -> None:
    vector = json.loads(
        (Path(__file__).resolve().parents[3] / "device-client/fixtures/connection-proof-v2.json").read_text()
    )
    public = bytes.fromhex(vector["publicKeyHex"])
    payload = connection_proof_bytes(
        bytes.fromhex(vector["bindingDigestHex"]),
        public,
        bytes.fromhex(vector["nonceHex"]),
        bytes.fromhex(vector["channelDigestHex"]),
        int(vector["generation"]),
        int(vector["issuedMs"]),
        int(vector["expiresMs"]),
    )
    Ed25519PublicKey.from_public_bytes(public).verify(bytes.fromhex(vector["signatureHex"]), payload)


def test_legacy_connection_vector_remains_verifiable_but_is_not_v2() -> None:
    vector = json.loads(
        (Path(__file__).resolve().parents[3] / "device-client/fixtures/connection-proof-v1.json").read_text()
    )
    public = bytes.fromhex(vector["publicKeyHex"])
    payload = (
        b"aiagent-device-connect-proof-candidate-v1"
        + bytes.fromhex(vector["bindingDigestHex"])
        + public
        + bytes.fromhex(vector["nonceHex"])
        + int(vector["generation"]).to_bytes(8, "big")
        + int(vector["issuedMs"]).to_bytes(8, "big")
        + int(vector["expiresMs"]).to_bytes(8, "big")
    )
    Ed25519PublicKey.from_public_bytes(public).verify(bytes.fromhex(vector["signatureHex"]), payload)


@pytest.fixture(scope="session")
def service() -> Iterator[PairingService]:
    admin_dsn = os.environ.get("AIAGENT_DEVICE_TEST_ADMIN_DSN")
    if not admin_dsn:
        pytest.skip("isolated PostgreSQL test database is not configured")
    params = conninfo_to_dict(admin_dsn)
    if params.get("host") not in ("127.0.0.1", "localhost", "::1") or params.get("dbname") != "device_gateway_test":
        raise RuntimeError("tests require an explicitly configured loopback-only device_gateway_test database")
    role_password = secrets.token_urlsafe(32)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        if conn.execute("SELECT to_regnamespace('device_gateway')").fetchone()[0] is not None:
            raise RuntimeError("test schema already exists; use a fresh disposable database")
        conn.execute(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
        conn.execute(
            sql.SQL("CREATE ROLE ldc_pairing_test LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
                sql.Literal(role_password)
            )
        )
        conn.execute("GRANT USAGE ON SCHEMA device_gateway TO ldc_pairing_test")
        conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA device_gateway TO ldc_pairing_test")
    app_dsn = make_conninfo(admin_dsn, user="ldc_pairing_test", password=role_password)
    try:
        yield PairingService(app_dsn, "https://gateway.example", bytes(range(32)))
    finally:
        # The schema and role were created by this fixture in its dedicated database.
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA device_gateway CASCADE")
            conn.execute("DROP ROLE ldc_pairing_test")


def make_scope() -> Scope:
    return Scope(str(uuid.uuid4()), str(uuid.uuid4()))


def test_pairing_lock_wait_is_bounded(service: PairingService) -> None:
    owner = make_scope()
    ticket = service.begin(owner)
    with service._connect() as conn, conn.transaction():
        service._scope(conn, owner.tenant_id, owner.user_id)
        conn.execute("SELECT id FROM device_gateway.pairing_requests WHERE id=%s FOR UPDATE", (ticket.pairing_id,))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(
                service.attach,
                owner.tenant_id,
                owner.user_id,
                ticket.pairing_id,
                bytes(32),
                bytes(32),
                bytes(32),
                bytes(64),
            )
            with pytest.raises(psycopg.errors.LockNotAvailable):
                task.result(timeout=8)


def attach(service: PairingService, scope: Scope, bootstrap: object):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    digest = bytes(range(32))
    proof = pairing_proof_bytes(
        bootstrap.pairing_id, digest, public, bootstrap.nonce, bootstrap.issued_ms, bootstrap.expires_ms
    )
    code = service.attach(
        scope.tenant_id, scope.user_id, bootstrap.pairing_id, bootstrap.token, public, digest, key.sign(proof)
    )
    return code, key, public, digest, proof


def test_pairing_binds_owner_proof_and_single_consumption(service: PairingService) -> None:
    owner = make_scope()
    ticket = service.begin(owner)
    assert service.status(owner, ticket.pairing_id).state == "WAITING_DEVICE"
    assert (ticket.tenant_id, ticket.owner_user_id, ticket.revocation_generation) == (owner.tenant_id, owner.user_id, 0)
    assert ticket.token.hex() not in repr(ticket)
    with pytest.raises(PairingRejected):
        service.attach(owner.tenant_id, owner.user_id, ticket.pairing_id, b"x" * 32, b"x" * 32, b"x" * 32, b"x" * 64)
    wrong_key = Ed25519PrivateKey.generate()
    public = wrong_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    with pytest.raises(PairingRejected):
        service.attach(owner.tenant_id, owner.user_id, ticket.pairing_id, ticket.token, public, bytes(32), bytes(64))
    code, key, public, digest, proof = attach(service, owner, ticket)
    assert service.status(owner, ticket.pairing_id).state == "WAITING_APPROVAL"
    assert code.code not in repr(code)
    pending = service.inspect(owner, ticket.pairing_id)
    assert pending.public_key_sha256 == code.public_key_sha256
    assert pending.binding_digest == digest
    assert pending.identity_id == ticket.identity_id
    with pytest.raises(PairingRejected):
        service.attach(owner.tenant_id, owner.user_id, ticket.pairing_id, ticket.token, public, digest, key.sign(proof))
    for other in (Scope(owner.tenant_id, str(uuid.uuid4())), make_scope()):
        with pytest.raises(PairingRejected):
            service.status(other, ticket.pairing_id)
        with pytest.raises(PairingRejected):
            service.inspect(other, ticket.pairing_id)
        with pytest.raises(PairingRejected):
            service.approve(other, ticket.pairing_id, code.code, code.public_key_sha256, digest)
    with pytest.raises(PairingRejected):
        service.approve(owner, ticket.pairing_id, code.code, code.public_key_sha256, b"z" * 32)
    service.approve(owner, ticket.pairing_id, code.code, code.public_key_sha256, digest)
    assert service.status(owner, ticket.pairing_id).state == "APPROVED"
    with pytest.raises(PairingRejected):
        service.approve(owner, ticket.pairing_id, code.code, code.public_key_sha256, digest)

    def attempt() -> bool:
        try:
            return service.consume(owner.tenant_id, owner.user_id, ticket.pairing_id, ticket.token) == (
                ticket.identity_id,
                ticket.device_id,
            )
        except PairingRejected:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [False, True]

    status = service.status(owner, ticket.pairing_id)
    assert (status.state, status.identity_id, status.device_id) == ("CONSUMED", ticket.identity_id, ticket.device_id)
    assert (status.public_key_sha256, status.binding_digest) == (hashlib.sha256(public).hexdigest(), digest)
    assert ticket.token.hex() not in repr(status) and code.code not in repr(status)

    with service._connect() as conn, conn.transaction():
        role = conn.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
        assert role == {"rolsuper": False, "rolbypassrls": False}
        service._scope(conn, owner.tenant_id, owner.user_id)
        row = conn.execute(
            "SELECT owner_id, public_key FROM device_gateway.device_identities WHERE id=%s", (ticket.identity_id,)
        ).fetchone()
        assert row == {"owner_id": owner.user_id, "public_key": public}
        service._scope(conn, owner.tenant_id, str(uuid.uuid4()))
        assert (
            conn.execute(
                "SELECT id FROM device_gateway.device_identities WHERE id=%s", (ticket.identity_id,)
            ).fetchone()
            is None
        )
        service._scope(conn, str(uuid.uuid4()), owner.user_id)
        assert (
            conn.execute(
                "SELECT id FROM device_gateway.device_identities WHERE id=%s", (ticket.identity_id,)
            ).fetchone()
            is None
        )


def test_invalid_signatures_and_codes_lock_out(service: PairingService) -> None:
    owner = make_scope()
    ticket = service.begin(owner)
    for _ in range(5):
        with pytest.raises(PairingRejected):
            service.attach(
                owner.tenant_id, owner.user_id, ticket.pairing_id, ticket.token, bytes(32), bytes(32), bytes(64)
            )
    with pytest.raises(PairingRejected):
        attach(service, owner, ticket)

    ticket = service.begin(owner)
    code, _, _, digest, _ = attach(service, owner, ticket)
    wrong = "00000000" if code.code != "00000000" else "00000001"
    for _ in range(5):
        with pytest.raises(PairingRejected):
            service.approve(owner, ticket.pairing_id, wrong, code.public_key_sha256, digest)
    with pytest.raises(PairingRejected):
        service.approve(owner, ticket.pairing_id, code.code, code.public_key_sha256, digest)


def test_expiry_and_cross_tenant_requests_are_rejected(service: PairingService) -> None:
    owner = make_scope()
    ticket = service.begin(owner)
    with pytest.raises(PairingRejected):
        service.attach(
            str(uuid.uuid4()), owner.user_id, ticket.pairing_id, ticket.token, bytes(32), bytes(32), bytes(64)
        )
    with pytest.raises(PairingRejected):
        service.attach(
            owner.tenant_id, str(uuid.uuid4()), ticket.pairing_id, ticket.token, bytes(32), bytes(32), bytes(64)
        )
    with service._connect() as conn, conn.transaction():
        service._scope(conn, owner.tenant_id, owner.user_id)
        conn.execute(
            "UPDATE device_gateway.pairing_requests SET expires_ms=issued_ms WHERE id=%s", (ticket.pairing_id,)
        )
    assert service.status(owner, ticket.pairing_id).state == "EXPIRED"
    with pytest.raises(PairingRejected):
        attach(service, owner, ticket)
    ticket = service.begin(owner)
    with service._connect() as conn, conn.transaction():
        service._scope(conn, owner.tenant_id, owner.user_id)
        conn.execute(
            "UPDATE device_gateway.pairing_requests SET issued_ms=issued_ms+600000, "
            "expires_ms=expires_ms+600000 WHERE id=%s",
            (ticket.pairing_id,),
        )
    with pytest.raises(PairingRejected):
        attach(service, owner, ticket)


def test_restart_and_failed_registration_transaction_preserve_approval(service: PairingService) -> None:
    owner = make_scope()
    ticket = service.begin(owner)
    code, _, _, digest, _ = attach(service, owner, ticket)
    restarted = PairingService(service._dsn, "https://gateway.example", bytes(range(32)))
    restarted.approve(owner, ticket.pairing_id, code.code, code.public_key_sha256, digest)
    admin_dsn = os.environ["AIAGENT_DEVICE_TEST_ADMIN_DSN"]
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE FUNCTION device_gateway.reject_identity() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'injected registration failure'; END $$"
        )
        conn.execute(
            "CREATE TRIGGER reject_identity BEFORE INSERT ON device_gateway.device_identities "
            "FOR EACH ROW EXECUTE FUNCTION device_gateway.reject_identity()"
        )
    try:
        with pytest.raises(psycopg.Error):
            restarted.consume(owner.tenant_id, owner.user_id, ticket.pairing_id, ticket.token)
        with restarted._connect() as conn, conn.transaction():
            restarted._scope(conn, owner.tenant_id, owner.user_id)
            row = conn.execute(
                "SELECT state FROM device_gateway.pairing_requests WHERE id=%s", (ticket.pairing_id,)
            ).fetchone()
            assert row["state"] == "APPROVED"
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute("DROP TRIGGER reject_identity ON device_gateway.device_identities")
            conn.execute("DROP FUNCTION device_gateway.reject_identity()")
    assert restarted.consume(owner.tenant_id, owner.user_id, ticket.pairing_id, ticket.token) == (
        ticket.identity_id,
        ticket.device_id,
    )


def test_expiry_after_approval_blocks_registration(service: PairingService) -> None:
    owner = make_scope()
    ticket = service.begin(owner)
    code, _, _, digest, _ = attach(service, owner, ticket)
    service.approve(owner, ticket.pairing_id, code.code, code.public_key_sha256, digest)
    with service._connect() as conn, conn.transaction():
        service._scope(conn, owner.tenant_id, owner.user_id)
        conn.execute(
            "UPDATE device_gateway.pairing_requests SET expires_ms=issued_ms WHERE id=%s", (ticket.pairing_id,)
        )
    with pytest.raises(PairingRejected):
        service.consume(owner.tenant_id, owner.user_id, ticket.pairing_id, ticket.token)


def test_concurrent_code_approvals_have_one_winner(service: PairingService) -> None:
    owner = make_scope()
    ticket = service.begin(owner)
    code, _, _, digest, _ = attach(service, owner, ticket)

    def attempt() -> bool:
        try:
            service.approve(owner, ticket.pairing_id, code.code, code.public_key_sha256, digest)
            return True
        except PairingRejected:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [False, True]
    assert service.consume(owner.tenant_id, owner.user_id, ticket.pairing_id, ticket.token) == (
        ticket.identity_id,
        ticket.device_id,
    )


def registered_device(service: PairingService, owner: Scope):
    ticket = service.begin(owner)
    code, key, public, digest, _ = attach(service, owner, ticket)
    service.approve(owner, ticket.pairing_id, code.code, code.public_key_sha256, digest)
    service.consume(owner.tenant_id, owner.user_id, ticket.pairing_id, ticket.token)
    return ticket.identity_id, key, public, digest


def test_connection_challenge_single_use_generation_and_revocation(service: PairingService) -> None:
    owner = make_scope()
    identity_id, key, public, digest = registered_device(service, owner)
    gateway = ConnectionService(service, 7)
    channel = ConnectionChannel()
    challenge = gateway.challenge(owner.tenant_id, owner.user_id, identity_id, channel)
    assert challenge == gateway.challenge(owner.tenant_id, owner.user_id, identity_id, channel)
    message = connection_proof_bytes(
        digest,
        public,
        challenge.nonce,
        challenge.channel_digest,
        challenge.generation,
        challenge.issued_ms,
        challenge.expires_ms,
    )
    with pytest.raises(ConnectionRejected):
        gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, channel, bytes(64))
    legacy = (
        b"aiagent-device-connect-proof-candidate-v1"
        + digest
        + public
        + challenge.nonce
        + challenge.generation.to_bytes(8, "big")
        + challenge.issued_ms.to_bytes(8, "big")
        + challenge.expires_ms.to_bytes(8, "big")
    )
    with pytest.raises(ConnectionRejected):
        gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, channel, key.sign(legacy))
    signature = key.sign(message)
    with pytest.raises(ConnectionRejected):
        gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, ConnectionChannel(), signature)

    def attempt():
        try:
            return gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, channel, signature)
        except ConnectionRejected:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(lambda _: attempt(), range(2)))
    sessions = [session for session in attempts if session is not None]
    assert len(sessions) == 1
    session = sessions[0]
    assert session.token.hex() not in repr(session)
    gateway._heartbeat_unchecked_for_tests(
        owner.tenant_id, owner.user_id, identity_id, channel, session.generation, session.token
    )
    with service._connect() as conn, conn.transaction():
        service._scope(conn, owner.tenant_id, owner.user_id)
        row = conn.execute(
            "SELECT token_digest FROM device_gateway.connection_sessions WHERE identity_id=%s", (identity_id,)
        ).fetchone()
        assert row["token_digest"] == hashlib.sha256(session.token).digest()
        assert row["token_digest"] != session.token
        service._scope(conn, owner.tenant_id, str(uuid.uuid4()))
        assert (
            conn.execute(
                "SELECT identity_id FROM device_gateway.connection_sessions WHERE identity_id=%s", (identity_id,)
            ).fetchone()
            is None
        )
    for tenant, user, generation, token in [
        (owner.tenant_id, owner.user_id, session.generation + 1, session.token),
        (owner.tenant_id, owner.user_id, session.generation, bytes(32)),
        (owner.tenant_id, str(uuid.uuid4()), session.generation, session.token),
        (str(uuid.uuid4()), owner.user_id, session.generation, session.token),
    ]:
        with pytest.raises(ConnectionRejected):
            gateway._heartbeat_unchecked_for_tests(tenant, user, identity_id, channel, generation, token)
    other_channel = ConnectionChannel()
    with pytest.raises(ConnectionRejected):
        gateway._heartbeat_unchecked_for_tests(
            owner.tenant_id, owner.user_id, identity_id, other_channel, session.generation, session.token
        )
    reconnect = gateway.challenge(owner.tenant_id, owner.user_id, identity_id, other_channel)
    assert reconnect.generation == session.generation + 1
    second_sig = key.sign(
        connection_proof_bytes(
            digest,
            public,
            reconnect.nonce,
            reconnect.channel_digest,
            reconnect.generation,
            reconnect.issued_ms,
            reconnect.expires_ms,
        )
    )
    with pytest.raises(ConnectionRejected):
        gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, channel, second_sig)
    second = gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, other_channel, second_sig)
    with pytest.raises(ConnectionRejected):
        gateway._heartbeat_unchecked_for_tests(
            owner.tenant_id, owner.user_id, identity_id, channel, session.generation, session.token
        )
    gateway.disconnect(owner.tenant_id, owner.user_id, identity_id, channel)
    gateway._heartbeat_unchecked_for_tests(
        owner.tenant_id, owner.user_id, identity_id, other_channel, second.generation, second.token
    )
    with pytest.raises(ConnectionRejected):
        gateway.revoke(Scope(owner.tenant_id, str(uuid.uuid4())), identity_id)
    gateway.revoke(owner, identity_id)
    gateway.revoke(owner, identity_id)
    with service._connect() as conn, conn.transaction():
        service._scope(conn, owner.tenant_id, owner.user_id)
        row = conn.execute(
            "SELECT revoked, revocation_generation FROM device_gateway.device_identities WHERE id=%s", (identity_id,)
        ).fetchone()
        assert row == {"revoked": True, "revocation_generation": 1}
    with pytest.raises(ConnectionRejected):
        gateway._heartbeat_unchecked_for_tests(
            owner.tenant_id, owner.user_id, identity_id, other_channel, second.generation, second.token
        )
    with pytest.raises(ConnectionRejected):
        gateway.challenge(owner.tenant_id, owner.user_id, identity_id, channel)


def test_new_generation_expires_and_rejects_old_restore_incarnation(service: PairingService) -> None:
    owner = make_scope()
    identity_id, key, public, digest = registered_device(service, owner)
    gateway = ConnectionService(service, 1)
    channel = ConnectionChannel()
    first = gateway.challenge(owner.tenant_id, owner.user_id, identity_id, channel)
    signature = key.sign(
        connection_proof_bytes(
            digest, public, first.nonce, first.channel_digest, first.generation, first.issued_ms, first.expires_ms
        )
    )
    session = gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, channel, signature)
    restarted = ConnectionService(service, 2)
    with pytest.raises(ConnectionRejected):
        restarted._heartbeat_unchecked_for_tests(
            owner.tenant_id, owner.user_id, identity_id, channel, session.generation, session.token
        )
    next_challenge = restarted.challenge(owner.tenant_id, owner.user_id, identity_id, channel)
    assert next_challenge.generation == session.generation + 1
    # A new unauthenticated challenge cannot fence the prior process. Until an
    # independent restore barrier exists, the old session remains a known gap.
    gateway._heartbeat_unchecked_for_tests(
        owner.tenant_id, owner.user_id, identity_id, channel, session.generation, session.token
    )
    with pytest.raises(ConnectionRejected):
        restarted.authenticate(owner.tenant_id, owner.user_id, identity_id, channel, signature)
    next_signature = key.sign(
        connection_proof_bytes(
            digest,
            public,
            next_challenge.nonce,
            next_challenge.channel_digest,
            next_challenge.generation,
            next_challenge.issued_ms,
            next_challenge.expires_ms,
        )
    )
    next_session = restarted.authenticate(owner.tenant_id, owner.user_id, identity_id, channel, next_signature)
    with pytest.raises(ConnectionRejected):
        gateway.challenge(owner.tenant_id, owner.user_id, identity_id, channel)
    with pytest.raises(ConnectionRejected):
        restarted._heartbeat_unchecked_for_tests(
            owner.tenant_id, owner.user_id, identity_id, channel, session.generation, session.token
        )
    restarted._heartbeat_unchecked_for_tests(
        owner.tenant_id, owner.user_id, identity_id, channel, next_session.generation, next_session.token
    )
    with service._connect() as conn, conn.transaction():
        service._scope(conn, owner.tenant_id, owner.user_id)
        conn.execute("UPDATE device_gateway.connection_sessions SET expires_ms=0 WHERE identity_id=%s", (identity_id,))
    with pytest.raises(ConnectionRejected):
        restarted._heartbeat_unchecked_for_tests(
            owner.tenant_id, owner.user_id, identity_id, channel, next_session.generation, next_session.token
        )


def test_revocation_races_authentication_without_live_session(service: PairingService) -> None:
    owner = make_scope()
    identity_id, key, public, digest = registered_device(service, owner)
    gateway = ConnectionService(service, 1)
    channel = ConnectionChannel()
    challenge = gateway.challenge(owner.tenant_id, owner.user_id, identity_id, channel)
    signature = key.sign(
        connection_proof_bytes(
            digest,
            public,
            challenge.nonce,
            challenge.channel_digest,
            challenge.generation,
            challenge.issued_ms,
            challenge.expires_ms,
        )
    )

    def authenticate():
        try:
            return gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, channel, signature)
        except ConnectionRejected:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        auth_future = pool.submit(authenticate)
        revoke_future = pool.submit(gateway.revoke, owner, identity_id)
        session = auth_future.result()
        revoke_future.result()
    if session is not None:
        with pytest.raises(ConnectionRejected):
            gateway._heartbeat_unchecked_for_tests(
                owner.tenant_id, owner.user_id, identity_id, channel, session.generation, session.token
            )
    with pytest.raises(ConnectionRejected):
        gateway.challenge(owner.tenant_id, owner.user_id, identity_id, channel)


def test_connection_rejects_expired_or_future_challenges_and_rolled_back_clock(service: PairingService) -> None:
    owner = make_scope()
    identity_id, key, public, digest = registered_device(service, owner)
    gateway = ConnectionService(service, 1)
    channel = ConnectionChannel()
    challenge = gateway.challenge(owner.tenant_id, owner.user_id, identity_id, channel)
    signature = key.sign(
        connection_proof_bytes(
            digest,
            public,
            challenge.nonce,
            challenge.channel_digest,
            challenge.generation,
            challenge.issued_ms,
            challenge.expires_ms,
        )
    )
    with service._connect() as conn, conn.transaction():
        service._scope(conn, owner.tenant_id, owner.user_id)
        conn.execute(
            "UPDATE device_gateway.connection_challenges SET issued_ms=issued_ms+600000, "
            "expires_ms=expires_ms+600000 WHERE identity_id=%s",
            (identity_id,),
        )
    with pytest.raises(ConnectionRejected):
        gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, channel, signature)
    with pytest.raises(ConnectionRejected):
        gateway.challenge(owner.tenant_id, owner.user_id, identity_id, channel)
    with service._connect() as conn, conn.transaction():
        service._scope(conn, owner.tenant_id, owner.user_id)
        conn.execute(
            "UPDATE device_gateway.connection_challenges SET issued_ms=0, expires_ms=1 WHERE identity_id=%s",
            (identity_id,),
        )
    with pytest.raises(ConnectionRejected):
        gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, channel, signature)
    fresh = gateway.challenge(owner.tenant_id, owner.user_id, identity_id, channel)
    signature = key.sign(
        connection_proof_bytes(
            digest, public, fresh.nonce, fresh.channel_digest, fresh.generation, fresh.issued_ms, fresh.expires_ms
        )
    )
    session = gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, channel, signature)
    with service._connect() as conn, conn.transaction():
        service._scope(conn, owner.tenant_id, owner.user_id)
        conn.execute(
            "UPDATE device_gateway.connection_sessions SET last_heartbeat_ms=last_heartbeat_ms+600000 "
            "WHERE identity_id=%s",
            (identity_id,),
        )
    with pytest.raises(ConnectionRejected):
        gateway._heartbeat_unchecked_for_tests(
            owner.tenant_id, owner.user_id, identity_id, channel, session.generation, session.token
        )


def test_failed_session_commit_does_not_consume_challenge(service: PairingService) -> None:
    owner = make_scope()
    identity_id, key, public, digest = registered_device(service, owner)
    gateway = ConnectionService(service, 1)
    channel = ConnectionChannel()
    challenge = gateway.challenge(owner.tenant_id, owner.user_id, identity_id, channel)
    signature = key.sign(
        connection_proof_bytes(
            digest,
            public,
            challenge.nonce,
            challenge.channel_digest,
            challenge.generation,
            challenge.issued_ms,
            challenge.expires_ms,
        )
    )
    admin_dsn = os.environ["AIAGENT_DEVICE_TEST_ADMIN_DSN"]
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE FUNCTION device_gateway.reject_session() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'injected session failure'; END $$"
        )
        conn.execute(
            "CREATE TRIGGER reject_session BEFORE INSERT ON device_gateway.connection_sessions "
            "FOR EACH ROW EXECUTE FUNCTION device_gateway.reject_session()"
        )
    try:
        with pytest.raises(psycopg.Error):
            gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, channel, signature)
        with service._connect() as conn, conn.transaction():
            service._scope(conn, owner.tenant_id, owner.user_id)
            row = conn.execute(
                "SELECT used FROM device_gateway.connection_challenges WHERE identity_id=%s", (identity_id,)
            ).fetchone()
            assert row["used"] is False
            row = conn.execute(
                "SELECT connection_generation FROM device_gateway.device_identities WHERE id=%s", (identity_id,)
            ).fetchone()
            assert row["connection_generation"] == 0
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute("DROP TRIGGER reject_session ON device_gateway.connection_sessions")
            conn.execute("DROP FUNCTION device_gateway.reject_session()")
    session = gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, channel, signature)
    gateway._heartbeat_unchecked_for_tests(
        owner.tenant_id, owner.user_id, identity_id, channel, session.generation, session.token
    )


def test_challenge_replay_on_new_channel_and_disconnect_are_rejected(service: PairingService) -> None:
    owner = make_scope()
    identity_id, key, public, digest = registered_device(service, owner)
    gateway = ConnectionService(service, 1)
    old, new = ConnectionChannel(), ConnectionChannel()
    first = gateway.challenge(owner.tenant_id, owner.user_id, identity_id, old)
    signature = key.sign(
        connection_proof_bytes(
            digest, public, first.nonce, first.channel_digest, first.generation, first.issued_ms, first.expires_ms
        )
    )
    second = gateway.challenge(owner.tenant_id, owner.user_id, identity_id, new)
    assert first.nonce != second.nonce
    # A different, unauthenticated channel cannot replace the first challenge.
    assert first == gateway.challenge(owner.tenant_id, owner.user_id, identity_id, old)
    with pytest.raises(ConnectionRejected):
        gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, new, signature)
    fresh_sig = key.sign(
        connection_proof_bytes(
            digest, public, second.nonce, second.channel_digest, second.generation, second.issued_ms, second.expires_ms
        )
    )
    session = gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, new, fresh_sig)
    # The winning proof fences another valid proof for the same generation.
    with pytest.raises(ConnectionRejected):
        gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, old, signature)
    gateway.disconnect(owner.tenant_id, owner.user_id, identity_id, new)
    with pytest.raises(ConnectionRejected):
        gateway._heartbeat_unchecked_for_tests(
            owner.tenant_id, owner.user_id, identity_id, new, session.generation, session.token
        )
    with pytest.raises(ConnectionRejected):
        gateway.challenge(owner.tenant_id, owner.user_id, identity_id, new)


def test_disconnect_races_authentication_without_live_session(service: PairingService) -> None:
    owner = make_scope()
    identity_id, key, public, digest = registered_device(service, owner)
    gateway, channel = ConnectionService(service, 1), ConnectionChannel()
    challenge = gateway.challenge(owner.tenant_id, owner.user_id, identity_id, channel)
    signature = key.sign(
        connection_proof_bytes(
            digest,
            public,
            challenge.nonce,
            challenge.channel_digest,
            challenge.generation,
            challenge.issued_ms,
            challenge.expires_ms,
        )
    )

    def authenticate():
        try:
            return gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, channel, signature)
        except ConnectionRejected:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        auth_future = pool.submit(authenticate)
        close_future = pool.submit(gateway.disconnect, owner.tenant_id, owner.user_id, identity_id, channel)
        session = auth_future.result()
        close_future.result()
    if session is not None:
        with pytest.raises(ConnectionRejected):
            gateway._heartbeat_unchecked_for_tests(
                owner.tenant_id, owner.user_id, identity_id, channel, session.generation, session.token
            )
    with pytest.raises(ConnectionRejected):
        gateway.challenge(owner.tenant_id, owner.user_id, identity_id, channel)


def test_channel_cannot_switch_owner_or_identity(service: PairingService) -> None:
    alice, bob = make_scope(), make_scope()
    alice_id, key, public, digest = registered_device(service, alice)
    bob_id, _, _, _ = registered_device(service, bob)
    gateway, channel = ConnectionService(service, 1), ConnectionChannel()
    challenge = gateway.challenge(alice.tenant_id, alice.user_id, alice_id, channel)
    with pytest.raises(ConnectionRejected):
        gateway.challenge(bob.tenant_id, bob.user_id, bob_id, channel)
    with pytest.raises(ConnectionRejected):
        gateway.disconnect(bob.tenant_id, bob.user_id, bob_id, channel)
    signature = key.sign(
        connection_proof_bytes(
            digest,
            public,
            challenge.nonce,
            challenge.channel_digest,
            challenge.generation,
            challenge.issued_ms,
            challenge.expires_ms,
        )
    )
    session = gateway.authenticate(alice.tenant_id, alice.user_id, alice_id, channel, signature)
    gateway._heartbeat_unchecked_for_tests(
        alice.tenant_id, alice.user_id, alice_id, channel, session.generation, session.token
    )


def test_challenge_flood_does_not_replace_existing_proof(service: PairingService) -> None:
    owner = make_scope()
    identity_id, key, public, digest = registered_device(service, owner)
    gateway, legitimate = ConnectionService(service, 1), ConnectionChannel()
    first = gateway.challenge(owner.tenant_id, owner.user_id, identity_id, legitimate)
    for _ in range(63):
        gateway.challenge(owner.tenant_id, owner.user_id, identity_id, ConnectionChannel())
    with pytest.raises(ConnectionRejected):
        gateway.challenge(owner.tenant_id, owner.user_id, identity_id, ConnectionChannel())
    signature = key.sign(
        connection_proof_bytes(
            digest, public, first.nonce, first.channel_digest, first.generation, first.issued_ms, first.expires_ms
        )
    )
    gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, legitimate, signature)
    with service._connect() as conn, conn.transaction():
        service._scope(conn, owner.tenant_id, owner.user_id)
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM device_gateway.connection_challenges WHERE identity_id=%s", (identity_id,)
            ).fetchone()["n"]
            == 0
        )
    gateway.challenge(owner.tenant_id, owner.user_id, identity_id, ConnectionChannel())


def test_concurrent_channel_binding_has_one_owner(service: PairingService) -> None:
    alice, bob = make_scope(), make_scope()
    alice_id, _, _, _ = registered_device(service, alice)
    bob_id, _, _, _ = registered_device(service, bob)
    gateway, channel = ConnectionService(service, 1), ConnectionChannel()

    def attempt(scope: Scope, identity_id: uuid.UUID) -> bool:
        try:
            gateway.challenge(scope.tenant_id, scope.user_id, identity_id, channel)
            return True
        except ConnectionRejected:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(attempt, alice, alice_id)
        second = pool.submit(attempt, bob, bob_id)
        assert sorted([first.result(), second.result()]) == [False, True]


def test_signed_heartbeat_sequence_is_atomic_and_replay_safe(service: PairingService) -> None:
    owner = make_scope()
    identity_id, key, public, digest = registered_device(service, owner)
    gateway, channel = ConnectionService(service, 1), ConnectionChannel()
    challenge = gateway.challenge(owner.tenant_id, owner.user_id, identity_id, channel)
    proof = key.sign(
        connection_proof_bytes(
            digest,
            public,
            challenge.nonce,
            challenge.channel_digest,
            challenge.generation,
            challenge.issued_ms,
            challenge.expires_ms,
        )
    )
    session = gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, channel, proof)
    message = (
        b"aiagent-device-heartbeat-candidate-v1"
        + digest
        + public
        + challenge.channel_digest
        + session.generation.to_bytes(8, "big")
        + (1).to_bytes(8, "big")
    )
    signature = key.sign(message)
    with pytest.raises(ConnectionRejected):
        gateway.signed_heartbeat(
            owner.tenant_id, owner.user_id, identity_id, channel, session.generation, session.token, 1, bytes(64)
        )

    def attempt() -> bool:
        try:
            gateway.signed_heartbeat(
                owner.tenant_id, owner.user_id, identity_id, channel, session.generation, session.token, 1, signature
            )
            return True
        except ConnectionRejected:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [False, True]
    with service._connect() as conn, conn.transaction():
        service._scope(conn, owner.tenant_id, owner.user_id)
        assert (
            conn.execute(
                "SELECT heartbeat_sequence FROM device_gateway.connection_sessions WHERE identity_id=%s", (identity_id,)
            ).fetchone()["heartbeat_sequence"]
            == 1
        )


def test_wss_frame_validation_rejects_duplicates_and_unknown_versions() -> None:
    for raw in (
        '{"version":"1.1","version":"1.1","kind":"PROOF","signature":"' + "00" * 64 + '"}',
        '{"version":"1.0","kind":"PROOF","signature":"' + "00" * 64 + '"}',
        '{"version":"2.0","kind":"PROOF","signature":"' + "00" * 64 + '"}',
        '{"version":"1.1","kind":"PROOF","signature":"' + "00" * 64 + '","extra":1}',
    ):
        with pytest.raises(InvalidFrame):
            decode_frame(raw, "PROOF")
    for sequence in ("0", "01", "9223372036854775808", "1\n"):
        with pytest.raises(InvalidFrame):
            decode_frame(
                json.dumps(
                    {
                        "version": "1.1",
                        "kind": "HEARTBEAT",
                        "generation": "1",
                        "sequence": sequence,
                        "signature": "00" * 64,
                    }
                ),
                "HEARTBEAT",
            )
    with pytest.raises(InvalidFrame):
        decode_frame(b"{}", "PROOF")
    with pytest.raises(InvalidFrame):
        decode_frame('{"version":"1.1","kind":"ACTION_OFFER"}', "HEARTBEAT")


def test_real_tls_websocket_challenge_heartbeat_and_replay(service: PairingService, tmp_path: Path) -> None:
    owner = make_scope()
    identity_id, key, public, digest = registered_device(service, owner)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=5))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .sign(private, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "test-cert.pem", tmp_path / "test-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert_path, key_path)
    client_tls = ssl.create_default_context(cafile=str(cert_path))
    gateway = DeviceWss(ConnectionService(service, 1), pairing=service)

    async def exercise() -> None:
        with pytest.raises(ValueError):
            gateway.listen("127.0.0.1", 0, server_tls)
        with pytest.raises(ValueError):
            gateway.listen("0.0.0.0", 0, server_tls, development_only=True)  # noqa: S104 -- rejection test
        async with gateway.listen("127.0.0.1", 0, server_tls, development_only=True) as server:
            port = server.sockets[0].getsockname()[1]
            url = f"wss://localhost:{port}/v1/device/connect"
            pairing_url = f"wss://localhost:{port}/v1/device/pair"
            ticket = await asyncio.to_thread(service.begin, owner)
            pair_key = Ed25519PrivateKey.generate()
            pair_public = pair_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            pair_digest = bytes([42]) * 32
            attach = {
                "version": "1.0",
                "kind": "ATTACH",
                "tenantId": owner.tenant_id,
                "ownerId": owner.user_id,
                "pairingId": str(ticket.pairing_id),
                "token": ticket.token.hex(),
                "publicKey": pair_public.hex(),
                "bindingDigest": pair_digest.hex(),
                "signature": pair_key.sign(
                    pairing_proof_bytes(
                        ticket.pairing_id, pair_digest, pair_public, ticket.nonce, ticket.issued_ms, ticket.expires_ms
                    )
                ).hex(),
            }
            async with connect(pairing_url, ssl=client_tls, subprotocols=["aiagent-device.v1"], proxy=None) as socket:
                await socket.send(json.dumps(attach))
                code = decode_frame(await socket.recv(), "CODE", pairing=True)
                assert code["bindingDigest"] == pair_digest.hex()
                assert code["publicKeySha256"] == hashlib.sha256(pair_public).hexdigest()
                await asyncio.to_thread(
                    service.approve, owner, ticket.pairing_id, code["code"], code["publicKeySha256"], pair_digest
                )
                await socket.send(json.dumps({"version": "1.0", "kind": "CONSUME"}))
                paired = decode_frame(await socket.recv(), "PAIRED", pairing=True)
                assert (paired["identityId"], paired["deviceId"]) == (str(ticket.identity_id), str(ticket.device_id))
            async with connect(pairing_url, ssl=client_tls, subprotocols=["aiagent-device.v1"], proxy=None) as socket:
                await socket.send(json.dumps(attach))
                with pytest.raises(ConnectionClosed):
                    await socket.recv()
            unapproved = await asyncio.to_thread(service.begin, owner)
            attach.update(
                {
                    "pairingId": str(unapproved.pairing_id),
                    "token": unapproved.token.hex(),
                    "signature": pair_key.sign(
                        pairing_proof_bytes(
                            unapproved.pairing_id,
                            pair_digest,
                            pair_public,
                            unapproved.nonce,
                            unapproved.issued_ms,
                            unapproved.expires_ms,
                        )
                    ).hex(),
                }
            )
            async with connect(pairing_url, ssl=client_tls, subprotocols=["aiagent-device.v1"], proxy=None) as socket:
                await socket.send(json.dumps(attach))
                decode_frame(await socket.recv(), "CODE", pairing=True)
                await socket.send(json.dumps({"version": "1.0", "kind": "CONSUME"}))
                with pytest.raises(ConnectionClosed):
                    await socket.recv()
            with pytest.raises(PairingRejected):
                await asyncio.to_thread(
                    service.consume, owner.tenant_id, owner.user_id, unapproved.pairing_id, unapproved.token
                )
            with pytest.raises(ssl.SSLCertVerificationError):
                async with connect(f"wss://127.0.0.1:{port}/v1/device/connect", ssl=client_tls, proxy=None):
                    pass
            with pytest.raises(ssl.SSLCertVerificationError):
                async with connect(url, ssl=ssl.create_default_context(), proxy=None):
                    pass
            hello = {
                "version": "1.1",
                "kind": "HELLO",
                "tenantId": owner.tenant_id,
                "ownerId": owner.user_id,
                "identityId": str(identity_id),
            }
            async with connect(url, ssl=client_tls, subprotocols=["aiagent-device.v1"], proxy=None) as socket:
                await socket.send(json.dumps(hello))
                challenge = decode_frame(await socket.recv(), "CHALLENGE")
                payload = connection_proof_bytes(
                    digest,
                    public,
                    bytes.fromhex(challenge["nonce"]),
                    bytes.fromhex(challenge["channelDigest"]),
                    int(challenge["generation"]),
                    int(challenge["issuedMs"]),
                    int(challenge["expiresMs"]),
                )
                signature = key.sign(payload).hex()
                await socket.send(json.dumps({"version": "1.1", "kind": "PROOF", "signature": signature}))
                accepted = decode_frame(await socket.recv(), "ACCEPTED")
                heartbeat = {
                    "version": "1.1",
                    "kind": "HEARTBEAT",
                    "generation": accepted["generation"],
                    "sequence": "1",
                    "signature": key.sign(
                        b"aiagent-device-heartbeat-candidate-v1"
                        + digest
                        + public
                        + bytes.fromhex(challenge["channelDigest"])
                        + int(accepted["generation"]).to_bytes(8, "big")
                        + (1).to_bytes(8, "big")
                    ).hex(),
                }
                await socket.send(json.dumps(heartbeat))
                assert decode_frame(await socket.recv(), "HEARTBEAT_ACK")["sequence"] == "1"
                await socket.send(json.dumps(heartbeat))
                with pytest.raises(ConnectionClosed):
                    await socket.recv()
            async with connect(url, ssl=client_tls, subprotocols=["aiagent-device.v1"], proxy=None) as socket:
                await socket.send(json.dumps(hello))
                decode_frame(await socket.recv(), "CHALLENGE")
                await socket.send(json.dumps({"version": "1.1", "kind": "PROOF", "signature": signature}))
                with pytest.raises(ConnectionClosed):
                    await socket.recv()
            async with connect(url, ssl=client_tls, subprotocols=["aiagent-device.v1"], proxy=None) as socket:
                await socket.send(json.dumps(hello))
                challenge = decode_frame(await socket.recv(), "CHALLENGE")
                payload = connection_proof_bytes(
                    digest,
                    public,
                    bytes.fromhex(challenge["nonce"]),
                    bytes.fromhex(challenge["channelDigest"]),
                    int(challenge["generation"]),
                    int(challenge["issuedMs"]),
                    int(challenge["expiresMs"]),
                )
                await socket.send(json.dumps({"version": "1.1", "kind": "PROOF", "signature": key.sign(payload).hex()}))
                accepted = decode_frame(await socket.recv(), "ACCEPTED")
                await socket.send(
                    json.dumps(
                        {
                            "version": "1.1",
                            "kind": "HEARTBEAT",
                            "generation": accepted["generation"],
                            "sequence": "1",
                            "signature": "00" * 64,
                        }
                    )
                )
                with pytest.raises(ConnectionClosed):
                    await socket.recv()

    asyncio.run(exercise())


def test_windows_rust_dpapi_client_to_python_gateway(service: PairingService, tmp_path: Path) -> None:
    exe = os.environ.get("AIAGENT_RUST_TEST_CLIENT")
    if sys.platform != "win32" or not exe:
        pytest.skip("requires explicitly built Windows DPAPI integration fixture")
    owner = make_scope()
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=5))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .sign(private, hashes.SHA256())
    )
    cert_pem, cert_der, key_path = tmp_path / "cert.pem", tmp_path / "cert.der", tmp_path / "key.pem"
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cert_der.write_bytes(cert.public_bytes(serialization.Encoding.DER))
    key_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    root = tmp_path / "identity-root"
    root.mkdir()
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert_pem, key_path)

    async def exercise() -> None:
        gateway = DeviceWss(ConnectionService(service, 1), pairing=service)
        async with gateway.listen("127.0.0.1", 0, server_tls, development_only=True) as server:
            port = server.sockets[0].getsockname()[1]
            origin = f"https://localhost:{port}"
            scoped = PairingService(service._dsn, origin, bytes(range(32)))
            ticket = await asyncio.to_thread(scoped.begin, owner)
            proc = await asyncio.create_subprocess_exec(
                exe, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
                config = {
                    "tenant": owner.tenant_id,
                    "owner": owner.user_id,
                    "identity": str(ticket.identity_id),
                    "device": str(ticket.device_id),
                    "origin": origin,
                    "url": f"wss://localhost:{port}/v1/device/connect",
                    "pairUrl": f"wss://localhost:{port}/v1/device/pair",
                    "certDer": str(cert_der),
                    "root": str(root),
                    "pairingIdHex": ticket.pairing_id.hex,
                    "nonceHex": ticket.nonce.hex(),
                    "tokenHex": ticket.token.hex(),
                    "issuedMs": str(ticket.issued_ms),
                    "expiresMs": str(ticket.expires_ms),
                }
                proc.stdin.write((json.dumps(config) + "\n").encode())
                await proc.stdin.drain()
                code = json.loads(await asyncio.wait_for(proc.stdout.readline(), 20))
                assert code["kind"] == "CODE"
                pending = await asyncio.to_thread(scoped.inspect, owner, ticket.pairing_id)
                await asyncio.to_thread(
                    scoped.approve,
                    owner,
                    ticket.pairing_id,
                    code["code"],
                    pending.public_key_sha256,
                    pending.binding_digest,
                )
                proc.stdin.write(b"CONTINUE\n")
                await proc.stdin.drain()
                assert json.loads(await asyncio.wait_for(proc.stdout.readline(), 20)) == {"kind": "OK"}
                assert await asyncio.wait_for(proc.wait(), 20) == 0
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
        with service._connect() as conn, conn.transaction():
            service._scope(conn, owner.tenant_id, owner.user_id)
            state = conn.execute(
                "SELECT valid FROM device_gateway.connection_sessions WHERE identity_id=%s", (ticket.identity_id,)
            ).fetchone()
            assert state is not None and state["valid"] is False

    asyncio.run(exercise())
