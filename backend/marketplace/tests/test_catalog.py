"""MKT-1 offline tests: no real remote API, no installation or execution."""
import asyncio
import os

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiagent_marketplace.api import create_router
from aiagent_marketplace.catalog import (
    CatalogError,
    CatalogService,
    FixedSourceHTTP,
    _catalog_id,
    _mcp_item,
)


class FakeTransport:
    def __init__(self, *, pages=None, failures=None):
        self.pages = pages or {}
        self.failures = failures or {}
        self.calls = []

    async def get(self, source, path, params):
        self.calls.append((source, path, dict(params)))
        if source in self.failures:
            raise CatalogError(self.failures[source], 503)
        value = self.pages.get((source, path))
        if callable(value):
            return value(dict(params))
        return value if value is not None else ({"servers": [], "metadata": {}} if source == "mcp" else {"items": [], "nextCursor": None})


def sample_mcp(*, status="active", remote=True):
    return {
        "server": {
            "name": "io.github.sample/github-tools",
            "title": "GitHub Tools",
            "description": "Public MCP tool catalog record",
            "version": "1.10.0",
            "remotes": [{"type": "streamable-http", "url": "https://tools.example/mcp"}] if remote else [],
            "packages": [{"registryType": "npm"}],
        },
        "_meta": {
            "io.modelcontextprotocol.registry/official": {
                "status": status, "isLatest": True
            }
        },
    }


def sample_skill():
    return {
        "slug": "code-review", "ownerHandle": "team-a",
        "displayName": "Code Review", "summary": "Review source code",
        "latestVersion": {"version": "2.0.0"},
    }


def run(coro):
    return asyncio.run(coro)


def test_browse_normalization_and_pagination():
    async def mcp(params):
        assert params["version"] == "latest"
        assert params["limit"] == 2
        return {
            "servers": [sample_mcp(status="deprecated", remote=False)],
            "metadata": {"nextCursor": "encoded:opaque" if not params.get("cursor") else None},
        }

    transport = FakeTransport(pages={
        ("mcp", "/v0.1/servers"): mcp,
        ("clawhub", "/api/v1/skills"): {"items": [sample_skill()], "nextCursor": None},
    })
    service = CatalogService(transport, secret=b"test-signing-secret")
    first = run(service.list_items(limit=4))
    assert len(first["items"]) == 2
    assert {x["kind"] for x in first["items"]} == {"MCP", "SKILL"}
    assert first["items"][0]["transport"] == "unsupported"
    assert first["items"][0]["catalogStatus"] == "deprecated"
    assert all(not x["installable"] for x in first["items"])
    assert first["nextCursor"]
    second = run(service.list_items(limit=4, cursor=first["nextCursor"]))
    assert second["nextCursor"] is None
    assert transport.calls[-1][2]["cursor"] == "encoded:opaque"
    assert transport.calls[-1][2]["version"] == "latest"


def test_clawhub_search_is_one_page_and_not_automatically_trusted():
    transport = FakeTransport(pages={
        ("clawhub", "/api/v1/search"): {
            "results": [
                {"slug": "code-review", "ownerHandle": "team-a", "displayName": "Review", "version": "2.0.0", "summary": "Hello"}
            ]
        }
    })
    service = CatalogService(transport)
    result = run(service.list_items(q="review", kind="SKILL"))
    assert result["items"][0]["url"] == "https://clawhub.ai/team-a/skills/code-review"
    assert result["items"][0]["catalogStatus"] == "public-unverified"
    assert result["nextCursor"] is None
    assert transport.calls[0][2]["nonSuspiciousOnly"] == "true"


def test_one_and_two_character_query_skips_only_mcp():
    transport = FakeTransport(pages={
        ("clawhub", "/api/v1/search"): {"results": [sample_skill()]}
    })
    service = CatalogService(transport)
    result = run(service.list_items(q="AI", source="all"))
    assert len(result["items"]) == 1
    assert result["warnings"] == [{"source": "mcp", "code": "MCP_QUERY_TOO_SHORT"}]
    assert transport.calls[0][0] == "clawhub"
    with pytest.raises(CatalogError) as error:
        run(service.list_items(q="AI", source="mcp"))
    assert error.value.code == "MCP_QUERY_TOO_SHORT"


def test_upstream_failure_is_partial_not_empty_success():
    service = CatalogService(FakeTransport(failures={"mcp": "RATE_LIMITED", "clawhub": "UPSTREAM_UNAVAILABLE"}))
    result = run(service.list_items())
    assert result["partial"] is True
    assert result["items"] == []
    assert len(result["warnings"]) == 2


def test_deleted_and_invalid_rows_filtered():
    assert _mcp_item(sample_mcp(status="deleted")) is None
    assert _mcp_item({"server": {"name": "broken"}}) is None
    assert _mcp_item({"server": {"name": "foo/bar"}, "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}}})["transport"] == "unsupported"


def test_cursor_cannot_be_changed_or_reused_for_another_filter():
    transport = FakeTransport(pages={
        ("mcp", "/v0.1/servers"): {"servers": [sample_mcp()], "metadata": {"nextCursor": "nextpage"}}
    })
    service = CatalogService(transport, secret=b"a-secret-for-cursor")
    first = run(service.list_items(source="mcp"))
    token = first["nextCursor"]
    assert token
    tampered = "Z" + token[1:]
    with pytest.raises(CatalogError) as error:
        run(service.list_items(source="mcp", cursor=tampered))
    assert error.value.code == "INVALID_CURSOR"
    with pytest.raises(CatalogError) as error:
        run(service.list_items(source="mcp", limit=10, cursor=token))
    assert error.value.code == "CURSOR_FILTER_MISMATCH"


def test_detail_validates_owner_and_does_not_mix_slugs():
    transport = FakeTransport(pages={
        ("clawhub", "/api/v1/skills/code-review"): {
            "skill": {"slug": "code-review", "displayName": "Review"},
            "owner": {"handle": "team-b"},
            "latestVersion": {"version": "1.0.0"},
        },
    })
    service = CatalogService(transport)
    with pytest.raises(CatalogError) as error:
        run(service.detail(_catalog_id("clawhub", "team-a/code-review")))
    assert error.value.code == "ITEM_NOT_FOUND"


def test_detail_mcp_returns_same_identity():
    mcp = sample_mcp()
    transport = FakeTransport(pages={
        ("mcp", "/v0.1/servers/io.github.sample%2Fgithub-tools/versions/latest"): mcp
    })
    service = CatalogService(transport)
    result = run(service.detail(_catalog_id("mcp", "io.github.sample/github-tools")))
    assert result["name"] == "GitHub Tools"
    assert result["version"] == "1.10.0"
    assert result["transport"] == "streamable-http"


def test_api_read_only_and_auth_dependency_required():
    transport = FakeTransport(pages={
        ("clawhub", "/api/v1/search"): {"results": [sample_skill()]}
    })
    service = CatalogService(transport)
    app = FastAPI()
    def require_user():
        return "test-user"
    app.include_router(create_router(service, authenticate=require_user))
    with TestClient(app) as client:
        response = client.get("/api/v1/marketplace/items", params={"q": "review", "kind": "SKILL"})
        assert response.status_code == 200
        assert response.json()["items"][0]["kind"] == "SKILL"
        assert client.post("/api/v1/marketplace/installations").status_code == 404
        assert client.get("/api/v1/marketplace/items", params={"source": "mcp", "q": "ab"}).status_code == 422
        assert client.get("/api/v1/marketplace/items", params={"limit": 999}).status_code == 422


def test_direct_transport_requires_explicit_dev_mode(monkeypatch):
    monkeypatch.delenv("AIAGENT_MARKETPLACE_DEV_MODE", raising=False)
    with pytest.raises(RuntimeError):
        FixedSourceHTTP()


def test_direct_transport_never_follows_redirect_or_unknown_url(monkeypatch):
    monkeypatch.setenv("AIAGENT_MARKETPLACE_DEV_MODE", "1")
    seen = []

    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            fetch = FixedSourceHTTP(http)
            with pytest.raises(CatalogError) as e:
                await fetch.get("mcp", "/v0.1/servers", {"version": "latest"})
            assert e.value.code == "UPSTREAM_UNAVAILABLE"
            with pytest.raises(CatalogError) as e:
                await fetch.get("mcp", "//evil.tld/path", {})
            assert e.value.code == "INVALID_PATH"
            with pytest.raises(CatalogError):
                await fetch.get("evil", "/v0.1/servers", {})

    run(check())
    assert len(seen) == 1 and seen[0].startswith("https://registry.modelcontextprotocol.io/")


def test_direct_transport_bounds_size_rate_limit_and_invalid_json(monkeypatch):
    monkeypatch.setenv("AIAGENT_MARKETPLACE_DEV_MODE", "1")
    responses = [
        httpx.Response(429, headers={"Retry-After": "15"}),
        httpx.Response(200, content=b"{" + b"a" * (512 * 1024 + 1) + b"}"),
        httpx.Response(200, text="not json"),
    ]

    def handle(request):
        return responses.pop(0)

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            fetch = FixedSourceHTTP(http)
            for code in ("RATE_LIMITED", "UPSTREAM_RESPONSE_TOO_LARGE", "UPSTREAM_INVALID_JSON"):
                with pytest.raises(CatalogError) as e:
                    await fetch.get("clawhub", "/api/v1/skills", {"limit": 10})
                assert e.value.code == code
                if code == "RATE_LIMITED":
                    assert e.value.retry_after == 15

    run(check())


def test_dev_app_fail_closed_without_flag(monkeypatch):
    monkeypatch.delenv("AIAGENT_MARKETPLACE_DEV_MODE", raising=False)
    # app.py is deliberately not imported on package import in normal runtime.
    import subprocess
    import sys
    proc = subprocess.run(
        [sys.executable, "-c", "import aiagent_marketplace.app"],
        capture_output=True, text=True, env={**os.environ, "AIAGENT_MARKETPLACE_DEV_MODE": "0"}, check=False
    )
    assert proc.returncode != 0
    assert "standalone server is disabled" in proc.stderr
