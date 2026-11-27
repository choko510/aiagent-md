"""PostgreSQL store integration: RLS, owner isolation, idempotency, lease CAS."""

from __future__ import annotations

import hashlib
import threading
import time
import uuid

import pytest
from aiagent_core import admin
from aiagent_multimodal.errors import MmiError
from aiagent_multimodal.store import PostgresStore, Principal


def _principal(tenant: dict[str, str]) -> Principal:
    return Principal(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])


def _upload(store: PostgresStore, principal: Principal, *, key: str = "u1") -> str:
    upload_id = str(uuid.uuid4())
    return store.create_upload(
        principal,
        upload_id=upload_id,
        original_filename="x.csv",
        declared_media_type="text/csv",
        expected_bytes=4,
        content_sha256=hashlib.sha256(b"a,b\n").hexdigest(),
        idempotency_key=key,
        expires_at="2030-01-01T00:00:00Z",
    ).id


def test_postgres_roundtrip_and_owner_isolation(pg, tenant) -> None:
    store = PostgresStore(pg.db("api", max_size=4))
    principal = _principal(tenant)
    upload_id = _upload(store, principal)
    assert store.get_upload(principal, upload_id) is not None
    store.append_chunk(principal, upload_id=upload_id, index=0, byte_length=4, chunk_sha256="a" * 64)
    artifact_id = str(uuid.uuid4())
    artifact = store.complete_upload(
        principal,
        upload_id=upload_id,
        artifact_id=artifact_id,
        detected_format="csv",
        storage_key=f"{principal.tenant_id}/{artifact_id}.bin",
        content_sha256=hashlib.sha256(b"a,b\n").hexdigest(),
        byte_length=4,
        expires_at="2030-01-01T00:00:00Z",
    )
    assert artifact.quarantine_state == "quarantined"
    admitted = store.admit_artifact(principal, artifact_id)
    assert admitted.quarantine_state == "admitted"

    other_user = admin.add_user(pg.owner, tenant["tenantId"])
    other = Principal(other_user["tenantId"], other_user["userId"], other_user["workspaceId"])
    assert store.get_artifact(other, artifact_id) is None
    with pytest.raises(MmiError):
        store.admit_artifact(other, artifact_id)

    job_id = str(uuid.uuid4())
    job = store.create_job(
        principal,
        job_id=job_id,
        artifact_id=artifact_id,
        parse_key="b" * 64,
        request_sha256="c" * 64,
        request={
            "version": "1.0",
            "sourceArtifactId": artifact_id,
            "source": {"kind": "user_upload", "acquiredAt": "2026-01-01T00:00:00Z"},
            "requestedFields": ["amount"],
            "preferredLocale": None,
            "allowLocalOcr": False,
            "allowExternalVision": False,
            "idempotencyKey": "r",
            "deadlineAt": "2030-01-01T00:00:00Z",
        },
        expires_at="2030-01-01T00:00:00Z",
    )
    assert job.status == "queued"
    claimed = store.claim_job(principal, job_id, lease_owner="worker-a", lease_seconds=60)
    assert claimed.lease_epoch == 1
    assert (
        store.finish_job(
            principal,
            job_id,
            lease_owner="worker-b",
            lease_epoch=claimed.lease_epoch,
            outcome={},
            document=None,
            error_code=None,
        )
        is False
    )
    assert (
        store.finish_job(
            principal,
            job_id,
            lease_owner="worker-a",
            lease_epoch=claimed.lease_epoch,
            outcome={"version": "1.0"},
            document={"sourceArtifactId": artifact_id},
            error_code=None,
        )
        is True
    )
    latest = store.latest_job(principal, artifact_id)
    assert latest is not None and latest.status == "succeeded"
    assert latest.document == {"sourceArtifactId": artifact_id}


def test_postgres_idempotent_upload_replay(pg, tenant) -> None:
    store = PostgresStore(pg.db("api", max_size=4))
    principal = _principal(tenant)
    first = _upload(store, principal, key="same")
    second = _upload(store, principal, key="same")
    assert first == second
    assert store.get_upload(principal, first) is not None


def _admitted_artifact(store: PostgresStore, principal: Principal, *, key: str) -> str:
    upload_id = _upload(store, principal, key=key)
    store.append_chunk(principal, upload_id=upload_id, index=0, byte_length=4, chunk_sha256="a" * 64)
    artifact_id = str(uuid.uuid4())
    store.complete_upload(
        principal,
        upload_id=upload_id,
        artifact_id=artifact_id,
        detected_format="csv",
        storage_key=f"{principal.tenant_id}/{artifact_id}.bin",
        content_sha256=hashlib.sha256(b"a,b\n").hexdigest(),
        byte_length=4,
        expires_at="2030-01-01T00:00:00Z",
    )
    return store.admit_artifact(principal, artifact_id).id


def _queued_job(store: PostgresStore, principal: Principal, artifact_id: str, *, key: str) -> str:
    job_id = str(uuid.uuid4())
    store.create_job(
        principal,
        job_id=job_id,
        artifact_id=artifact_id,
        parse_key=key,
        request_sha256="c" * 64,
        request={"version": "1.0"},
        expires_at="2030-01-01T00:00:00Z",
    )
    return job_id


def test_postgres_chunk_replay_and_conflict(pg, tenant) -> None:
    store = PostgresStore(pg.db("api", max_size=4))
    principal = _principal(tenant)
    upload_id = _upload(store, principal, key="chunks")
    first = store.append_chunk(principal, upload_id=upload_id, index=0, byte_length=4, chunk_sha256="a" * 64)
    assert first.received_bytes == 4 and first.chunk_count == 1
    replay = store.append_chunk(principal, upload_id=upload_id, index=0, byte_length=4, chunk_sha256="a" * 64)
    assert replay.received_bytes == 4 and replay.chunk_count == 1
    assert store.get_chunk(principal, upload_id, 0) == (4, "a" * 64)
    with pytest.raises(MmiError) as conflict:
        store.append_chunk(principal, upload_id=upload_id, index=0, byte_length=4, chunk_sha256="b" * 64)
    assert conflict.value.code == "CHUNK_CONFLICT"
    assert store.get_upload(principal, upload_id).received_bytes == 4


def test_postgres_chunk_transaction_serializes_writers(pg, tenant) -> None:
    store = PostgresStore(pg.db("api", max_size=4))
    principal = _principal(tenant)
    upload_id = _upload(store, principal, key="lock")
    entered = threading.Event()
    failure: list[BaseException] = []

    def second_writer() -> None:
        entered.set()
        try:
            with store.chunk_transaction(principal, upload_id, 0) as chunk:
                chunk.append(byte_length=4, chunk_sha256="a" * 64)
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
            failure.append(exc)

    with store.chunk_transaction(principal, upload_id, 0) as chunk:
        assert chunk.get() is None
        writer = threading.Thread(target=second_writer)
        writer.start()
        assert entered.wait(2)
        time.sleep(0.2)
        assert writer.is_alive()
        assert store.get_chunk(principal, upload_id, 0) is None
    writer.join(5)
    assert not writer.is_alive()
    assert failure == []
    assert store.get_chunk(principal, upload_id, 0) == (4, "a" * 64)


def test_postgres_expired_lease_and_revocation_fence(pg, tenant) -> None:
    store = PostgresStore(pg.db("api", max_size=4))
    principal = _principal(tenant)
    artifact_id = _admitted_artifact(store, principal, key="lease")
    job_id = _queued_job(store, principal, artifact_id, key="d" * 64)
    claimed = store.claim_job(principal, job_id, lease_owner="worker-a", lease_seconds=60)
    assert claimed.lease_epoch == 1
    with store.db.tx(principal.tenant_id) as c:
        c.execute(
            "UPDATE agent.mmi_parse_jobs SET lease_expires_at=now()-interval '1 second' WHERE tenant_id=%s AND id=%s",
            (principal.tenant_id, job_id),
        )
    assert (
        store.finish_job(
            principal,
            job_id,
            lease_owner="worker-a",
            lease_epoch=claimed.lease_epoch,
            outcome={"status": "complete"},
            document={"sourceArtifactId": artifact_id},
            error_code=None,
        )
        is False
    )
    reclaimed = store.claim_job(principal, job_id, lease_owner="worker-b", lease_seconds=60)
    assert reclaimed.lease_epoch == 2
    store.revoke_artifact(principal, artifact_id)
    assert (
        store.finish_job(
            principal,
            job_id,
            lease_owner="worker-b",
            lease_epoch=reclaimed.lease_epoch,
            outcome={"status": "complete"},
            document={"sourceArtifactId": artifact_id},
            error_code=None,
        )
        is False
    )
    after = store.get_job(principal, job_id)
    assert after is not None
    assert after.status == "cancelled"
    assert after.outcome is None and after.document is None
    assert store.get_artifact(principal, artifact_id).quarantine_state == "revoked"
