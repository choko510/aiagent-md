"""Dependency-injected read-only FastAPI router.

Mount into the authenticated aiagent-api only when the caller has wired
Sentinel-enforced egress and a real authentication dependency. The development
entrypoint uses an explicit local-only opt-in, not production credentials.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from .catalog import CatalogError, CatalogService


def create_router(service: CatalogService, *, authenticate: Callable[..., Any]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/marketplace", tags=["marketplace"], dependencies=[Depends(authenticate)])

    @router.get("/items")
    async def list_items(
        q: str = Query(default="", max_length=200),
        kind: str = Query(default="all"),
        source: str = Query(default="all"),
        limit: int = Query(default=20, ge=1, le=50),
        cursor: str | None = Query(default=None, max_length=4096),
    ) -> dict[str, Any]:
        try:
            return await service.list_items(q=q, kind=kind, source=source, limit=limit, cursor=cursor)
        except CatalogError as exc:
            headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
            raise HTTPException(status_code=exc.status, detail=exc.code, headers=headers) from exc

    @router.get("/items/{item_id}")
    async def get_item(item_id: str) -> dict[str, Any]:
        try:
            return await service.detail(item_id)
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code) from exc

    return router
