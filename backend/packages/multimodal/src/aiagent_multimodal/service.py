"""Application service for upload, quarantine, parse jobs and grounded retrieval."""

from __future__ import annotations

import hashlib
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .blobstore import QuarantineBlobStore
from .context import grounded_answer, retrieve
from .contracts import MmiCandidate, MmiRequestV1, canonical_parse_key
from .detection import Detection
from .errors import MmiError
from .ids import uuid7
from .limits import MmiLimits
from .parser_registry import parser_build_digest
from .parsers.base import Document
from .pipeline import ParserExecutor, Pipeline, citations_from_outcome
from .store import ArtifactRecord, JobRecord, Principal, Store, UploadSession

_SHA = re.compile(r"^[0-9a-f]{64}$")
_CHUNK_MAX = 1 << 20


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _plus(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _deadline_elapsed(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        return True
    return parsed <= datetime.now(UTC)


def safe_display_name(raw: str) -> str:
    name = unicodedata.normalize("NFKC", raw or "upload").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\x00\r\n")
    name = name.strip().strip(".") or "upload"
    return name[:255]


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    upload_ttl_seconds: int = 3600
    artifact_ttl_seconds: int = 7 * 24 * 3600
    job_ttl_seconds: int = 24 * 3600
    lease_seconds: int = 120
    chunk_bytes: int = 1 << 20


class MultimodalService:
    def __init__(
        self,
        *,
        store: Store,
        blobs: QuarantineBlobStore,
        executor: ParserExecutor | None,
        limits: MmiLimits | None = None,
        config: ServiceConfig | None = None,
        isolation_note: str = "",
    ) -> None:
        self.store = store
        self.blobs = blobs
        self.limits = limits or MmiLimits()
        self.config = config or ServiceConfig()
        self.executor = executor
        self.pipeline = Pipeline(self.limits, executor) if executor is not None else None
        self.isolation_note = isolation_note
        self._complete_lock = threading.Lock()

    # ------------------------------------------------------------------ upload
    def begin_upload(
        self,
        principal: Principal,
        *,
        original_filename: str,
        declared_media_type: str,
        expected_bytes: int,
        content_sha256: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if expected_bytes < 1 or expected_bytes > self.limits.max_file_bytes:
            raise MmiError("MAX_INPUT", "declared size is outside the allowed range")
        if not _SHA.fullmatch(content_sha256):
            raise MmiError("MAX_INPUT", "contentSha256 must be lowercase hex SHA-256")
        if not idempotency_key or len(idempotency_key) > 256:
            raise MmiError("POLICY_DENIED", "idempotency key is required")
        session = self.store.create_upload(
            principal,
            upload_id=uuid7(),
            original_filename=safe_display_name(original_filename),
            declared_media_type=declared_media_type[:128] or "application/octet-stream",
            expected_bytes=expected_bytes,
            content_sha256=content_sha256,
            idempotency_key=idempotency_key,
            expires_at=_plus(self.config.upload_ttl_seconds),
        )
        return {
            "uploadId": session.id,
            "chunkBytes": self.config.chunk_bytes,
            "expectedBytes": session.expected_bytes,
            "receivedBytes": session.received_bytes,
            "chunkCount": session.chunk_count,
            "state": session.state,
            "expiresAt": session.expires_at,
        }

    def put_chunk(
        self,
        principal: Principal,
        upload_id: str,
        index: int,
        data: bytes,
        claimed_sha256: str | None,
    ) -> dict[str, Any]:
        session = self.store.get_upload(principal, upload_id)
        if session is None:
            raise MmiError("POLICY_DENIED", "upload not found")
        if session.state != "open" or session.expires_at < _now():
            raise MmiError("POLICY_DENIED", "upload is not open")
        if not data or len(data) > min(self.config.chunk_bytes, _CHUNK_MAX):
            raise MmiError("MAX_INPUT", "chunk size is outside the allowed range")
        digest = hashlib.sha256(data).hexdigest()
        if claimed_sha256 is not None and claimed_sha256 != digest:
            raise MmiError("SOURCE_MISMATCH", "chunk digest mismatch")
        existing = self.store.get_chunk(principal, upload_id, index)
        if existing is not None and existing != (len(data), digest):
            raise MmiError("CHUNK_CONFLICT", "chunk replay differs from the accepted chunk")
        if existing is None and session.received_bytes + len(data) > session.expected_bytes:
            raise MmiError("MAX_INPUT", "upload exceeds declared size")
        self.blobs.put_chunk(principal.tenant_id, upload_id, index, data)
        # A retry is idempotent in the store. Never abort the whole staging directory because one
        # request failed: a lost ACK must remain recoverable.
        try:
            updated = self.store.append_chunk(
                principal, upload_id=upload_id, index=index, byte_length=len(data), chunk_sha256=digest
            )
        except Exception:
            # FS-before-DB is intentional: a DB row must never claim bytes that are not durable.
            # If the DB did not accept this chunk, remove only the exact bytes we published so a
            # later request with different bytes is not permanently blocked by an orphan. Re-check
            # the DB first because a concurrent identical writer may have committed the same bytes.
            if self.store.get_chunk(principal, upload_id, index) is None:
                self.blobs.delete_chunk(principal.tenant_id, upload_id, index, digest)
            raise
        return {
            "uploadId": upload_id,
            "receivedBytes": updated.received_bytes,
            "chunkCount": updated.chunk_count,
            "state": updated.state,
        }

    def complete_upload(self, principal: Principal, upload_id: str) -> dict[str, Any]:
        with self._complete_lock:
            return self._complete_upload_locked(principal, upload_id)

    def _complete_upload_locked(self, principal: Principal, upload_id: str) -> dict[str, Any]:
        session = self.store.get_upload(principal, upload_id)
        if session is None:
            raise MmiError("POLICY_DENIED", "upload not found")
        if session.state == "completed" and session.artifact_id:
            artifact = self.store.get_artifact(principal, session.artifact_id)
            if artifact is None:
                raise MmiError("POLICY_DENIED", "artifact not found")
            if artifact.quarantine_state == "quarantined":
                # Complete committed but admission did not. Re-running complete repairs it instead of
                # returning a quarantined artifact that no parse job can use.
                artifact = self.store.admit_artifact(principal, artifact.id)
            return _artifact_projection(artifact)
        if session.state != "open" or session.expires_at < _now():
            raise MmiError("POLICY_DENIED", "upload is not open")
        if session.received_bytes != session.expected_bytes:
            raise MmiError("MAX_INPUT", "upload is incomplete")
        artifact_id = uuid7()
        committed = False
        stored = self.blobs.finalize(
            principal.tenant_id,
            upload_id,
            artifact_id,
            expected_sha256=session.content_sha256,
            expected_bytes=session.expected_bytes,
            chunk_count=session.chunk_count,
        )
        try:
            raw = self.blobs.read(stored.storage_key, stored.sha256, stored.byte_length)
            detection = (
                self.pipeline.detect(
                    raw,
                    file_name=session.original_filename,
                    declared_mime=session.declared_media_type,
                )
                if self.pipeline is not None
                else _detect(raw, session, self.limits)
            )
            artifact = self.store.complete_upload(
                principal,
                upload_id=upload_id,
                artifact_id=artifact_id,
                detected_format=detection.format,
                storage_key=stored.storage_key,
                content_sha256=stored.sha256,
                byte_length=stored.byte_length,
                expires_at=_plus(self.config.artifact_ttl_seconds),
            )
            committed = True
            # The artifact row now owns the final blob. Staging is no longer needed, but its
            # cleanup must happen only after this commit; finalize() deliberately retained it.
            self.blobs.cleanup_upload(principal.tenant_id, upload_id)
            artifact = self.store.admit_artifact(principal, artifact.id)
            return _artifact_projection(artifact)
        except Exception:
            # Before commit, keep staging so the same upload can be finalized again. The final
            # blob (if rename succeeded) is an uncommitted orphan and can be dropped safely.
            if not committed:
                self.blobs.delete(stored.storage_key)
            raise

    def get_artifact(self, principal: Principal, artifact_id: str) -> dict[str, Any]:
        artifact = self.store.get_artifact(principal, artifact_id)
        if artifact is None or artifact.quarantine_state != "admitted":
            raise MmiError("POLICY_DENIED", "artifact not found")
        if artifact.expires_at < _now():
            raise MmiError("POLICY_DENIED", "artifact expired")
        return _artifact_projection(artifact)

    def revoke_artifact(self, principal: Principal, artifact_id: str) -> dict[str, Any]:
        artifact = self.store.revoke_artifact(principal, artifact_id)
        self.blobs.delete(artifact.storage_key)
        return _artifact_projection(artifact)

    # --------------------------------------------------------------- parse jobs
    def start_parse(
        self,
        principal: Principal,
        artifact_id: str,
        *,
        request: MmiRequestV1,
        idempotency_key: str,
    ) -> JobRecord:
        if self.pipeline is None:
            raise MmiError(
                "POLICY_DENIED",
                "isolated parser executor is not configured; in-process fallback is disabled",
            )
        artifact = self.store.get_artifact(principal, artifact_id)
        if artifact is None or artifact.quarantine_state != "admitted":
            raise MmiError("POLICY_DENIED", "artifact not found")
        if artifact.expires_at < _now():
            raise MmiError("POLICY_DENIED", "artifact expired")
        if request.source_artifact_id != artifact.id:
            raise MmiError("SOURCE_MISMATCH", "request source does not match artifact")
        parse_key = canonical_parse_key(
            tenant_id=principal.tenant_id,
            owner_id=principal.owner_user_id,
            source_artifact_id=artifact.id,
            source_sha256=artifact.content_sha256,
            request=request,
            policy_revision="mmi/1",
            parser_build_digest=parser_build_digest(),
            visibility="private",
        )
        request_json = request.to_dict()
        request_sha = hashlib.sha256(
            __import__("json").dumps(request_json, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return self.store.create_job(
            principal,
            job_id=uuid7(),
            artifact_id=artifact.id,
            parse_key=parse_key,
            request_sha256=request_sha,
            request=request_json,
            expires_at=_plus(self.config.job_ttl_seconds),
        )

    def run_job(self, principal: Principal, job_id: str, *, lease_owner: str | None = None) -> JobRecord:
        if self.pipeline is None:
            raise MmiError("POLICY_DENIED", "isolated parser executor is unavailable")
        owner = lease_owner or f"inprocess:{uuid7()}"
        job = self.store.claim_job(principal, job_id, lease_owner=owner, lease_seconds=self.config.lease_seconds)
        artifact = self.store.get_artifact(principal, job.artifact_id)
        if artifact is None or artifact.quarantine_state != "admitted" or artifact.expires_at <= _now():
            self.store.finish_job(
                principal,
                job.id,
                lease_owner=owner,
                lease_epoch=job.lease_epoch,
                outcome=None,
                document=None,
                error_code="OWNER_REVOKED",
            )
            raise MmiError("OWNER_REVOKED", "artifact is no longer available")
        try:
            raw = self.blobs.read(artifact.storage_key, artifact.content_sha256, artifact.byte_length)
            request = MmiRequestV1.from_dict(job.request)
            if _deadline_elapsed(request.deadline_at):
                raise MmiError("PARSE_TIMEOUT", "request deadline elapsed before parsing")
            detection = self.pipeline.detect(
                raw,
                file_name=artifact.original_filename,
                declared_mime=artifact.declared_media_type,
            )
            if detection.format != artifact.detected_format:
                raise MmiError("MIME_MISMATCH", "format changed since quarantine admission")
            deadline = time.monotonic() + min(self.limits.wall_clock_seconds, self.config.lease_seconds)
            result = self.pipeline.process(
                source_artifact_id=artifact.id,
                source_sha256=artifact.content_sha256,
                raw=raw,
                request=request,
                detection=detection,
                run_ocr=request.allow_local_ocr,
                deadline_monotonic=deadline,
            )
            ok = self.store.finish_job(
                principal,
                job.id,
                lease_owner=owner,
                lease_epoch=job.lease_epoch,
                outcome=result.outcome,
                document=result.document.to_dict(),
                error_code=None,
            )
            if not ok:
                raise MmiError("STALE_LEASE", "lease was lost before commit")
            finished = self.store.get_job(principal, job.id)
            if finished is None:
                raise MmiError("STALE_LEASE", "job disappeared")
            return finished
        except MmiError as exc:
            self.store.finish_job(
                principal,
                job.id,
                lease_owner=owner,
                lease_epoch=job.lease_epoch,
                outcome=None,
                document=None,
                error_code=exc.code,
            )
            raise
        except Exception as exc:
            self.store.finish_job(
                principal,
                job.id,
                lease_owner=owner,
                lease_epoch=job.lease_epoch,
                outcome=None,
                document=None,
                error_code="PARSER_CRASH",
            )
            raise MmiError("PARSER_CRASH", f"parser fault: {type(exc).__name__}") from exc

    def get_job(self, principal: Principal, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(principal, job_id)
        if job is None:
            raise MmiError("POLICY_DENIED", "job not found")
        if job.expires_at <= _now():
            raise MmiError("POLICY_DENIED", "job expired")
        artifact = self.store.get_artifact(principal, job.artifact_id)
        if artifact is None or artifact.quarantine_state == "revoked" or artifact.expires_at <= _now():
            # Do not expose an outcome or citations derived from a revoked/expired source.
            raise MmiError("POLICY_DENIED", "job not found")
        return _job_projection(job, artifact)

    def cancel_job(self, principal: Principal, job_id: str) -> dict[str, Any]:
        job = self.store.cancel_job(principal, job_id)
        artifact = self.store.get_artifact(principal, job.artifact_id)
        return _job_projection(job, artifact)

    # ---------------------------------------------------------------- retrieval
    def retrieve(self, principal: Principal, artifact_id: str, query: str, *, limit: int = 8) -> dict[str, Any]:
        job, document = self._succeeded_document(principal, artifact_id)
        chunks = retrieve(document, query, limit=limit)
        return {
            "artifactId": artifact_id,
            "jobId": job.id,
            "query": query,
            "chunks": [chunk.to_dict() for chunk in chunks],
            "untrusted": True,
        }

    def ask(self, principal: Principal, artifact_id: str, question: str) -> dict[str, Any]:
        job, document = self._succeeded_document(principal, artifact_id)
        outcome = job.outcome or {}
        candidates = [MmiCandidate.from_dict(c) for c in outcome.get("candidates", [])]
        answer = grounded_answer(question, candidates, document=document)
        answer["artifactId"] = artifact_id
        answer["jobId"] = job.id
        answer["contextChunks"] = [chunk.to_dict() for chunk in retrieve(document, question, limit=4)]
        return answer

    def _succeeded_document(self, principal: Principal, artifact_id: str) -> tuple[JobRecord, Document]:
        artifact = self.store.get_artifact(principal, artifact_id)
        if artifact is None or artifact.quarantine_state != "admitted" or artifact.expires_at <= _now():
            raise MmiError("POLICY_DENIED", "artifact not found")
        job = self.store.latest_job(principal, artifact_id)
        if (
            job is None
            or job.status != "succeeded"
            or job.document is None
            or job.outcome is None
            or job.expires_at <= _now()
        ):
            raise MmiError("POLICY_DENIED", "no succeeded parse job for artifact")
        try:
            document = Document.from_dict(job.document)
        except (KeyError, TypeError, ValueError) as exc:
            raise MmiError("PARSER_CRASH", "stored document projection is invalid") from exc
        return job, document

    def citations(self, principal: Principal, job_id: str) -> list[dict[str, Any]]:
        job = self.store.get_job(principal, job_id)
        if job is None or job.outcome is None or job.expires_at <= _now():
            return []
        artifact = self.store.get_artifact(principal, job.artifact_id)
        if artifact is None or artifact.quarantine_state != "admitted" or artifact.expires_at <= _now():
            return []
        return citations_from_outcome(
            job.outcome, fmt=artifact.detected_format, artifact_id=artifact.id, sha=artifact.content_sha256
        )


def _detect(raw: bytes, session: UploadSession, limits: MmiLimits) -> Detection:
    from .detection import DeclaredInput, detect

    return detect(
        raw,
        DeclaredInput(file_name=session.original_filename, declared_mime=session.declared_media_type),
        limits,
    )


def _artifact_projection(artifact: ArtifactRecord) -> dict[str, Any]:
    return {
        "artifactId": artifact.id,
        "displayName": artifact.original_filename,
        "declaredMediaType": artifact.declared_media_type,
        "format": artifact.detected_format,
        "sha256": artifact.content_sha256,
        "byteLength": artifact.byte_length,
        "quarantineState": artifact.quarantine_state,
        "createdAt": artifact.created_at,
        "expiresAt": artifact.expires_at,
        "revokedAt": artifact.revoked_at,
    }


def _job_projection(job: JobRecord, artifact: ArtifactRecord | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "jobId": job.id,
        "artifactId": job.artifact_id,
        "status": job.status,
        "attempts": job.attempts,
        "errorCode": job.error_code,
        "createdAt": job.created_at,
        "updatedAt": job.updated_at,
        "leaseEpoch": job.lease_epoch,
    }
    if (
        job.outcome is not None
        and artifact is not None
        and artifact.quarantine_state == "admitted"
        and artifact.expires_at > _now()
        and job.expires_at > _now()
    ):
        payload["outcome"] = job.outcome
        payload["citations"] = citations_from_outcome(
            job.outcome, fmt=artifact.detected_format, artifact_id=artifact.id, sha=artifact.content_sha256
        )
    return payload
