"""Local read-only preview with mandatory loopback *and* bearer gate.

This process cannot be exposed through `uvicorn --host 0.0.0.0`, even when
the operator accidentally requests it: non-loopback peer IPs are denied.
Production must mount create_router in the authenticated main FastAPI app
and replace direct HTTP with a Sentinel-enforced transport.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .api import create_router
from .catalog import CachedJSONTransport, CatalogService, FixedSourceHTTP

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def create_development_app() -> FastAPI:
    if os.environ.get("AIAGENT_MARKETPLACE_DEV_MODE") != "1":
        raise RuntimeError("MKT-1 standalone server is disabled outside explicit local development")
    token = os.environ.get("AIAGENT_MARKETPLACE_DEV_TOKEN")
    if token is None or len(token) < 32:
        raise RuntimeError("MKT-1 local preview requires AIAGENT_MARKETPLACE_DEV_TOKEN (32+ chars)")
    transport = FixedSourceHTTP()
    service = CatalogService(CachedJSONTransport(transport))

    def verify_local_request(request: Request) -> None:
        client = request.client
        try:
            peer = ipaddress.ip_address(client.host) if client else None
        except ValueError:
            peer = None
        if peer is None or not peer.is_loopback:
            raise HTTPException(403, "LOCAL_ONLY")
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            raise HTTPException(403, "CROSS_SITE_REQUEST_DENIED")
        origin = request.headers.get("origin")
        if origin:
            try:
                parsed = urlsplit(origin)
                allowed = parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS
            except ValueError:
                allowed = False
            if not allowed:
                raise HTTPException(403, "ORIGIN_NOT_LOCAL")
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied, "Bearer " + token):
            raise HTTPException(401, "LOCAL_TOKEN_REQUIRED")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Any:
        yield
        await transport.close()

    app = FastAPI(title="aiagent-md MKT-1 local preview", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])

    @app.middleware("http")
    async def local_gate(request: Request, call_next: Any) -> Any:
        # Guards health and all routes, regardless of mount order or path.
        try:
            verify_local_request(request)
        except HTTPException as exc:
            from fastapi.responses import JSONResponse

            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)

    app.include_router(create_router(service, authenticate=verify_local_request))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "development-only"}

    return app


app = create_development_app()
