from __future__ import annotations

from aiagent_mcp_adapter.errors import ErrorCode, McpError, classify_http_status
from aiagent_mcp_adapter.sdk_transport import _uncertain


def test_trusted_pre_send_denial_remains_pre_send() -> None:
    error = McpError(ErrorCode.EGRESS_PRE_SEND_DENIED, "proxy_denied:PRE_SEND_DENIED")
    assert _uncertain(error).effect_uncertain is False


def test_post_send_origin_denial_is_not_treated_as_pre_send() -> None:
    for status in (401, 403, 429, 500, 418):
        error = classify_http_status(status)
        assert error.effect_uncertain is True
        assert _uncertain(error).effect_uncertain is True


def test_redirect_denial_is_uncertain_but_connect_failures_are_retryable() -> None:
    redirect = McpError(ErrorCode.EGRESS_DENIED, "redirect_not_followed")
    assert _uncertain(redirect).effect_uncertain is True
    connect = McpError(ErrorCode.CONNECT_FAILED, "connect_failed")
    assert connect.retryable is True
    assert connect.effect_uncertain is False
