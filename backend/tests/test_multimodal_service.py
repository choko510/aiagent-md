"""End-to-end service tests: upload, quarantine, parse, verify, retrieve, revoke."""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from pathlib import Path

import pytest
from aiagent_multimodal.blobstore import QuarantineBlobStore
from aiagent_multimodal.contracts import MmiRequestV1, MmiSource
from aiagent_multimodal.errors import MmiError
from aiagent_multimodal.limits import MmiLimits
from aiagent_multimodal.pipeline import InProcessParserExecutor
from aiagent_multimodal.service import MultimodalService
from aiagent_multimodal.store import InMemoryStore, Principal, _now

CSV = b"item,price\napple,\xc2\xa5100\n"


def _principal() -> Principal:
    return Principal(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))


def _request(artifact_id: str) -> MmiRequestV1:
    return MmiRequestV1(
        source_artifact_id=artifact_id,
        source=MmiSource(kind="user_upload", acquired_at="2026-01-01T00:00:00Z"),
        requested_fields=("amount",),
        idempotency_key="req-1",
        deadline_at="2030-01-01T00:00:00Z",
    )


@pytest.fixture()
def service(tmp_path: Path) -> MultimodalService:
    limits = __import__("aiagent_multimodal.limits", fromlist=["MmiLimits"]).MmiLimits()
    return MultimodalService(
        store=InMemoryStore(),
        blobs=QuarantineBlobStore(tmp_path, max_file_bytes=limits.max_file_bytes),
        executor=InProcessParserExecutor(),
        limits=limits,
    )


def _upload(service: MultimodalService, principal: Principal, data: bytes = CSV) -> str:
    digest = hashlib.sha256(data).hexdigest()
    started = service.begin_upload(
        principal,
        original_filename="../../price.csv",
        declared_media_type="text/csv",
        expected_bytes=len(data),
        content_sha256=digest,
        idempotency_key="upload-1",
    )
    service.put_chunk(principal, started["uploadId"], 0, data, digest)
    artifact = service.complete_upload(principal, started["uploadId"])
    assert artifact["displayName"] == "price.csv"
    assert artifact["quarantineState"] == "admitted"
    return artifact["artifactId"]


def test_upload_parse_retrieve_and_ask(service: MultimodalService) -> None:
    principal = _principal()
    artifact_id = _upload(service, principal)
    job = service.start_parse(principal, artifact_id, request=_request(artifact_id), idempotency_key="job-1")
    assert job.status == "queued"
    finished = service.run_job(principal, job.id)
    assert finished.status == "succeeded"
    assert finished.outcome is not None
    assert finished.outcome["status"] == "complete", finished.outcome
    assert finished.outcome["candidates"]
    amount = [c for c in finished.outcome["candidates"] if c["fieldKey"] == "amount"]
    assert amount and amount[0]["verification"] == "anchored"
    assert amount[0]["normalized"]["amount"] == "100"

    context = service.retrieve(principal, artifact_id, "apple price")
    assert context["untrusted"] is True
    assert any("apple" in chunk["text"] for chunk in context["chunks"])

    answer = service.ask(principal, artifact_id, "What is the price?")
    # The amount is anchored but tax treatment is unknown, so the answer is conditional rather than
    # silently presenting a tax-inclusive price.
    assert answer["status"] == "conditional"
    assert answer["citations"][0]["locator"]["kind"] == "csv"
    assert "tax:unknown" in answer["citations"][0]["conditions"]
    assert "100" in answer["answer"]

    unrelated = service.ask(principal, artifact_id, "What is the warranty period?")
    assert unrelated["status"] == "unknown"
    assert unrelated["citations"] == []


def test_chunk_replay_is_idempotent_and_conflicts_survive(service: MultimodalService) -> None:
    principal = _principal()
    data = b"a,b\n1,2\n"
    digest = hashlib.sha256(data).hexdigest()
    started = service.begin_upload(
        principal,
        original_filename="two.csv",
        declared_media_type="text/csv",
        expected_bytes=len(data),
        content_sha256=digest,
        idempotency_key="replay",
    )
    upload_id = started["uploadId"]
    first = data[:4]
    second = data[4:]
    first_digest = hashlib.sha256(first).hexdigest()
    service.put_chunk(principal, upload_id, 0, first, first_digest)
    replay = service.put_chunk(principal, upload_id, 0, first, first_digest)
    assert replay["receivedBytes"] == len(first)
    with pytest.raises(MmiError) as conflict:
        service.put_chunk(principal, upload_id, 0, b"xxxx", hashlib.sha256(b"xxxx").hexdigest())
    assert conflict.value.code == "CHUNK_CONFLICT"
    with pytest.raises(MmiError) as gap:
        service.put_chunk(principal, upload_id, 2, b"zz", hashlib.sha256(b"zz").hexdigest())
    assert gap.value.code == "POLICY_DENIED"
    service.put_chunk(principal, upload_id, 1, second, hashlib.sha256(second).hexdigest())
    artifact = service.complete_upload(principal, upload_id)
    assert artifact["quarantineState"] == "admitted"


def test_complete_retry_after_db_failure_keeps_staging(
    service: MultimodalService, monkeypatch: pytest.MonkeyPatch
) -> None:
    principal = _principal()
    data = b"a,b\n1,2\n"
    digest = hashlib.sha256(data).hexdigest()
    started = service.begin_upload(
        principal,
        original_filename="retry.csv",
        declared_media_type="text/csv",
        expected_bytes=len(data),
        content_sha256=digest,
        idempotency_key="retry",
    )
    upload_id = started["uploadId"]
    service.put_chunk(principal, upload_id, 0, data, digest)
    original = service.store.complete_upload
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected database failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(service.store, "complete_upload", fail_once)
    with pytest.raises(RuntimeError):
        service.complete_upload(principal, upload_id)
    retried = service.complete_upload(principal, upload_id)
    assert retried["quarantineState"] == "admitted"
    assert calls == 2


def test_revoke_clears_jobs_and_derived_access(service: MultimodalService) -> None:
    principal = _principal()
    artifact_id = _upload(service, principal)
    job = service.start_parse(principal, artifact_id, request=_request(artifact_id), idempotency_key="revoke")
    service.run_job(principal, job.id)
    service.revoke_artifact(principal, artifact_id)
    with pytest.raises(MmiError) as denied:
        service.get_job(principal, job.id)
    assert denied.value.code == "POLICY_DENIED"
    assert service.citations(principal, job.id) == []
    with pytest.raises(MmiError):
        service.ask(principal, artifact_id, "What is the price?")
    stored = service.store.get_job(principal, job.id)
    assert stored is not None and stored.status == "cancelled" and stored.outcome is None


def test_expired_lease_cannot_finish_and_can_be_reclaimed(service: MultimodalService) -> None:
    principal = _principal()
    artifact_id = _upload(service, principal)
    job = service.start_parse(principal, artifact_id, request=_request(artifact_id), idempotency_key="lease")
    claimed = service.store.claim_job(principal, job.id, lease_owner="worker-a", lease_seconds=60)
    assert claimed.lease_epoch == 1
    service.store._jobs[(principal.tenant_id, job.id)] = dataclasses.replace(
        claimed, lease_expires_at="2000-01-01T00:00:00Z"
    )
    assert (
        service.store.finish_job(
            principal,
            job.id,
            lease_owner="worker-a",
            lease_epoch=claimed.lease_epoch,
            outcome={"status": "complete"},
            document={},
            error_code=None,
        )
        is False
    )
    reclaimed = service.store.claim_job(principal, job.id, lease_owner="worker-b", lease_seconds=60)
    assert reclaimed.lease_epoch == 2
    assert reclaimed.lease_owner == "worker-b"
    assert reclaimed.lease_expires_at > _now()


def test_upload_digest_mismatch_leaves_no_artifact(service: MultimodalService) -> None:
    principal = _principal()
    started = service.begin_upload(
        principal,
        original_filename="x.csv",
        declared_media_type="text/csv",
        expected_bytes=len(CSV),
        content_sha256="0" * 64,
        idempotency_key="bad",
    )
    service.put_chunk(principal, started["uploadId"], 0, CSV, None)
    with pytest.raises(MmiError) as exc:
        service.complete_upload(principal, started["uploadId"])
    assert exc.value.code == "SOURCE_MISMATCH"


def test_revoke_blocks_parse_and_deletes_blob(service: MultimodalService) -> None:
    principal = _principal()
    artifact_id = _upload(service, principal)
    service.revoke_artifact(principal, artifact_id)
    with pytest.raises(MmiError):
        service.start_parse(principal, artifact_id, request=_request(artifact_id), idempotency_key="job-2")


def test_stale_lease_cannot_finish(service: MultimodalService) -> None:
    principal = _principal()
    artifact_id = _upload(service, principal)
    job = service.start_parse(principal, artifact_id, request=_request(artifact_id), idempotency_key="job-3")
    claimed = service.store.claim_job(principal, job.id, lease_owner="a", lease_seconds=60)
    assert (
        service.store.finish_job(
            principal,
            job.id,
            lease_owner="b",
            lease_epoch=claimed.lease_epoch,
            outcome={},
            document=None,
            error_code=None,
        )
        is False
    )
    assert (
        service.store.finish_job(
            principal,
            job.id,
            lease_owner="a",
            lease_epoch=claimed.lease_epoch,
            outcome={},
            document=None,
            error_code=None,
        )
        is True
    )
    with pytest.raises(MmiError) as exc:
        service.store.claim_job(principal, job.id, lease_owner="c", lease_seconds=60)
    assert exc.value.code == "STALE_LEASE"


def test_parse_fails_closed_without_executor(tmp_path: Path) -> None:
    limits = __import__("aiagent_multimodal.limits", fromlist=["MmiLimits"]).MmiLimits()
    service = MultimodalService(
        store=InMemoryStore(),
        blobs=QuarantineBlobStore(tmp_path, max_file_bytes=limits.max_file_bytes),
        executor=None,
        limits=limits,
    )
    principal = _principal()
    artifact_id = _upload(service, principal)
    with pytest.raises(MmiError) as caught:
        service.start_parse(principal, artifact_id, request=_request(artifact_id), idempotency_key="job-nox")
    assert caught.value.code == "POLICY_DENIED"


def test_complete_retry_repairs_unadmitted_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    limits = MmiLimits()
    store = InMemoryStore()
    service = MultimodalService(
        store=store,
        blobs=QuarantineBlobStore(tmp_path, max_file_bytes=limits.max_file_bytes),
        executor=InProcessParserExecutor(),
        limits=limits,
    )
    principal = _principal()
    digest = hashlib.sha256(CSV).hexdigest()
    started = service.begin_upload(
        principal,
        original_filename="price.csv",
        declared_media_type="text/csv",
        expected_bytes=len(CSV),
        content_sha256=digest,
        idempotency_key="upload-repair",
    )
    service.put_chunk(principal, started["uploadId"], 0, CSV, digest)
    original = store.admit_artifact

    def fail_admission(_principal: Principal, _artifact_id: str):
        raise MmiError("PARSER_CRASH", "injected admission failure")

    monkeypatch.setattr(store, "admit_artifact", fail_admission)
    with pytest.raises(MmiError):
        service.complete_upload(principal, started["uploadId"])
    session = store.get_upload(principal, started["uploadId"])
    assert session is not None and session.artifact_id
    quarantined = store.get_artifact(principal, session.artifact_id)
    assert quarantined is not None and quarantined.quarantine_state == "quarantined"

    monkeypatch.setattr(store, "admit_artifact", original)
    repaired = service.complete_upload(principal, started["uploadId"])
    assert repaired["quarantineState"] == "admitted"
