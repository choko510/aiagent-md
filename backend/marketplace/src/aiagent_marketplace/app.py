"""Local development preview ONLY. Never deploy this unauthenticated app publicly.

Start with AIAGENT_MARKETPLACE_DEV_MODE=1, bind uvicorn to 127.0.0.1, and use
Vite's /api proxy. Production must attach create_router() to authenticated
aiagent-api with a Sentinel-bound transport; no direct HTTP client is allowed.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from .api import create_router
from .catalog import CachedJSONTransport, CatalogService, FixedSourceHTTP


def create_development_app() -> FastAPI:
    if os.environ.get("AIAGENT_MARKETPLACE_DEV_MODE") != "1":
        raise RuntimeError("MKT-1 standalone server is disabled outside explicit local development")
    transport = FixedSourceHTTP()
    service = CatalogService(CachedJSONTransport(transport))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Any:
        yield
        await transport.close()

    app = FastAPI(title="aiagent-md MKT-1 local preview", lifespan=lifespan)
    # No production authentication, so NEVER publicly expose this app.
    app.include_router(create_router(service, authenticate=lambda: None))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "development-only"}

    return app


app = create_development_app()
