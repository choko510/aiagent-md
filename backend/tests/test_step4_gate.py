"""STEP 4 shared admission's fail-closed regression tests (no real external effects)."""

from __future__ import annotations

import pytest
from aiagent_api.app import create_app
from aiagent_core.step4_gate import (
    FEATURES,
    IntegrationBlocked,
    capability_snapshot,
    is_reserved_tool,
    require_step4_admission,
)
from aiagent_domain.tools import PROFILE_TOOLS, TOOLS
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    "tool",
    [
        "browser.navigate",
        "context.compact",
        "memory.retrieve",
        "development.git_push",
        "automation.campaign.start",
        "device.click",
        "step4.spawn",
        "BROWSER.navigate",
        "device",
        "step4",
        None,
    ],
)
def test_unintegrated_tool_cannot_issue_authority(tool: str | None) -> None:
    assert is_reserved_tool(tool) is True  # type: ignore[arg-type]
    with pytest.raises(IntegrationBlocked, match="STEP4_NOT_INTEGRATED"):
        require_step4_admission(tool)  # type: ignore[arg-type]


def test_existing_step3_tool_registry_is_unchanged() -> None:
    assert set(TOOLS) == {"search", "send_message", "sandbox_exec"}
    assert not any(is_reserved_tool(key) for key in TOOLS)
    assert not any(is_reserved_tool(key) for vals in PROFILE_TOOLS.values() for key in vals)


def test_no_environment_flag_can_grant_step4_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AIAGENT_STEP4_ENABLE", "AIAGENT_PROFILE", "AIAGENT_ALLOW_DEVICE"):
        monkeypatch.setenv(key, "production")
    with pytest.raises(IntegrationBlocked):
        require_step4_admission("device.build")


class FakeScopeDatabase:
    """Only stubs existing token lookup; it cannot enqueue, execute or grant a permit."""

    def one(self, sql: str, params: tuple[str, ...]) -> dict[str, str] | None:
        assert "resolve_api_token" in sql
        assert len(params) == 1 and len(params[0]) == 64
        return {"tenant_id": "tenant-1", "user_id": "owner-1", "workspace_id": "ws-1"}


def test_capabilities_requires_authenticated_scope() -> None:
    with TestClient(create_app(FakeScopeDatabase())) as client:  # type: ignore[arg-type]
        unauth = client.get("/v1/step4/capabilities")
        assert unauth.status_code == 401
        response = client.get("/v1/step4/capabilities", headers={"Authorization": "Bearer test"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["schemaVersion"] == "1.0"
        assert payload["capabilities"] == capability_snapshot()
        assert {x["component"] for x in payload["capabilities"]} == set(FEATURES)
        assert all(x["integrationState"] == "INTEGRATION_PENDING" for x in payload["capabilities"])
        assert all(x["externalActionsEnabled"] is False for x in payload["capabilities"])
        assert all(x["hostVerified"] is False for x in payload["capabilities"])
        assert all(x["productionReady"] is False for x in payload["capabilities"])


def test_step3_tools_are_not_affected() -> None:
    for key in ("search", "send_message", "sandbox_exec"):
        assert is_reserved_tool(key) is False
        require_step4_admission(key)
