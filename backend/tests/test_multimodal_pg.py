"""PostgreSQL store integration: RLS, owner isolation, idempotency, lease CAS."""

from __future__ import annotations

import hashlib
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
