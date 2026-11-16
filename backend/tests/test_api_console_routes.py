"""API Control Console read/list route isolation and integration tests."""

from __future__ import annotations

import pytest
from aiagent_core import admin
from aiagent_core.commands import Scope, submit_task
from aiagent_core.db import Database
from conftest import PgEnv
from fastapi.testclient import TestClient

pytestmark = pytest.mark.pg


def test_console_list_routes_enforce_owner_isolation(
    pg: PgEnv, dbs: dict[str, Database], tenant: dict[str, str], api: TestClient
) -> None:
    assert api.get("/v1/tasks").status_code == 401
    assert api.get("/v1/browser/sessions").status_code == 401
    assert api.get("/v1/campaigns").status_code == 401
    assert api.get("/v1/watches").status_code == 401

    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    created = submit_task(dbs["api"], scope, "console visibility", profile="browser")

    headers = {"Authorization": f"Bearer {tenant['token']}"}
    tasks = api.get("/v1/tasks", headers=headers)
    assert tasks.status_code == 200
    assert [x["id"] for x in tasks.json()["items"]] == [created["taskId"]]

    other = admin.provision_tenant(pg.owner)
    other_headers = {"Authorization": f"Bearer {other['token']}"}
    assert api.get("/v1/tasks", headers=other_headers).json()["items"] == []
    assert api.get("/v1/browser/sessions", headers=other_headers).json()["items"] == []
    assert api.get("/v1/campaigns", headers=other_headers).json()["items"] == []
    assert api.get("/v1/watches", headers=other_headers).json()["items"] == []

    sse = api.get(f"/v1/tasks/{created['taskId']}/events/stream?follow=true&max_polls=2", headers=headers)
    assert sse.status_code == 200
    assert "task.accepted" in sse.text
