"""HTTP routes for multimodal ingestion.

The router is built with the API's existing auth dependency so it inherits the
same bearer/session + CSRF rules. Every store call receives the authenticated
principal and applies owner predicates; no tenant or owner id is read from a
request body.
"""

import asyncio
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from aiagent_multimodal.blobstore import QuarantineBlobStore
from aiagent_multimodal.contracts import MmiRequestV1, MmiSource
from aiagent_multimodal.errors import MmiError
from aiagent_multimodal.limits import MmiLimits
from aiagent_multimodal.pipeline import InProcessParserExecutor, ParserExecutor
from aiagent_multimodal.service import MultimodalService, ServiceConfig
from aiagent_multimodal.store import PostgresStore, Principal
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

_MAX_JSON = 16 * 1024


def build_router(
    get_db: Callable[[], Any],
    auth_scope: Callable[..., Any],
    *,
    blob_root: Path | None = None,
    executor: ParserExecutor | None = None,
    allow_inprocess: bool | None = None,
    profile: str = "test",
) -> APIRouter:
    router = APIRouter(prefix="/v1/multimodal", tags=["multimodal"])
    lock = threading.Lock()
    holder: dict[str, MultimodalService] = {}

    def _root() -> Path:
        if blob_root is not None:
            return blob_root
        import os

        configured = os.environ.get("AIAGENT_MMI_BLOB_ROOT")
        if configured:
            return Path(configured)
        if profile == "production":
            raise HTTPException(503, detail={"error": "MMI_STORAGE_UNAVAILABLE"})
        return Path(tempfile.gettempdir()) / "aiagent-mmi-quarantine"

    def get_service() -> MultimodalService:
        if profile == "production":
            # There is no attested isolated worker implementation yet. Fail the entire MMI
            # admission surface closed, not only start_parse: even format detection touches
            # untrusted ZIP/XML/PDF bytes in the trusted API process.
            raise HTTPException(503, detail={"error": "MMI_PRODUCTION_DISABLED"})
        with lock:
            if "service" not in holder:
                chosen = executor
                enabled = allow_inprocess
                if enabled is None:
                    import os

                    enabled = os.environ.get("AIAGENT_MMI_ALLOW_INPROCESS") == "1"
                if chosen is None and enabled:
                    chosen = InProcessParserExecutor()
                limits = MmiLimits()
                holder["service"] = MultimodalService(
                    store=PostgresStore(get_db()),
                    blobs=QuarantineBlobStore(_root(), max_file_bytes=limits.max_file_bytes),
                    executor=chosen,
                    limits=limits,
                    config=ServiceConfig(),
                    isolation_note=(
                        "in-process executor is development-only; production requires an isolated worker"
                        if chosen is not None and isinstance(chosen, InProcessParserExecutor)
                        else ""
                    ),
                )
            return holder["service"]

    async def principal(s: Annotated[Any, Depends(auth_scope)]) -> Principal:
        return Principal(str(s.tenant_id), str(s.user_id), str(s.workspace_id))

    P = Annotated[Principal, Depends(principal)]

    def _http(exc: MmiError) -> HTTPException:
        detail: dict[str, Any] = {"error": exc.code}
        if exc.retryable:
            detail["retryable"] = True
        return HTTPException(exc.http_status, detail=detail)

    async def _json(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > _MAX_JSON:
            raise HTTPException(413, detail={"error": "MAX_INPUT"})
        import json

        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise HTTPException(422, detail={"error": "INVALID_JSON"}) from None
        if not isinstance(body, dict):
            raise HTTPException(422, detail={"error": "INVALID_REQUEST"})
        return body

    def _string(body: dict[str, Any], key: str, *, max_len: int = 512, required: bool = True) -> str:
        value = body.get(key)
        if value is None and not required:
            return ""
        if not isinstance(value, str) or not value or len(value) > max_len:
            raise HTTPException(422, detail={"error": f"INVALID_{key.upper()}"})
        return value

    def _flag(body: dict[str, Any], key: str) -> bool:
        value = body.get(key, False)
        if type(value) is not bool:
            raise HTTPException(422, detail={"error": f"INVALID_{key.upper()}"})
        return value

    @router.get("/capabilities")
    async def capabilities() -> JSONResponse:
        if profile == "production":
            return JSONResponse(
                {
                    "parserIsolation": "unavailable",
                    "externalVision": False,
                    "localOcr": "unavailable",
                    "note": "production multimodal admission is disabled until an attested isolated worker exists",
                }
            )
        service = get_service()
        if service.pipeline is None:
            isolation = "unavailable"
        elif service.isolation_note:
            isolation = "in_process_dev"
        else:
            # A custom executor's OS-level confinement is not attested here. Never label it "isolated"
            # merely because it is not the in-process implementation.
            isolation = "unverified"
        return JSONResponse(
            {
                "parserIsolation": isolation,
                "externalVision": False,
                "localOcr": "optional",
                "note": service.isolation_note,
            }
        )

    @router.post("/uploads", status_code=201)
    async def begin_upload(request: Request, p: P) -> JSONResponse:
        body = await _json(request)
        expected_bytes = body.get("byteLength")
        if type(expected_bytes) is not int or expected_bytes < 0:
            raise HTTPException(422, detail={"error": "INVALID_BYTELENGTH"})
        try:
            result = get_service().begin_upload(
                p,
                original_filename=_string(body, "fileName", max_len=255),
                declared_media_type=_string(body, "declaredMediaType", max_len=128, required=False)
                or "application/octet-stream",
                expected_bytes=expected_bytes,
                content_sha256=_string(body, "sha256", max_len=64),
                idempotency_key=_string(body, "idempotencyKey", max_len=256),
            )
        except MmiError as exc:
            raise _http(exc) from None
        except (TypeError, ValueError):
            raise HTTPException(422, detail={"error": "INVALID_REQUEST"}) from None
        return JSONResponse(result, status_code=201)

    @router.put("/uploads/{upload_id}/chunks/{index}")
    async def put_chunk(
        upload_id: str,
        index: int,
        request: Request,
        p: P,
        x_chunk_sha256: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        if index < 0 or index > 65535:
            raise HTTPException(422, detail={"error": "INVALID_CHUNK_INDEX"})
        total = 0
        parts: list[bytes] = []
        async for part in request.stream():
            total += len(part)
            if total > (1 << 20):
                raise HTTPException(413, detail={"error": "MAX_INPUT"})
            parts.append(part)
        if not parts:
            raise HTTPException(422, detail={"error": "EMPTY_CHUNK"})
        try:
            result = get_service().put_chunk(p, upload_id, index, b"".join(parts), x_chunk_sha256)
        except MmiError as exc:
            raise _http(exc) from None
        return JSONResponse(result)

    @router.post("/uploads/{upload_id}/complete")
    async def complete_upload(upload_id: str, p: P) -> JSONResponse:
        try:
            result = get_service().complete_upload(p, upload_id)
        except MmiError as exc:
            raise _http(exc) from None
        return JSONResponse(result)

    @router.get("/artifacts/{artifact_id}")
    async def get_artifact(artifact_id: str, p: P) -> JSONResponse:
        try:
            return JSONResponse(get_service().get_artifact(p, artifact_id))
        except MmiError as exc:
            raise _http(exc) from None

    @router.delete("/artifacts/{artifact_id}")
    async def revoke_artifact(artifact_id: str, p: P) -> JSONResponse:
        try:
            return JSONResponse(get_service().revoke_artifact(p, artifact_id))
        except MmiError as exc:
            raise _http(exc) from None

    @router.post("/artifacts/{artifact_id}/jobs", status_code=202)
    async def create_job(artifact_id: str, request: Request, p: P) -> JSONResponse:
        body = await _json(request)
        service = get_service()
        if service.pipeline is None:
            raise HTTPException(503, detail={"error": "PARSER_ISOLATION_UNAVAILABLE"})
        fields = body.get("requestedFields", ["amount"])
        if (
            not isinstance(fields, list)
            or not fields
            or len(fields) > 16
            or any(not isinstance(f, str) or not f or len(f) > 128 for f in fields)
        ):
            raise HTTPException(422, detail={"error": "INVALID_REQUESTED_FIELDS"})
        locale = body.get("preferredLocale")
        if locale is not None and (not isinstance(locale, str) or len(locale) > 35):
            raise HTTPException(422, detail={"error": "INVALID_PREFERRED_LOCALE"})
        try:
            artifact = service.get_artifact(p, artifact_id)
            request_v1 = MmiRequestV1(
                source_artifact_id=artifact_id,
                source=MmiSource(kind="user_upload", acquired_at=artifact["createdAt"]),
                requested_fields=tuple(fields),
                preferred_locale=locale,
                allow_local_ocr=_flag(body, "allowLocalOcr"),
                allow_external_vision=False,
                idempotency_key=_string(body, "idempotencyKey", max_len=256),
                deadline_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
            )
            job = service.start_parse(p, artifact_id, request=request_v1, idempotency_key=request_v1.idempotency_key)
        except MmiError as exc:
            raise _http(exc) from None
        except ValueError:
            raise HTTPException(422, detail={"error": "INVALID_REQUEST"}) from None
        return JSONResponse(service.get_job(p, job.id), status_code=202)

    @router.post("/jobs/{job_id}/run")
    async def run_job(job_id: str, p: P) -> JSONResponse:
        service = get_service()
        if service.pipeline is None:
            raise HTTPException(503, detail={"error": "PARSER_ISOLATION_UNAVAILABLE"})
        try:
            # Parsing and verification are CPU/IO-bound synchronous code. Running them
            # directly in the event loop would stall every other request in this worker.
            await asyncio.to_thread(service.run_job, p, job_id)
            return JSONResponse(service.get_job(p, job_id))
        except MmiError as exc:
            raise _http(exc) from None

    @router.get("/jobs/{job_id}")
    async def get_job(job_id: str, p: P) -> JSONResponse:
        try:
            return JSONResponse(get_service().get_job(p, job_id))
        except MmiError as exc:
            raise _http(exc) from None

    @router.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, p: P) -> JSONResponse:
        try:
            return JSONResponse(get_service().cancel_job(p, job_id))
        except MmiError as exc:
            raise _http(exc) from None

    @router.get("/artifacts/{artifact_id}/context")
    async def context(artifact_id: str, p: P, q: str = "", limit: int = 8) -> JSONResponse:
        if len(q) > 2_000 or limit < 1 or limit > 50:
            raise HTTPException(422, detail={"error": "INVALID_QUERY"})
        try:
            return JSONResponse(get_service().retrieve(p, artifact_id, q, limit=limit))
        except MmiError as exc:
            raise _http(exc) from None

    @router.post("/artifacts/{artifact_id}/answer")
    async def answer(artifact_id: str, request: Request, p: P) -> JSONResponse:
        body = await _json(request)
        question = _string(body, "question", max_len=2_000)
        try:
            return JSONResponse(get_service().ask(p, artifact_id, question))
        except MmiError as exc:
            raise _http(exc) from None

    @router.get("/jobs/{job_id}/citations")
    async def citations(job_id: str, p: P) -> JSONResponse:
        try:
            return JSONResponse({"jobId": job_id, "citations": get_service().citations(p, job_id)})
        except MmiError as exc:
            raise _http(exc) from None

    return router
