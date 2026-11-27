"""Real HTTP tests for the multimodal routes (auth, upload, parse, citations)."""

from __future__ import annotations

import gc
import hashlib
from types import SimpleNamespace

from aiagent_api.app import create_app
from aiagent_api.multimodal_api import build_router, production_gate
from aiagent_core import admin
from aiagent_multimodal.pipeline import InProcessParserExecutor
from fastapi import FastAPI
from fastapi.testclient import TestClient

CSV = "item,price\napple,\u00a5100\n".encode()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_upload_parse_answer_over_http(pg, tenant, tmp_path) -> None:
    db = pg.db("api", max_size=4)
    app = create_app(
        db=db,
        multimodal_blob_root=tmp_path,
        multimodal_executor=InProcessParserExecutor(),
    )
    try:
        _exercise(client=TestClient(app), tenant=tenant, pg=pg)
    finally:
        db.close()
        gc.collect()


def _exercise(*, client: TestClient, tenant, pg) -> None:
    with client:
        digest = hashlib.sha256(CSV).hexdigest()
        started = client.post(
            "/v1/multimodal/uploads",
            headers=_headers(tenant["token"]),
            json={
                "fileName": "../../price.csv",
                "declaredMediaType": "text/csv",
                "byteLength": len(CSV),
                "sha256": digest,
                "idempotencyKey": "http-upload-1",
            },
        )
        assert started.status_code == 201, started.text
        upload_id = started.json()["uploadId"]
        chunk = client.put(
            f"/v1/multimodal/uploads/{upload_id}/chunks/0",
            headers={**_headers(tenant["token"]), "X-Chunk-Sha256": digest},
            content=CSV,
        )
        assert chunk.status_code == 200, chunk.text
        completed = client.post(f"/v1/multimodal/uploads/{upload_id}/complete", headers=_headers(tenant["token"]))
        assert completed.status_code == 200, completed.text
        artifact = completed.json()
        assert artifact["displayName"] == "price.csv"
        artifact_id = artifact["artifactId"]

        created = client.post(
            f"/v1/multimodal/artifacts/{artifact_id}/jobs",
            headers=_headers(tenant["token"]),
            json={"requestedFields": ["amount"], "idempotencyKey": "http-job-1"},
        )
        assert created.status_code == 202, created.text
        job_id = created.json()["jobId"]
        ran = client.post(f"/v1/multimodal/jobs/{job_id}/run", headers=_headers(tenant["token"]))
        assert ran.status_code == 200, ran.text
        job = ran.json()
        assert job["status"] == "succeeded"
        assert job["citations"][0]["locator"]["kind"] == "csv"
        assert job["outcome"]["candidates"][0]["verification"] == "anchored"

        answer = client.post(
            f"/v1/multimodal/artifacts/{artifact_id}/answer",
            headers=_headers(tenant["token"]),
            json={"question": "What is the price?"},
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["status"] == "conditional"
        assert answer.json()["citations"][0]["fieldKey"] == "amount"
        assert "tax:unknown" in answer.json()["citations"][0]["conditions"]

        other = admin.add_user(pg.owner, tenant["tenantId"])
        denied = client.get(f"/v1/multimodal/artifacts/{artifact_id}", headers=_headers(other["token"]))
        assert denied.status_code == 403


def test_production_surface_is_disabled_even_with_inprocess_optin() -> None:
    scope = SimpleNamespace(
        tenant_id="01980000-0000-7000-8000-000000000001",
        user_id="01980000-0000-7000-8000-000000000002",
        workspace_id="01980000-0000-7000-8000-000000000003",
    )
    app = FastAPI()
    app.include_router(
        build_router(
            lambda: None,
            lambda: scope,
            profile="production",
            allow_inprocess=True,
            executor=InProcessParserExecutor(),
        )
    )
    with TestClient(app) as client:
        caps = client.get("/v1/multimodal/capabilities")
        assert caps.status_code == 200
        assert caps.json()["parserIsolation"] == "unavailable"
        denied = client.post(
            "/v1/multimodal/uploads",
            json={
                "fileName": "x.csv",
                "declaredMediaType": "text/csv",
                "byteLength": 4,
                "sha256": "0" * 64,
                "idempotencyKey": "prod",
            },
        )
        assert denied.status_code == 503
        assert denied.json()["detail"]["error"] == "MMI_PRODUCTION_DISABLED"


def test_production_gate_rejects_before_reading_a_large_body() -> None:
    scope = SimpleNamespace(
        tenant_id="01980000-0000-7000-8000-000000000001",
        user_id="01980000-0000-7000-8000-000000000002",
        workspace_id="01980000-0000-7000-8000-000000000003",
    )
    app = FastAPI()
    app.middleware("http")(production_gate)
    app.include_router(build_router(lambda: None, lambda: scope, profile="production"))
    with TestClient(app) as client:
        denied = client.post(
            "/v1/multimodal/uploads",
            content=b'{"x":"' + b"a" * (256 * 1024) + b'"}',
            headers={"Content-Type": "application/json"},
        )
        assert denied.status_code == 503
        assert denied.json()["detail"]["error"] == "MMI_PRODUCTION_DISABLED"
        caps = client.get("/v1/multimodal/capabilities")
        assert caps.status_code == 200
        assert caps.json()["parserIsolation"] == "unavailable"
