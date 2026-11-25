"""Real HTTP tests for the multimodal routes (auth, upload, parse, citations)."""

from __future__ import annotations

import gc
import hashlib

from aiagent_api.app import create_app
from aiagent_core import admin
from aiagent_multimodal.pipeline import InProcessParserExecutor
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
        assert answer.json()["status"] == "answered"
        assert answer.json()["citations"][0]["fieldKey"] == "amount"

        other = admin.add_user(pg.owner, tenant["tenantId"])
        denied = client.get(f"/v1/multimodal/artifacts/{artifact_id}", headers=_headers(other["token"]))
        assert denied.status_code == 403
