"""Upload, artifact and parse-job metadata stores.

Two implementations share one Protocol: an in-memory store for unit tests and
local development, and a PostgreSQL store used by the API. Every method takes
the authenticated principal and applies the owner predicate in the store, not
only in the route. Lease completion is a CAS on (lease_owner, lease_epoch), so
a stale worker cannot overwrite a newer attempt.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from .errors import MmiError


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: str
    owner_user_id: str
    workspace_id: str


@dataclass(frozen=True, slots=True)
class UploadSession:
    id: str
    principal: Principal
    original_filename: str
    declared_media_type: str
    expected_bytes: int
    content_sha256: str
    idempotency_key: str
    received_bytes: int
    chunk_count: int
    state: str
    artifact_id: str | None
    created_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: str
    principal: Principal
    original_filename: str
    declared_media_type: str
    detected_format: str
    content_sha256: str
    byte_length: int
    storage_key: str
    quarantine_state: str
    created_at: str
    expires_at: str
    revoked_at: str | None


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    principal: Principal
    artifact_id: str
    parse_key: str
    request_sha256: str
    request: dict[str, Any]
    status: str
    lease_owner: str | None
    lease_epoch: int
    lease_expires_at: str | None
    outcome: dict[str, Any] | None
    document: dict[str, Any] | None
    error_code: str | None
    attempts: int
    created_at: str
    updated_at: str


class Store(Protocol):
    def create_upload(
        self,
        principal: Principal,
        *,
        upload_id: str,
        original_filename: str,
        declared_media_type: str,
        expected_bytes: int,
        content_sha256: str,
        idempotency_key: str,
        expires_at: str,
    ) -> UploadSession: ...

    def get_upload(self, principal: Principal, upload_id: str) -> UploadSession | None: ...

    def append_chunk(
        self, principal: Principal, *, upload_id: str, index: int, byte_length: int, chunk_sha256: str
    ) -> UploadSession: ...

    def complete_upload(
        self,
        principal: Principal,
        *,
        upload_id: str,
        artifact_id: str,
        detected_format: str,
        storage_key: str,
        content_sha256: str,
        byte_length: int,
        expires_at: str,
    ) -> ArtifactRecord: ...

    def admit_artifact(self, principal: Principal, artifact_id: str) -> ArtifactRecord: ...

    def get_artifact(self, principal: Principal, artifact_id: str) -> ArtifactRecord | None: ...

    def revoke_artifact(self, principal: Principal, artifact_id: str) -> ArtifactRecord: ...

    def create_job(
        self,
        principal: Principal,
        *,
        job_id: str,
        artifact_id: str,
        parse_key: str,
        request_sha256: str,
        request: dict[str, Any],
        expires_at: str,
    ) -> JobRecord: ...

    def get_job(self, principal: Principal, job_id: str) -> JobRecord | None: ...

    def latest_job(self, principal: Principal, artifact_id: str) -> JobRecord | None: ...

    def claim_job(self, principal: Principal, job_id: str, *, lease_owner: str, lease_seconds: int) -> JobRecord: ...

    def finish_job(
        self,
        principal: Principal,
        job_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
        outcome: dict[str, Any] | None,
        document: dict[str, Any] | None,
        error_code: str | None,
    ) -> bool: ...

    def cancel_job(self, principal: Principal, job_id: str) -> JobRecord: ...


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _plus(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return str(value.astimezone(UTC).isoformat().replace("+00:00", "Z"))
    return str(value)


class InMemoryStore:
    """Thread-safe test store. Not durable; the API never uses it in production."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._uploads: dict[tuple[str, str], UploadSession] = {}
        self._chunks: dict[tuple[str, str], list[tuple[int, int, str]]] = {}
        self._artifacts: dict[tuple[str, str], ArtifactRecord] = {}
        self._jobs: dict[tuple[str, str], JobRecord] = {}

    def create_upload(
        self,
        principal: Principal,
        *,
        upload_id: str,
        original_filename: str,
        declared_media_type: str,
        expected_bytes: int,
        content_sha256: str,
        idempotency_key: str,
        expires_at: str,
    ) -> UploadSession:
        with self._lock:
            for (tenant, _), existing in self._uploads.items():
                if (
                    tenant == principal.tenant_id
                    and existing.principal.owner_user_id == principal.owner_user_id
                    and existing.idempotency_key == idempotency_key
                ):
                    if (
                        existing.expected_bytes != expected_bytes
                        or existing.content_sha256 != content_sha256
                        or existing.original_filename != original_filename
                    ):
                        raise MmiError("POLICY_DENIED", "idempotency key was reused with different input")
                    return existing
            session = UploadSession(
                upload_id,
                principal,
                original_filename,
                declared_media_type,
                expected_bytes,
                content_sha256,
                idempotency_key,
                0,
                0,
                "open",
                None,
                _now(),
                expires_at,
            )
            self._uploads[(principal.tenant_id, upload_id)] = session
            self._chunks[(principal.tenant_id, upload_id)] = []
            return session

    def get_upload(self, principal: Principal, upload_id: str) -> UploadSession | None:
        with self._lock:
            item = self._uploads.get((principal.tenant_id, upload_id))
            return item if item and item.principal.owner_user_id == principal.owner_user_id else None

    def append_chunk(
        self, principal: Principal, *, upload_id: str, index: int, byte_length: int, chunk_sha256: str
    ) -> UploadSession:
        with self._lock:
            item = self.get_upload(principal, upload_id)
            if item is None or item.state != "open":
                raise MmiError("POLICY_DENIED", "upload is not open")
            chunks = self._chunks[(principal.tenant_id, upload_id)]
            if index != len(chunks):
                raise MmiError("POLICY_DENIED", "chunks must be sent in order")
            if index > 65535 or item.received_bytes + byte_length > item.expected_bytes:
                raise MmiError("MAX_INPUT", "upload exceeds declared size")
            chunks.append((index, byte_length, chunk_sha256))
            updated = UploadSession(
                item.id,
                item.principal,
                item.original_filename,
                item.declared_media_type,
                item.expected_bytes,
                item.content_sha256,
                item.idempotency_key,
                item.received_bytes + byte_length,
                len(chunks),
                item.state,
                item.artifact_id,
                item.created_at,
                item.expires_at,
            )
            self._uploads[(principal.tenant_id, upload_id)] = updated
            return updated

    def complete_upload(
        self,
        principal: Principal,
        *,
        upload_id: str,
        artifact_id: str,
        detected_format: str,
        storage_key: str,
        content_sha256: str,
        byte_length: int,
        expires_at: str,
    ) -> ArtifactRecord:
        with self._lock:
            item = self.get_upload(principal, upload_id)
            if item is None:
                raise MmiError("POLICY_DENIED", "upload not found")
            if item.state == "completed" and item.artifact_id:
                existing = self._artifacts.get((principal.tenant_id, item.artifact_id))
                if existing is None:
                    raise MmiError("POLICY_DENIED", "artifact not found")
                return existing
            if item.state != "open":
                raise MmiError("POLICY_DENIED", "upload is not open")
            if item.received_bytes != item.expected_bytes or content_sha256 != item.content_sha256:
                raise MmiError("SOURCE_MISMATCH", "upload digest or length mismatch")
            artifact = ArtifactRecord(
                artifact_id,
                principal,
                item.original_filename,
                item.declared_media_type,
                detected_format,
                content_sha256,
                byte_length,
                storage_key,
                "quarantined",
                _now(),
                expires_at,
                None,
            )
            self._artifacts[(principal.tenant_id, artifact_id)] = artifact
            completed = UploadSession(
                item.id,
                item.principal,
                item.original_filename,
                item.declared_media_type,
                item.expected_bytes,
                item.content_sha256,
                item.idempotency_key,
                item.received_bytes,
                item.chunk_count,
                "completed",
                artifact_id,
                item.created_at,
                item.expires_at,
            )
            self._uploads[(principal.tenant_id, upload_id)] = completed
            return artifact

    def admit_artifact(self, principal: Principal, artifact_id: str) -> ArtifactRecord:
        with self._lock:
            item = self.get_artifact(principal, artifact_id)
            if item is None:
                raise MmiError("POLICY_DENIED", "artifact not found")
            if item.quarantine_state == "revoked":
                raise MmiError("OWNER_REVOKED", "artifact revoked")
            updated = ArtifactRecord(
                item.id,
                item.principal,
                item.original_filename,
                item.declared_media_type,
                item.detected_format,
                item.content_sha256,
                item.byte_length,
                item.storage_key,
                "admitted",
                item.created_at,
                item.expires_at,
                item.revoked_at,
            )
            self._artifacts[(principal.tenant_id, artifact_id)] = updated
            return updated

    def get_artifact(self, principal: Principal, artifact_id: str) -> ArtifactRecord | None:
        with self._lock:
            item = self._artifacts.get((principal.tenant_id, artifact_id))
            return item if item and item.principal.owner_user_id == principal.owner_user_id else None

    def revoke_artifact(self, principal: Principal, artifact_id: str) -> ArtifactRecord:
        with self._lock:
            item = self.get_artifact(principal, artifact_id)
            if item is None:
                raise MmiError("POLICY_DENIED", "artifact not found")
            updated = ArtifactRecord(
                item.id,
                item.principal,
                item.original_filename,
                item.declared_media_type,
                item.detected_format,
                item.content_sha256,
                item.byte_length,
                item.storage_key,
                "revoked",
                item.created_at,
                item.expires_at,
                _now(),
            )
            self._artifacts[(principal.tenant_id, artifact_id)] = updated
            return updated

    def create_job(
        self,
        principal: Principal,
        *,
        job_id: str,
        artifact_id: str,
        parse_key: str,
        request_sha256: str,
        request: dict[str, Any],
        expires_at: str,
    ) -> JobRecord:
        with self._lock:
            for (tenant, _), existing in self._jobs.items():
                if (
                    tenant == principal.tenant_id
                    and existing.principal.owner_user_id == principal.owner_user_id
                    and existing.parse_key == parse_key
                ):
                    return existing
            artifact = self.get_artifact(principal, artifact_id)
            if artifact is None or artifact.quarantine_state != "admitted":
                raise MmiError("QUARANTINED", "artifact is not admitted")
            job = JobRecord(
                job_id,
                principal,
                artifact_id,
                parse_key,
                request_sha256,
                request,
                "queued",
                None,
                0,
                None,
                None,
                None,
                None,
                0,
                _now(),
                _now(),
            )
            self._jobs[(principal.tenant_id, job_id)] = job
            return job

    def get_job(self, principal: Principal, job_id: str) -> JobRecord | None:
        with self._lock:
            item = self._jobs.get((principal.tenant_id, job_id))
            return item if item and item.principal.owner_user_id == principal.owner_user_id else None

    def latest_job(self, principal: Principal, artifact_id: str) -> JobRecord | None:
        with self._lock:
            matches = [
                item
                for item in self._jobs.values()
                if item.principal.tenant_id == principal.tenant_id
                and item.principal.owner_user_id == principal.owner_user_id
                and item.artifact_id == artifact_id
            ]
            return max(matches, key=lambda item: item.updated_at) if matches else None

    def claim_job(self, principal: Principal, job_id: str, *, lease_owner: str, lease_seconds: int) -> JobRecord:
        with self._lock:
            job = self.get_job(principal, job_id)
            if job is None:
                raise MmiError("POLICY_DENIED", "job not found")
            if job.status == "cancelled":
                raise MmiError("CANCELLED", "job cancelled")
            if job.status not in ("queued", "failed"):
                raise MmiError("STALE_LEASE", "job is not claimable")
            if job.attempts >= 3:
                raise MmiError("STALE_LEASE", "job attempt limit reached")
            claimed = JobRecord(
                job.id,
                job.principal,
                job.artifact_id,
                job.parse_key,
                job.request_sha256,
                job.request,
                "leased",
                lease_owner,
                job.lease_epoch + 1,
                _plus(lease_seconds),
                job.outcome,
                job.document,
                job.error_code,
                job.attempts + 1,
                job.created_at,
                _now(),
            )
            self._jobs[(principal.tenant_id, job_id)] = claimed
            return claimed

    def finish_job(
        self,
        principal: Principal,
        job_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
        outcome: dict[str, Any] | None,
        document: dict[str, Any] | None,
        error_code: str | None,
    ) -> bool:
        with self._lock:
            job = self.get_job(principal, job_id)
            if (
                job is None
                or job.status != "leased"
                or job.lease_owner != lease_owner
                or job.lease_epoch != lease_epoch
            ):
                return False
            status = "succeeded" if outcome is not None else "failed"
            finished = JobRecord(
                job.id,
                job.principal,
                job.artifact_id,
                job.parse_key,
                job.request_sha256,
                job.request,
                status,
                job.lease_owner,
                job.lease_epoch,
                job.lease_expires_at,
                outcome,
                document,
                error_code,
                job.attempts,
                job.created_at,
                _now(),
            )
            self._jobs[(principal.tenant_id, job_id)] = finished
            return True

    def cancel_job(self, principal: Principal, job_id: str) -> JobRecord:
        with self._lock:
            job = self.get_job(principal, job_id)
            if job is None:
                raise MmiError("POLICY_DENIED", "job not found")
            if job.status == "succeeded":
                return job
            cancelled = JobRecord(
                job.id,
                job.principal,
                job.artifact_id,
                job.parse_key,
                job.request_sha256,
                job.request,
                "cancelled",
                job.lease_owner,
                job.lease_epoch,
                job.lease_expires_at,
                None,
                None,
                "CANCELLED",
                job.attempts,
                job.created_at,
                _now(),
            )
            self._jobs[(principal.tenant_id, job_id)] = cancelled
            return cancelled


class PostgresStore:
    """PostgreSQL metadata store. Raw bytes stay in the quarantine blob store."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def _one(self, principal: Principal, sql: str, args: tuple[Any, ...]) -> dict[str, Any] | None:
        with self.db.tx(principal.tenant_id) as c:
            row = c.execute(sql, args).fetchone()
            return cast(dict[str, Any] | None, row)

    def create_upload(
        self,
        principal: Principal,
        *,
        upload_id: str,
        original_filename: str,
        declared_media_type: str,
        expected_bytes: int,
        content_sha256: str,
        idempotency_key: str,
        expires_at: str,
    ) -> UploadSession:
        with self.db.tx(principal.tenant_id) as c:
            row = c.execute(
                "INSERT INTO agent.mmi_upload_sessions(tenant_id,id,owner_user_id,workspace_id,"
                "original_filename,declared_media_type,expected_bytes,content_sha256,idempotency_key,"
                "expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,"
                "owner_user_id,idempotency_key) DO NOTHING RETURNING *, 0 AS chunk_count",
                (
                    principal.tenant_id,
                    upload_id,
                    principal.owner_user_id,
                    principal.workspace_id,
                    original_filename,
                    declared_media_type,
                    expected_bytes,
                    content_sha256,
                    idempotency_key,
                    expires_at,
                ),
            ).fetchone()
            if row is None:
                row = c.execute(
                    "SELECT s.*, (SELECT count(*) FROM agent.mmi_upload_chunks c"
                    " WHERE c.tenant_id=s.tenant_id AND c.upload_id=s.id) AS chunk_count"
                    " FROM agent.mmi_upload_sessions s WHERE s.tenant_id=%s AND s.owner_user_id=%s"
                    " AND s.idempotency_key=%s FOR UPDATE OF s",
                    (principal.tenant_id, principal.owner_user_id, idempotency_key),
                ).fetchone()
                if row is None:
                    raise MmiError("POLICY_DENIED", "upload not found")
                if (
                    row["expected_bytes"] != expected_bytes
                    or row["content_sha256"] != content_sha256
                    or row["original_filename"] != original_filename
                ):
                    raise MmiError("POLICY_DENIED", "idempotency key was reused with different input")
            return _upload(row)

    def get_upload(self, principal: Principal, upload_id: str) -> UploadSession | None:
        row = self._one(
            principal,
            "SELECT s.*, (SELECT count(*) FROM agent.mmi_upload_chunks c"
            " WHERE c.tenant_id=s.tenant_id AND c.upload_id=s.id) AS chunk_count"
            " FROM agent.mmi_upload_sessions s WHERE s.tenant_id=%s AND s.id=%s AND s.owner_user_id=%s",
            (principal.tenant_id, upload_id, principal.owner_user_id),
        )
        return _upload(row) if row else None

    def append_chunk(
        self, principal: Principal, *, upload_id: str, index: int, byte_length: int, chunk_sha256: str
    ) -> UploadSession:
        with self.db.tx(principal.tenant_id) as c:
            row = c.execute(
                "SELECT * FROM agent.mmi_upload_sessions WHERE tenant_id=%s AND id=%s AND owner_user_id=%s FOR UPDATE",
                (principal.tenant_id, upload_id, principal.owner_user_id),
            ).fetchone()
            if row is None or row["state"] != "open":
                raise MmiError("POLICY_DENIED", "upload is not open")
            count = c.execute(
                "SELECT count(*) AS n FROM agent.mmi_upload_chunks WHERE tenant_id=%s AND upload_id=%s",
                (principal.tenant_id, upload_id),
            ).fetchone()["n"]
            if index != count:
                raise MmiError("POLICY_DENIED", "chunks must be sent in order")
            if row["received_bytes"] + byte_length > row["expected_bytes"]:
                raise MmiError("MAX_INPUT", "upload exceeds declared size")
            c.execute(
                "INSERT INTO agent.mmi_upload_chunks(tenant_id,upload_id,chunk_index,byte_length,"
                "chunk_sha256) VALUES (%s,%s,%s,%s,%s)",
                (principal.tenant_id, upload_id, index, byte_length, chunk_sha256),
            )
            row = c.execute(
                "UPDATE agent.mmi_upload_sessions SET received_bytes=received_bytes+%s WHERE"
                " tenant_id=%s AND id=%s RETURNING *, %s AS chunk_count",
                (byte_length, principal.tenant_id, upload_id, count + 1),
            ).fetchone()
            return _upload(row)

    def complete_upload(
        self,
        principal: Principal,
        *,
        upload_id: str,
        artifact_id: str,
        detected_format: str,
        storage_key: str,
        content_sha256: str,
        byte_length: int,
        expires_at: str,
    ) -> ArtifactRecord:
        with self.db.tx(principal.tenant_id) as c:
            session = c.execute(
                "SELECT * FROM agent.mmi_upload_sessions WHERE tenant_id=%s AND id=%s AND owner_user_id=%s FOR UPDATE",
                (principal.tenant_id, upload_id, principal.owner_user_id),
            ).fetchone()
            if session is None:
                raise MmiError("POLICY_DENIED", "upload not found")
            if session["state"] == "completed" and session["artifact_id"]:
                row = c.execute(
                    "SELECT * FROM agent.mmi_artifacts WHERE tenant_id=%s AND id=%s",
                    (principal.tenant_id, session["artifact_id"]),
                ).fetchone()
                return _artifact(row)
            if session["state"] != "open":
                raise MmiError("POLICY_DENIED", "upload is not open")
            if session["received_bytes"] != session["expected_bytes"] or content_sha256 != session["content_sha256"]:
                raise MmiError("SOURCE_MISMATCH", "upload digest or length mismatch")
            c.execute(
                "INSERT INTO agent.mmi_artifacts(tenant_id,id,owner_user_id,workspace_id,"
                "original_filename,declared_media_type,detected_format,content_sha256,byte_length,"
                "storage_key,quarantine_state,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "'quarantined',%s)",
                (
                    principal.tenant_id,
                    artifact_id,
                    principal.owner_user_id,
                    principal.workspace_id,
                    session["original_filename"],
                    session["declared_media_type"],
                    detected_format,
                    content_sha256,
                    byte_length,
                    storage_key,
                    expires_at,
                ),
            )
            c.execute(
                "UPDATE agent.mmi_upload_sessions SET state='completed', artifact_id=%s WHERE tenant_id=%s AND id=%s",
                (artifact_id, principal.tenant_id, upload_id),
            )
            row = c.execute(
                "SELECT * FROM agent.mmi_artifacts WHERE tenant_id=%s AND id=%s",
                (principal.tenant_id, artifact_id),
            ).fetchone()
            return _artifact(row)

    def admit_artifact(self, principal: Principal, artifact_id: str) -> ArtifactRecord:
        with self.db.tx(principal.tenant_id) as c:
            row = c.execute(
                "UPDATE agent.mmi_artifacts SET quarantine_state='admitted' WHERE tenant_id=%s AND id=%s"
                " AND owner_user_id=%s AND quarantine_state='quarantined' RETURNING *",
                (principal.tenant_id, artifact_id, principal.owner_user_id),
            ).fetchone()
            if row is None:
                row = c.execute(
                    "SELECT * FROM agent.mmi_artifacts WHERE tenant_id=%s AND id=%s AND owner_user_id=%s",
                    (principal.tenant_id, artifact_id, principal.owner_user_id),
                ).fetchone()
                if row is None:
                    raise MmiError("POLICY_DENIED", "artifact not found")
                if row["quarantine_state"] == "revoked":
                    raise MmiError("OWNER_REVOKED", "artifact revoked")
            return _artifact(row)

    def get_artifact(self, principal: Principal, artifact_id: str) -> ArtifactRecord | None:
        row = self._one(
            principal,
            "SELECT * FROM agent.mmi_artifacts WHERE tenant_id=%s AND id=%s AND owner_user_id=%s",
            (principal.tenant_id, artifact_id, principal.owner_user_id),
        )
        return _artifact(row) if row else None

    def revoke_artifact(self, principal: Principal, artifact_id: str) -> ArtifactRecord:
        with self.db.tx(principal.tenant_id) as c:
            row = c.execute(
                "UPDATE agent.mmi_artifacts SET quarantine_state='revoked', revoked_at=now() WHERE"
                " tenant_id=%s AND id=%s AND owner_user_id=%s RETURNING *",
                (principal.tenant_id, artifact_id, principal.owner_user_id),
            ).fetchone()
            if row is None:
                raise MmiError("POLICY_DENIED", "artifact not found")
            return _artifact(row)

    def create_job(
        self,
        principal: Principal,
        *,
        job_id: str,
        artifact_id: str,
        parse_key: str,
        request_sha256: str,
        request: dict[str, Any],
        expires_at: str,
    ) -> JobRecord:
        from psycopg.types.json import Jsonb

        with self.db.tx(principal.tenant_id) as c:
            artifact = c.execute(
                "SELECT quarantine_state FROM agent.mmi_artifacts WHERE tenant_id=%s AND id=%s AND owner_user_id=%s",
                (principal.tenant_id, artifact_id, principal.owner_user_id),
            ).fetchone()
            if artifact is None or artifact["quarantine_state"] != "admitted":
                raise MmiError("QUARANTINED", "artifact is not admitted")
            row = c.execute(
                "INSERT INTO agent.mmi_parse_jobs(tenant_id,id,owner_user_id,workspace_id,artifact_id,"
                "parse_key,request_sha256,request_json,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (tenant_id,owner_user_id,parse_key) DO NOTHING RETURNING *",
                (
                    principal.tenant_id,
                    job_id,
                    principal.owner_user_id,
                    principal.workspace_id,
                    artifact_id,
                    parse_key,
                    request_sha256,
                    Jsonb(request),
                    expires_at,
                ),
            ).fetchone()
            if row is None:
                row = c.execute(
                    "SELECT * FROM agent.mmi_parse_jobs WHERE tenant_id=%s AND owner_user_id=%s AND parse_key=%s",
                    (principal.tenant_id, principal.owner_user_id, parse_key),
                ).fetchone()
                if row is None:
                    raise MmiError("POLICY_DENIED", "job not found")
            return _job(row)

    def get_job(self, principal: Principal, job_id: str) -> JobRecord | None:
        row = self._one(
            principal,
            "SELECT * FROM agent.mmi_parse_jobs WHERE tenant_id=%s AND id=%s AND owner_user_id=%s",
            (principal.tenant_id, job_id, principal.owner_user_id),
        )
        return _job(row) if row else None

    def latest_job(self, principal: Principal, artifact_id: str) -> JobRecord | None:
        row = self._one(
            principal,
            "SELECT * FROM agent.mmi_parse_jobs WHERE tenant_id=%s AND owner_user_id=%s"
            " AND artifact_id=%s ORDER BY updated_at DESC LIMIT 1",
            (principal.tenant_id, principal.owner_user_id, artifact_id),
        )
        return _job(row) if row else None

    def claim_job(self, principal: Principal, job_id: str, *, lease_owner: str, lease_seconds: int) -> JobRecord:
        with self.db.tx(principal.tenant_id) as c:
            row = c.execute(
                "UPDATE agent.mmi_parse_jobs SET status='leased', lease_owner=%s,"
                " lease_epoch=lease_epoch+1, lease_expires_at=now()+(%s || ' seconds')::interval,"
                " attempts=attempts+1, updated_at=now() WHERE tenant_id=%s AND id=%s AND owner_user_id=%s"
                " AND status IN ('queued','failed') AND attempts<3 RETURNING *",
                (lease_owner, str(lease_seconds), principal.tenant_id, job_id, principal.owner_user_id),
            ).fetchone()
            if row is None:
                current = c.execute(
                    "SELECT status FROM agent.mmi_parse_jobs WHERE tenant_id=%s AND id=%s AND owner_user_id=%s",
                    (principal.tenant_id, job_id, principal.owner_user_id),
                ).fetchone()
                if current and current["status"] == "cancelled":
                    raise MmiError("CANCELLED", "job cancelled")
                raise MmiError("STALE_LEASE", "job is not claimable")
            return _job(row)

    def finish_job(
        self,
        principal: Principal,
        job_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
        outcome: dict[str, Any] | None,
        document: dict[str, Any] | None,
        error_code: str | None,
    ) -> bool:
        from psycopg.types.json import Jsonb

        with self.db.tx(principal.tenant_id) as c:
            row = c.execute(
                "UPDATE agent.mmi_parse_jobs SET status=%s, outcome_json=%s, document_json=%s, error_code=%s,"
                " updated_at=now() WHERE tenant_id=%s AND id=%s AND owner_user_id=%s AND status='leased'"
                " AND lease_owner=%s AND lease_epoch=%s RETURNING id",
                (
                    "succeeded" if outcome is not None else "failed",
                    Jsonb(outcome) if outcome is not None else None,
                    Jsonb(document) if document is not None else None,
                    error_code,
                    principal.tenant_id,
                    job_id,
                    principal.owner_user_id,
                    lease_owner,
                    lease_epoch,
                ),
            ).fetchone()
            return row is not None

    def cancel_job(self, principal: Principal, job_id: str) -> JobRecord:
        with self.db.tx(principal.tenant_id) as c:
            row = c.execute(
                "UPDATE agent.mmi_parse_jobs SET status='cancelled', error_code='CANCELLED',"
                " updated_at=now() WHERE tenant_id=%s AND id=%s AND owner_user_id=%s"
                " AND status<>'succeeded' RETURNING *",
                (principal.tenant_id, job_id, principal.owner_user_id),
            ).fetchone()
            if row is None:
                row = c.execute(
                    "SELECT * FROM agent.mmi_parse_jobs WHERE tenant_id=%s AND id=%s AND owner_user_id=%s",
                    (principal.tenant_id, job_id, principal.owner_user_id),
                ).fetchone()
                if row is None:
                    raise MmiError("POLICY_DENIED", "job not found")
            return _job(row)


def _upload(row: dict[str, Any]) -> UploadSession:
    return UploadSession(
        row["id"],
        Principal(row["tenant_id"], row["owner_user_id"], row["workspace_id"]),
        row["original_filename"],
        row["declared_media_type"],
        row["expected_bytes"],
        row["content_sha256"],
        row["idempotency_key"],
        row["received_bytes"],
        int(row.get("chunk_count") or 0),
        row["state"],
        row.get("artifact_id"),
        _iso(row["created_at"]) or "",
        _iso(row["expires_at"]) or "",
    )


def _artifact(row: dict[str, Any]) -> ArtifactRecord:
    return ArtifactRecord(
        row["id"],
        Principal(row["tenant_id"], row["owner_user_id"], row["workspace_id"]),
        row["original_filename"],
        row["declared_media_type"],
        row["detected_format"],
        row["content_sha256"],
        row["byte_length"],
        row["storage_key"],
        row["quarantine_state"],
        _iso(row["created_at"]) or "",
        _iso(row["expires_at"]) or "",
        _iso(row.get("revoked_at")),
    )


def _job(row: dict[str, Any]) -> JobRecord:
    return JobRecord(
        row["id"],
        Principal(row["tenant_id"], row["owner_user_id"], row["workspace_id"]),
        row["artifact_id"],
        row["parse_key"],
        row["request_sha256"],
        dict(row["request_json"]),
        row["status"],
        row.get("lease_owner"),
        row["lease_epoch"],
        _iso(row.get("lease_expires_at")),
        row.get("outcome_json"),
        row.get("document_json"),
        row.get("error_code"),
        row["attempts"],
        _iso(row["created_at"]) or "",
        _iso(row["updated_at"]) or "",
    )
