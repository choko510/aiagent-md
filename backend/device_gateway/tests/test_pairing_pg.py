"""Real PostgreSQL fixture tests; no production migration or HTTP route is installed."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from backend.device_gateway.connections import ConnectionRejected, ConnectionService, connection_proof_bytes
from backend.device_gateway.pairing import PairingRejected, PairingService, pairing_proof_bytes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


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


def test_unpatched_ed25519_backend_cannot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backend.device_gateway.pairing.crypto_core_ed25519_is_valid_point", lambda _: True)
    with pytest.raises(RuntimeError, match="UNSAFE_ED25519_BACKEND"):
        PairingService("unused", "https://gateway.example", bytes(range(32)))


def test_rust_and_python_share_connection_proof_vector() -> None:
    vector = json.loads(
        (Path(__file__).resolve().parents[3] / "device-client/fixtures/connection-proof-v1.json").read_text()
    )
    public = bytes.fromhex(vector["publicKeyHex"])
    payload = connection_proof_bytes(
        bytes.fromhex(vector["bindingDigestHex"]),
        public,
        bytes.fromhex(vector["nonceHex"]),
        int(vector["generation"]),
        int(vector["issuedMs"]),
        int(vector["expiresMs"]),
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
        conn.execute("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA device_gateway TO ldc_pairing_test")
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
    assert (ticket.tenant_id, ticket.owner_user_id, ticket.revocation_generation) == (owner.tenant_id, owner.user_id, 0)
    assert ticket.token.hex() not in repr(ticket)
    with pytest.raises(PairingRejected):
        service.attach(owner.tenant_id, owner.user_id, ticket.pairing_id, b"x" * 32, b"x" * 32, b"x" * 32, b"x" * 64)
    wrong_key = Ed25519PrivateKey.generate()
    public = wrong_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    with pytest.raises(PairingRejected):
        service.attach(owner.tenant_id, owner.user_id, ticket.pairing_id, ticket.token, public, bytes(32), bytes(64))
    code, key, public, digest, proof = attach(service, owner, ticket)
    assert code.code not in repr(code)
    pending = service.inspect(owner, ticket.pairing_id)
    assert pending.public_key_sha256 == code.public_key_sha256
    assert pending.binding_digest == digest
    assert pending.identity_id == ticket.identity_id
    with pytest.raises(PairingRejected):
        service.attach(owner.tenant_id, owner.user_id, ticket.pairing_id, ticket.token, public, digest, key.sign(proof))
    for other in (Scope(owner.tenant_id, str(uuid.uuid4())), make_scope()):
        with pytest.raises(PairingRejected):
            service.inspect(other, ticket.pairing_id)
        with pytest.raises(PairingRejected):
            service.approve(other, ticket.pairing_id, code.code, code.public_key_sha256, digest)
    with pytest.raises(PairingRejected):
        service.approve(owner, ticket.pairing_id, code.code, code.public_key_sha256, b"z" * 32)
    service.approve(owner, ticket.pairing_id, code.code, code.public_key_sha256, digest)
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
    challenge = gateway.challenge(owner.tenant_id, owner.user_id, identity_id)
    assert challenge == gateway.challenge(owner.tenant_id, owner.user_id, identity_id)
    message = connection_proof_bytes(
        digest, public, challenge.nonce, challenge.generation, challenge.issued_ms, challenge.expires_ms
    )
    with pytest.raises(ConnectionRejected):
        gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, bytes(64))
    signature = key.sign(message)

    def attempt():
        try:
            return gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, signature)
        except ConnectionRejected:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(lambda _: attempt(), range(2)))
    sessions = [session for session in attempts if session is not None]
    assert len(sessions) == 1
    session = sessions[0]
    assert session.token.hex() not in repr(session)
    gateway.heartbeat(owner.tenant_id, owner.user_id, identity_id, session.generation, session.token)
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
            gateway.heartbeat(tenant, user, identity_id, generation, token)
    reconnect = gateway.challenge(owner.tenant_id, owner.user_id, identity_id)
    assert reconnect.generation == session.generation + 1
    second_sig = key.sign(
        connection_proof_bytes(
            digest, public, reconnect.nonce, reconnect.generation, reconnect.issued_ms, reconnect.expires_ms
        )
    )
    second = gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, second_sig)
    with pytest.raises(ConnectionRejected):
        gateway.heartbeat(owner.tenant_id, owner.user_id, identity_id, session.generation, session.token)
    gateway.heartbeat(owner.tenant_id, owner.user_id, identity_id, second.generation, second.token)
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
        gateway.heartbeat(owner.tenant_id, owner.user_id, identity_id, second.generation, second.token)
    with pytest.raises(ConnectionRejected):
        gateway.challenge(owner.tenant_id, owner.user_id, identity_id)


def test_new_generation_expires_and_rejects_old_restore_incarnation(service: PairingService) -> None:
    owner = make_scope()
    identity_id, key, public, digest = registered_device(service, owner)
    gateway = ConnectionService(service, 1)
    first = gateway.challenge(owner.tenant_id, owner.user_id, identity_id)
    signature = key.sign(
        connection_proof_bytes(digest, public, first.nonce, first.generation, first.issued_ms, first.expires_ms)
    )
    session = gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, signature)
    restarted = ConnectionService(service, 2)
    with pytest.raises(ConnectionRejected):
        restarted.heartbeat(owner.tenant_id, owner.user_id, identity_id, session.generation, session.token)
    next_challenge = restarted.challenge(owner.tenant_id, owner.user_id, identity_id)
    assert next_challenge.generation == session.generation + 1
    with pytest.raises(ConnectionRejected):
        gateway.challenge(owner.tenant_id, owner.user_id, identity_id)
    with pytest.raises(ConnectionRejected):
        gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, signature)
    with pytest.raises(ConnectionRejected):
        restarted.authenticate(owner.tenant_id, owner.user_id, identity_id, signature)
    next_signature = key.sign(
        connection_proof_bytes(
            digest,
            public,
            next_challenge.nonce,
            next_challenge.generation,
            next_challenge.issued_ms,
            next_challenge.expires_ms,
        )
    )
    next_session = restarted.authenticate(owner.tenant_id, owner.user_id, identity_id, next_signature)
    with pytest.raises(ConnectionRejected):
        restarted.heartbeat(owner.tenant_id, owner.user_id, identity_id, session.generation, session.token)
    restarted.heartbeat(owner.tenant_id, owner.user_id, identity_id, next_session.generation, next_session.token)
    with service._connect() as conn, conn.transaction():
        service._scope(conn, owner.tenant_id, owner.user_id)
        conn.execute("UPDATE device_gateway.connection_sessions SET expires_ms=0 WHERE identity_id=%s", (identity_id,))
    with pytest.raises(ConnectionRejected):
        restarted.heartbeat(owner.tenant_id, owner.user_id, identity_id, next_session.generation, next_session.token)


def test_revocation_races_authentication_without_live_session(service: PairingService) -> None:
    owner = make_scope()
    identity_id, key, public, digest = registered_device(service, owner)
    gateway = ConnectionService(service, 1)
    challenge = gateway.challenge(owner.tenant_id, owner.user_id, identity_id)
    signature = key.sign(
        connection_proof_bytes(
            digest, public, challenge.nonce, challenge.generation, challenge.issued_ms, challenge.expires_ms
        )
    )

    def authenticate():
        try:
            return gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, signature)
        except ConnectionRejected:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        auth_future = pool.submit(authenticate)
        revoke_future = pool.submit(gateway.revoke, owner, identity_id)
        session = auth_future.result()
        revoke_future.result()
    if session is not None:
        with pytest.raises(ConnectionRejected):
            gateway.heartbeat(owner.tenant_id, owner.user_id, identity_id, session.generation, session.token)
    with pytest.raises(ConnectionRejected):
        gateway.challenge(owner.tenant_id, owner.user_id, identity_id)


def test_connection_rejects_expired_or_future_challenges_and_rolled_back_clock(service: PairingService) -> None:
    owner = make_scope()
    identity_id, key, public, digest = registered_device(service, owner)
    gateway = ConnectionService(service, 1)
    challenge = gateway.challenge(owner.tenant_id, owner.user_id, identity_id)
    signature = key.sign(
        connection_proof_bytes(
            digest, public, challenge.nonce, challenge.generation, challenge.issued_ms, challenge.expires_ms
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
        gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, signature)
    with pytest.raises(ConnectionRejected):
        gateway.challenge(owner.tenant_id, owner.user_id, identity_id)
    with service._connect() as conn, conn.transaction():
        service._scope(conn, owner.tenant_id, owner.user_id)
        conn.execute(
            "UPDATE device_gateway.connection_challenges SET issued_ms=0, expires_ms=1 WHERE identity_id=%s",
            (identity_id,),
        )
    with pytest.raises(ConnectionRejected):
        gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, signature)
    fresh = gateway.challenge(owner.tenant_id, owner.user_id, identity_id)
    signature = key.sign(
        connection_proof_bytes(digest, public, fresh.nonce, fresh.generation, fresh.issued_ms, fresh.expires_ms)
    )
    session = gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, signature)
    with service._connect() as conn, conn.transaction():
        service._scope(conn, owner.tenant_id, owner.user_id)
        conn.execute(
            "UPDATE device_gateway.connection_sessions SET last_heartbeat_ms=last_heartbeat_ms+600000 "
            "WHERE identity_id=%s",
            (identity_id,),
        )
    with pytest.raises(ConnectionRejected):
        gateway.heartbeat(owner.tenant_id, owner.user_id, identity_id, session.generation, session.token)


def test_failed_session_commit_does_not_consume_challenge(service: PairingService) -> None:
    owner = make_scope()
    identity_id, key, public, digest = registered_device(service, owner)
    gateway = ConnectionService(service, 1)
    challenge = gateway.challenge(owner.tenant_id, owner.user_id, identity_id)
    signature = key.sign(
        connection_proof_bytes(
            digest, public, challenge.nonce, challenge.generation, challenge.issued_ms, challenge.expires_ms
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
            gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, signature)
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
    session = gateway.authenticate(owner.tenant_id, owner.user_id, identity_id, signature)
    gateway.heartbeat(owner.tenant_id, owner.user_id, identity_id, session.generation, session.token)
