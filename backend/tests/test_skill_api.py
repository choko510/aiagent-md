from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

from aiagent_api.skills import build_skills_router
from aiagent_skills.contracts import InstallationState
from aiagent_skills.quarantine import QuarantineStore
from aiagent_skills.registry import InMemorySkillStore, SkillRegistry
from fastapi import FastAPI
from fastapi.testclient import TestClient

TENANT = "018f0000-0000-7000-8000-000000000001"
OWNER = "018f0000-0000-7000-8000-000000000002"


def make_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "SKILL.md",
            "---\nname: api-demo\ndescription: API lifecycle demo.\nversion: 1.0.0\n"
            "allowed-tools: tool:web.search\n---\n\n# Demo\n\nbody\n",
        )
    return buffer.getvalue()


def build_client(tmp_path: Path) -> tuple[TestClient, SkillRegistry]:
    store = InMemorySkillStore()
    registry = SkillRegistry(
        store,
        quarantine=QuarantineStore(tmp_path / "q"),
        run_authorizer=lambda _tenant, _run, _owner: True,
    )
    app = FastAPI()
    app.include_router(
        build_skills_router(
            registry_factory=lambda: registry,
            scope_dep=lambda: SimpleNamespace(tenant_id=TENANT, user_id=OWNER, workspace_id="ws"),
            policy_revision="policy:1",
        )
    )
    return TestClient(app), registry


def test_api_ingest_install_enable_and_preview(tmp_path: Path) -> None:
    client, registry = build_client(tmp_path)
    response = client.post(
        "/v1/extensions/skills/ingest",
        content=make_zip(),
        headers={"Content-Type": "application/zip", "Idempotency-Key": "api-ingest"},
    )
    assert response.status_code == 201, response.text
    receipt = response.json()["receipt"]
    release_id = receipt["releaseId"]
    assert receipt["result"]["scan"]["blocking"] is False
    release = registry.store.get_release(TENANT, release_id)
    assert release.scan is not None

    installed = client.post(
        f"/v1/extensions/skills/releases/{release_id}/install",
        json={"expectedContentDigest": release.content_digest},
        headers={"Idempotency-Key": "api-install"},
    )
    assert installed.status_code == 201, installed.text
    installation = registry.store.get_installation(TENANT, installed.json()["receipt"]["installationId"])
    assert installation.effective is InstallationState.INSTALLED_DISABLED

    enabled = client.post(
        f"/v1/extensions/skills/installations/{installation.id}/enable",
        json={
            "expectedRevision": installation.record_version,
            "expectedContentDigest": release.content_digest,
        },
        headers={"Idempotency-Key": "api-enable"},
    )
    assert enabled.status_code == 200, enabled.text

    catalog = client.get("/v1/skills")
    assert catalog.status_code == 200
    assert [item["name"] for item in catalog.json()["skills"]] == ["api-demo"]

    preview = client.get(f"/v1/extensions/skills/releases/{release_id}/files/SKILL.md")
    assert preview.status_code == 200
    assert preview.headers["x-content-type-options"] == "nosniff"
    assert "body" in preview.text

    files = client.get(f"/v1/extensions/skills/releases/{release_id}/files")
    assert files.status_code == 200
    assert files.json()["files"][0]["relativePath"] == "SKILL.md"


def test_api_requires_idempotency_and_exact_digest(tmp_path: Path) -> None:
    client, registry = build_client(tmp_path)
    archive = make_zip()
    assert client.post("/v1/extensions/skills/ingest", content=archive).status_code == 400

    response = client.post(
        "/v1/extensions/skills/ingest",
        content=archive,
        headers={"Content-Type": "application/zip", "Idempotency-Key": "k1"},
    )
    assert response.status_code == 201
    release_id = response.json()["receipt"]["releaseId"]

    mismatch = client.post(
        f"/v1/extensions/skills/releases/{release_id}/install",
        json={"expectedContentDigest": "0" * 64},
        headers={"Idempotency-Key": "k2"},
    )
    assert mismatch.status_code == 409

    idempotent = client.post(
        "/v1/extensions/skills/ingest",
        content=archive,
        headers={"Content-Type": "application/zip", "Idempotency-Key": "k1"},
    )
    assert idempotent.status_code == 201
    assert idempotent.json()["receipt"]["id"] == response.json()["receipt"]["id"]
    assert registry.store.get_release(TENANT, release_id).scan is not None


def ingest_version(client: TestClient, version: str, key: str) -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "SKILL.md",
            f"---\nname: api-demo\ndescription: API lifecycle demo.\nversion: {version}\n"
            "allowed-tools: tool:web.search\n---\n\n# Demo\n\nbody\n",
        )
    response = client.post(
        "/v1/extensions/skills/ingest",
        content=buffer.getvalue(),
        headers={"Content-Type": "application/zip", "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()["receipt"]["releaseId"]


def test_api_update_rollback_pin_disable_remove(tmp_path: Path) -> None:
    client, registry = build_client(tmp_path)
    v1 = ingest_version(client, "1.0.0", "v1")
    release1 = registry.store.get_release(TENANT, v1)
    installed = client.post(
        f"/v1/extensions/skills/releases/{v1}/install",
        json={"expectedContentDigest": release1.content_digest},
        headers={"Idempotency-Key": "install-v1"},
    )
    assert installed.status_code == 201, installed.text
    installation_id = installed.json()["receipt"]["installationId"]
    installation = registry.store.get_installation(TENANT, installation_id)
    enabled = client.post(
        f"/v1/extensions/skills/installations/{installation_id}/enable",
        json={"expectedRevision": installation.record_version, "expectedContentDigest": release1.content_digest},
        headers={"Idempotency-Key": "enable-v1"},
    )
    assert enabled.status_code == 200, enabled.text
    installation = registry.store.get_installation(TENANT, installation_id)

    pin = client.post(
        f"/v1/extensions/skills/installations/{installation_id}/pin",
        json={"runId": "018f0000-0000-7000-8000-0000000000aa"},
        headers={"Idempotency-Key": "pin-1"},
    )
    assert pin.status_code == 201, pin.text
    assert pin.json()["pin"]["releaseId"] == v1
    assert pin.json()["pin"]["activationGeneration"] == installation.activation_generation

    v2 = ingest_version(client, "1.1.0", "v2")
    release2 = registry.store.get_release(TENANT, v2)
    updated = client.post(
        f"/v1/extensions/skills/installations/{installation_id}/update",
        json={
            "targetReleaseId": v2,
            "expectedRevision": installation.record_version,
            "expectedContentDigest": release2.content_digest,
            "enableAfter": True,
        },
        headers={"Idempotency-Key": "update-v2"},
    )
    assert updated.status_code == 200, updated.text
    installation = registry.store.get_installation(TENANT, installation_id)
    assert installation.release_id == v2

    rolled = client.post(
        f"/v1/extensions/skills/installations/{installation_id}/rollback",
        json={
            "targetReleaseId": v1,
            "expectedRevision": installation.record_version,
            "expectedContentDigest": release1.content_digest,
            "reenable": True,
        },
        headers={"Idempotency-Key": "rollback-v1"},
    )
    assert rolled.status_code == 200, rolled.text
    installation = registry.store.get_installation(TENANT, installation_id)
    assert installation.release_id == v1

    disabled = client.post(
        f"/v1/extensions/skills/installations/{installation_id}/disable",
        json={"expectedRevision": installation.record_version},
        headers={"Idempotency-Key": "disable-v1"},
    )
    assert disabled.status_code == 200, disabled.text
    assert client.get("/v1/skills").json()["skills"] == []
    installation = registry.store.get_installation(TENANT, installation_id)

    removed = client.post(
        f"/v1/extensions/skills/installations/{installation_id}/remove",
        json={"expectedRevision": installation.record_version},
        headers={"Idempotency-Key": "remove-v1"},
    )
    assert removed.status_code == 200, removed.text
    removed_view = client.get(f"/v1/extensions/skills/installations/{installation_id}")
    assert removed_view.status_code == 200
    assert removed_view.json()["installation"]["effective"] == "REMOVED"


def test_api_upload_limits_and_preview_traversal(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    wrong_type = client.post(
        "/v1/extensions/skills/ingest",
        content=b"not a zip",
        headers={"Content-Type": "text/plain", "Idempotency-Key": "type"},
    )
    assert wrong_type.status_code == 415

    response = client.post(
        "/v1/extensions/skills/ingest",
        content=make_zip(),
        headers={"Content-Type": "application/zip", "Idempotency-Key": "preview"},
    )
    release_id = response.json()["receipt"]["releaseId"]
    assert client.get(f"/v1/extensions/skills/releases/{release_id}/files/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert client.get("/v1/extensions/skills/releases/missing").status_code == 404
    assert client.get("/v1/extensions/skills/installations/not-a-uuid").status_code == 404


def test_feature_flag_off_hides_routes(tmp_path: Path) -> None:
    registry = SkillRegistry(InMemorySkillStore(), quarantine=QuarantineStore(tmp_path / "q"))
    app = FastAPI()
    app.include_router(
        build_skills_router(
            registry_factory=lambda: registry,
            scope_dep=lambda: SimpleNamespace(tenant_id=TENANT, user_id=OWNER, workspace_id="ws"),
            policy_revision="policy:1",
            enabled=lambda: False,
        )
    )
    client = TestClient(app)
    assert client.get("/v1/skills").status_code == 404
    assert (
        client.post(
            "/v1/extensions/skills/ingest",
            content=make_zip(),
            headers={"Content-Type": "application/zip", "Idempotency-Key": "off"},
        ).status_code
        == 404
    )
