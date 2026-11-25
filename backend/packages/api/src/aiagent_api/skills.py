"""Skill lifecycle HTTP routes.

The router is intentionally constructed with an injected registry factory and scope dependency so it can be tested
without the main application or a database. Mutations require Idempotency-Key and an explicit expected revision;
installation and enablement are always separate operations.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from aiagent_skills.contracts import ReleaseState, SkillApproval, SkillRelease
from aiagent_skills.digest import capability_digest
from aiagent_skills.errors import SkillError
from aiagent_skills.policy import validate_requested
from aiagent_skills.registry import SkillRegistry, make_approval
from aiagent_skills.sources import upload_source
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_PREVIEW_BYTES = 64 * 1024
ZIP_TYPES = frozenset({"application/zip", "application/x-zip-compressed", "application/octet-stream"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstallRequest(_Strict):
    expected_content_digest: str = Field(alias="expectedContentDigest", min_length=64, max_length=64)
    policy_revision: str | None = Field(default=None, alias="policyRevision", max_length=128)
    ttl_seconds: int = Field(default=3600, alias="ttlSeconds", ge=1, le=86400)


class EnableRequest(_Strict):
    expected_revision: int = Field(alias="expectedRevision", ge=1)
    expected_content_digest: str = Field(alias="expectedContentDigest", min_length=64, max_length=64)
    policy_revision: str | None = Field(default=None, alias="policyRevision", max_length=128)
    ttl_seconds: int = Field(default=3600, alias="ttlSeconds", ge=1, le=86400)


class RevisionRequest(_Strict):
    expected_revision: int = Field(alias="expectedRevision", ge=1)


class UpdateRequest(_Strict):
    target_release_id: str = Field(alias="targetReleaseId", min_length=1, max_length=64)
    expected_revision: int = Field(alias="expectedRevision", ge=1)
    expected_content_digest: str = Field(alias="expectedContentDigest", min_length=64, max_length=64)
    enable_after: bool = Field(default=False, alias="enableAfter")
    policy_revision: str | None = Field(default=None, alias="policyRevision", max_length=128)
    ttl_seconds: int = Field(default=3600, alias="ttlSeconds", ge=1, le=86400)


class RollbackRequest(_Strict):
    target_release_id: str = Field(alias="targetReleaseId", min_length=1, max_length=64)
    expected_revision: int = Field(alias="expectedRevision", ge=1)
    expected_content_digest: str = Field(alias="expectedContentDigest", min_length=64, max_length=64)
    reenable: bool = Field(default=False)
    policy_revision: str | None = Field(default=None, alias="policyRevision", max_length=128)
    ttl_seconds: int = Field(default=3600, alias="ttlSeconds", ge=1, le=86400)


class PinRequest(_Strict):
    run_id: UUID = Field(alias="runId")


def _ids(scope: Any) -> tuple[str, str]:
    tenant_id = getattr(scope, "tenant_id", None)
    user_id = getattr(scope, "user_id", None)
    if not tenant_id or not user_id:
        raise HTTPException(401, detail={"error": "UNAUTHENTICATED"})
    return str(tenant_id), str(user_id)


def _raise(exc: SkillError) -> None:
    raise HTTPException(exc.http_status, detail={"error": exc.code, "message": exc.detail, **exc.context}) from None


def _uuid_or_404(value: str) -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError):
        raise HTTPException(404, detail={"error": "SKILL_NOT_FOUND"}) from None


def _requested_digest(release: SkillRelease) -> str:
    return capability_digest(validate_requested(release.manifest.requested_capabilities))


def _approval(
    *,
    scope: Any,
    action: str,
    release: SkillRelease,
    request_id: str,
    expected_revision: int,
    policy_revision: str,
    ttl_seconds: int,
) -> SkillApproval:
    tenant_id, user_id = _ids(scope)
    return make_approval(
        tenant_id=tenant_id,
        owner_user_id=user_id,
        actor_user_id=user_id,
        action=action,
        release=release,
        capability_digest_value=_requested_digest(release),
        policy_revision=policy_revision,
        request_id=request_id,
        expected_installation_revision=expected_revision,
        ttl_seconds=ttl_seconds,
    )


def build_skills_router(
    *,
    registry_factory: Callable[[], SkillRegistry],
    scope_dep: Callable[..., Any],
    policy_revision: str,
    max_upload_bytes: int = MAX_UPLOAD_BYTES,
    enabled: Callable[[], bool] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["skills"])

    def require_enabled() -> None:
        if enabled is not None and not enabled():
            raise HTTPException(404, detail={"error": "SKILLS_DISABLED"})

    def key_or_400(key: str | None) -> str:
        if not key or len(key) > 128:
            raise HTTPException(400, detail={"error": "IDEMPOTENCY_KEY_REQUIRED"})
        return key

    def policy_or_400(value: str | None) -> str:
        if value is not None and value != policy_revision:
            raise HTTPException(409, detail={"error": "STALE_POLICY_REVISION"})
        return policy_revision

    def get_release(scope: Any, release_id: str) -> SkillRelease:
        tenant_id, user_id = _ids(scope)
        release_id = _uuid_or_404(release_id)
        registry = registry_factory()
        try:
            release = registry.store.get_release(tenant_id, release_id)
        except SkillError:
            raise HTTPException(404, detail={"error": "SKILL_NOT_FOUND"}) from None
        if release.owner_user_id not in (None, user_id):
            raise HTTPException(404, detail={"error": "SKILL_NOT_FOUND"})
        return release

    @router.get("/v1/skills")
    def list_skills(scope: object = Depends(scope_dep)) -> dict[str, Any]:
        require_enabled()
        tenant_id, user_id = _ids(scope)
        entries = registry_factory().catalog(tenant_id, user_id).all_entries()
        return {"skills": [entry.to_wire() for entry in entries]}

    @router.get("/v1/skills/search")
    def search_skills(
        scope: object = Depends(scope_dep),
        q: Annotated[str, Query(min_length=0, max_length=128)] = "",
    ) -> dict[str, Any]:
        require_enabled()
        tenant_id, user_id = _ids(scope)
        try:
            entries = registry_factory().search(tenant_id, user_id, q)
        except SkillError as exc:
            _raise(exc)
        return {"skills": [entry.to_wire() for entry in entries], "query": q}

    @router.get("/v1/extensions/skills/installations")
    def list_installations(scope: object = Depends(scope_dep)) -> dict[str, Any]:
        require_enabled()
        tenant_id, user_id = _ids(scope)
        rows = registry_factory().store.list_installations(tenant_id, user_id)
        return {"installations": [row.to_wire() for row in rows]}

    @router.get("/v1/extensions/skills/installations/{installation_id}")
    def get_installation(installation_id: str, scope: object = Depends(scope_dep)) -> dict[str, Any]:
        require_enabled()
        installation_id = _uuid_or_404(installation_id)
        tenant_id, user_id = _ids(scope)
        try:
            row = registry_factory().store.get_installation(tenant_id, installation_id)
        except SkillError as exc:
            _raise(exc)
        if row.owner_user_id != user_id:
            raise HTTPException(404, detail={"error": "SKILL_NOT_FOUND"})
        return {"installation": row.to_wire()}

    @router.get("/v1/extensions/skills/releases/{release_id}")
    def get_release_route(release_id: str, scope: object = Depends(scope_dep)) -> dict[str, Any]:
        require_enabled()
        release = get_release(scope, release_id)
        return {"release": release.to_wire()}

    @router.get("/v1/extensions/skills/releases/{release_id}/files")
    def list_release_files(release_id: str, scope: object = Depends(scope_dep)) -> dict[str, Any]:
        require_enabled()
        release = get_release(scope, release_id)
        return {"files": [entry.to_wire() for entry in release.manifest.files]}

    @router.get("/v1/extensions/skills/releases/{release_id}/files/{file_path:path}")
    def preview_release_file(release_id: str, file_path: str, scope: object = Depends(scope_dep)) -> Response:
        require_enabled()
        release = get_release(scope, release_id)
        if release.state is ReleaseState.REVOKED or release.manifest.source.kind.value == "builtin":
            raise HTTPException(404, detail={"error": "SKILL_NOT_FOUND"})
        entry = next((f for f in release.manifest.files if f.relative_path == file_path), None)
        if entry is None:
            raise HTTPException(404, detail={"error": "SKILL_FILE_NOT_FOUND"})
        registry = registry_factory()
        if registry.quarantine is None:
            raise HTTPException(503, detail={"error": "QUARANTINE_UNAVAILABLE"})
        bundle_digest = str(release.manifest.provenance.get("bundleDigest", ""))
        try:
            files = registry.quarantine.read(bundle_digest)
        except SkillError as exc:
            _raise(exc)
        payload = files.get(file_path)
        if payload is None:
            raise HTTPException(404, detail={"error": "SKILL_FILE_NOT_FOUND"})
        if len(payload) > MAX_PREVIEW_BYTES:
            raise HTTPException(413, detail={"error": "SKILL_FILE_TOO_LARGE"})
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(415, detail={"error": "SKILL_FILE_NOT_TEXT"}) from None
        return PlainTextResponse(
            text,
            media_type="text/plain; charset=utf-8",
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
                "Cache-Control": "no-store",
            },
        )

    @router.post("/v1/extensions/skills/ingest", status_code=201)
    async def ingest_skill(
        request: Request,
        scope: object = Depends(scope_dep),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        require_enabled()
        key = key_or_400(idempotency_key)
        content_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if content_type not in ZIP_TYPES:
            raise HTTPException(415, detail={"error": "UNSUPPORTED_MEDIA_TYPE"})
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > max_upload_bytes:
                raise HTTPException(413, detail={"error": "UPLOAD_TOO_LARGE"})
            chunks.append(chunk)
        archive = b"".join(chunks)
        tenant_id, user_id = _ids(scope)
        try:
            receipt = registry_factory().ingest(
                tenant_id=tenant_id,
                owner_user_id=user_id,
                actor_user_id=user_id,
                source=upload_source(archive).source,
                archive=archive,
                request_id=key,
            )
        except SkillError as exc:
            _raise(exc)
        return {"receipt": receipt.to_wire()}

    @router.post("/v1/extensions/skills/releases/{release_id}/install", status_code=201)
    def install_skill(
        release_id: str,
        body: InstallRequest,
        scope: object = Depends(scope_dep),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        require_enabled()
        key = key_or_400(idempotency_key)
        release = get_release(scope, release_id)
        if release.content_digest != body.expected_content_digest:
            raise HTTPException(409, detail={"error": "STALE_CONTENT_DIGEST"})
        policy = policy_or_400(body.policy_revision)
        tenant_id, user_id = _ids(scope)
        registry = registry_factory()
        approval = _approval(
            scope=scope,
            action="install",
            release=release,
            request_id=key,
            expected_revision=0,
            policy_revision=policy,
            ttl_seconds=body.ttl_seconds,
        )
        try:
            receipt = registry.install(
                tenant_id=tenant_id,
                owner_user_id=user_id,
                actor_user_id=user_id,
                release_id=release.id,
                approval=approval,
                capability_digest_value=_requested_digest(release),
                policy_revision=policy,
                request_id=key,
            )
        except SkillError as exc:
            _raise(exc)
        return {"receipt": receipt.to_wire()}

    @router.post("/v1/extensions/skills/installations/{installation_id}/enable")
    def enable_skill(
        installation_id: str,
        body: EnableRequest,
        scope: object = Depends(scope_dep),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        require_enabled()
        installation_id = _uuid_or_404(installation_id)
        key = key_or_400(idempotency_key)
        tenant_id, user_id = _ids(scope)
        registry = registry_factory()
        try:
            installation = registry.store.get_installation(tenant_id, installation_id)
        except SkillError as exc:
            _raise(exc)
        if installation.owner_user_id != user_id:
            raise HTTPException(404, detail={"error": "SKILL_NOT_FOUND"})
        release = get_release(scope, installation.release_id)
        if release.content_digest != body.expected_content_digest:
            raise HTTPException(409, detail={"error": "STALE_CONTENT_DIGEST"})
        policy = policy_or_400(body.policy_revision)
        approval = _approval(
            scope=scope,
            action="enable",
            release=release,
            request_id=key,
            expected_revision=body.expected_revision,
            policy_revision=policy,
            ttl_seconds=body.ttl_seconds,
        )
        try:
            receipt = registry.enable(
                tenant_id=tenant_id,
                owner_user_id=user_id,
                actor_user_id=user_id,
                installation_id=installation_id,
                approval=approval,
                capability_digest_value=_requested_digest(release),
                policy_revision=policy,
                expected_revision=body.expected_revision,
                request_id=key,
            )
        except SkillError as exc:
            _raise(exc)
        return {"receipt": receipt.to_wire()}

    @router.post("/v1/extensions/skills/installations/{installation_id}/disable")
    def disable_skill(
        installation_id: str,
        body: RevisionRequest,
        scope: object = Depends(scope_dep),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        require_enabled()
        installation_id = _uuid_or_404(installation_id)
        key = key_or_400(idempotency_key)
        tenant_id, user_id = _ids(scope)
        try:
            receipt = registry_factory().disable(
                tenant_id=tenant_id,
                owner_user_id=user_id,
                actor_user_id=user_id,
                installation_id=installation_id,
                expected_revision=body.expected_revision,
                request_id=key,
            )
        except SkillError as exc:
            _raise(exc)
        return {"receipt": receipt.to_wire()}

    @router.post("/v1/extensions/skills/installations/{installation_id}/update")
    def update_skill(
        installation_id: str,
        body: UpdateRequest,
        scope: object = Depends(scope_dep),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        require_enabled()
        installation_id = _uuid_or_404(installation_id)
        key = key_or_400(idempotency_key)
        tenant_id, user_id = _ids(scope)
        registry = registry_factory()
        target = get_release(scope, body.target_release_id)
        if target.content_digest != body.expected_content_digest:
            raise HTTPException(409, detail={"error": "STALE_CONTENT_DIGEST"})
        policy = policy_or_400(body.policy_revision)
        approval = _approval(
            scope=scope,
            action="update",
            release=target,
            request_id=key,
            expected_revision=body.expected_revision,
            policy_revision=policy,
            ttl_seconds=body.ttl_seconds,
        )
        try:
            receipt = registry.update(
                tenant_id=tenant_id,
                owner_user_id=user_id,
                actor_user_id=user_id,
                installation_id=installation_id,
                target_release_id=target.id,
                approval=approval,
                capability_digest_value=_requested_digest(target),
                policy_revision=policy,
                expected_revision=body.expected_revision,
                enable_after=body.enable_after,
                request_id=key,
            )
        except SkillError as exc:
            _raise(exc)
        return {"receipt": receipt.to_wire()}

    @router.post("/v1/extensions/skills/installations/{installation_id}/rollback")
    def rollback_skill(
        installation_id: str,
        body: RollbackRequest,
        scope: object = Depends(scope_dep),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        require_enabled()
        installation_id = _uuid_or_404(installation_id)
        key = key_or_400(idempotency_key)
        tenant_id, user_id = _ids(scope)
        registry = registry_factory()
        target = get_release(scope, body.target_release_id)
        if target.content_digest != body.expected_content_digest:
            raise HTTPException(409, detail={"error": "STALE_CONTENT_DIGEST"})
        policy = policy_or_400(body.policy_revision)
        approval = _approval(
            scope=scope,
            action="rollback",
            release=target,
            request_id=key,
            expected_revision=body.expected_revision,
            policy_revision=policy,
            ttl_seconds=body.ttl_seconds,
        )
        try:
            receipt = registry.rollback(
                tenant_id=tenant_id,
                owner_user_id=user_id,
                actor_user_id=user_id,
                installation_id=installation_id,
                target_release_id=target.id,
                approval=approval,
                capability_digest_value=_requested_digest(target),
                policy_revision=policy,
                expected_revision=body.expected_revision,
                reenable=body.reenable,
                request_id=key,
            )
        except SkillError as exc:
            _raise(exc)
        return {"receipt": receipt.to_wire()}

    @router.post("/v1/extensions/skills/installations/{installation_id}/remove")
    def remove_skill(
        installation_id: str,
        body: RevisionRequest,
        scope: object = Depends(scope_dep),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        require_enabled()
        installation_id = _uuid_or_404(installation_id)
        key = key_or_400(idempotency_key)
        tenant_id, user_id = _ids(scope)
        try:
            receipt = registry_factory().remove(
                tenant_id=tenant_id,
                owner_user_id=user_id,
                actor_user_id=user_id,
                installation_id=installation_id,
                expected_revision=body.expected_revision,
                request_id=key,
            )
        except SkillError as exc:
            _raise(exc)
        return {"receipt": receipt.to_wire()}

    @router.post("/v1/extensions/skills/installations/{installation_id}/revoke")
    def revoke_skill(
        installation_id: str,
        scope: object = Depends(scope_dep),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        require_enabled()
        installation_id = _uuid_or_404(installation_id)
        key = key_or_400(idempotency_key)
        tenant_id, user_id = _ids(scope)
        try:
            receipt = registry_factory().revoke(
                tenant_id=tenant_id,
                owner_user_id=user_id,
                actor_user_id=user_id,
                installation_id=installation_id,
                request_id=key,
            )
        except SkillError as exc:
            _raise(exc)
        return {"receipt": receipt.to_wire()}

    @router.post("/v1/extensions/skills/installations/{installation_id}/pin", status_code=201)
    def pin_skill(
        installation_id: str,
        body: PinRequest,
        scope: object = Depends(scope_dep),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        require_enabled()
        installation_id = _uuid_or_404(installation_id)
        key_or_400(idempotency_key)
        tenant_id, user_id = _ids(scope)
        registry = registry_factory()
        try:
            installation = registry.store.get_installation(tenant_id, installation_id)
        except SkillError as exc:
            _raise(exc)
        if installation.owner_user_id != user_id:
            raise HTTPException(404, detail={"error": "SKILL_NOT_FOUND"})
        release = get_release(scope, installation.release_id)
        try:
            resolution = registry.resolve_dependencies(tenant_id, user_id, release)
            pin = registry.pin_for_run(
                tenant_id=tenant_id,
                run_id=str(body.run_id),
                installation_id=installation_id,
                capability_digest_value=_requested_digest(release),
                dependency_digest_value=resolution.digest,
            )
        except SkillError as exc:
            _raise(exc)
        return {"pin": pin.to_wire()}

    return router


def skill_lifecycle_enabled() -> bool:
    import os

    return os.environ.get("AIAGENT_SKILLS_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


__all__ = ["build_skills_router", "skill_lifecycle_enabled"]
