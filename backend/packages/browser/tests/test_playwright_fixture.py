"""Optional real Headless Chromium; if binary is unavailable the test is NOT_RUN, not PASS."""

from __future__ import annotations

import pytest

from aiagent_browser import PlaywrightBrowserWorker


@pytest.mark.asyncio
async def test_chromium_contexts_and_forbidden_subresources():
    worker = PlaywrightBrowserWorker()
    try:
        try:
            await worker.start("session-A")
        except Exception as exc:
            if getattr(exc, "code", None) == "BROWSER_START_BLOCKED":
                pytest.skip("NOT_RUN: sandboxed Chromium binary/prerequisites unavailable")
            raise
        await worker.start("session-B")
        await worker.render("session-A", "<h1>A</h1><input type='password' value='SECRET-A'>")
        await worker.render("session-B", "<h1>B</h1><img src='http://169.254.169.254/latest/meta-data/'>")
        assert await worker.observe("session-A") == "A"
        assert await worker.observe("session-B") == "B"
        await worker.close("session-A")
        assert await worker.observe("session-B") == "B"
    finally:
        await worker.shutdown()
