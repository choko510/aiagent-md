"""Optional real Headless Chromium; if binary is unavailable the test is NOT_RUN, not PASS."""

from __future__ import annotations

import os

import pytest
from aiagent_browser import BrowserError, BrowserTarget, FormInput, PlaywrightBrowserWorker


@pytest.mark.asyncio
async def test_chromium_contexts_and_forbidden_subresources():
    worker = PlaywrightBrowserWorker()
    try:
        try:
            await worker.start("session-A")
        except Exception as exc:
            if (
                getattr(exc, "code", None) == "BROWSER_START_BLOCKED"
                and os.environ.get("AIAGENT_REQUIRE_CHROMIUM") != "1"
            ):
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


@pytest.mark.asyncio
async def test_chromium_strict_click_submit_and_ambiguity_guards():
    worker = PlaywrightBrowserWorker()
    try:
        try:
            await worker.start("session-action")
        except Exception as exc:
            if (
                getattr(exc, "code", None) == "BROWSER_START_BLOCKED"
                and os.environ.get("AIAGENT_REQUIRE_CHROMIUM") != "1"
            ):
                pytest.skip("NOT_RUN: sandboxed Chromium binary/prerequisites unavailable")
            raise
        await worker.render(
            "session-action",
            "<details><summary data-testid='toggle'>Show Info</summary><p>Revealed Text</p></details>"
            "<label for='note'>Order Note</label><input id='note' type='text' value=''>"
            "<button data-testid='dup'>Same</button><button data-testid='dup'>Same</button>"
            "<button data-testid='hidden' style='display:none'>Hidden</button>"
            "<form action='#submitted' method='get'><button data-testid='send' type='submit'>Confirm</button></form>",
        )
        with pytest.raises(BrowserError, match="TARGET_NOT_FOUND"):
            await worker.click("session-action", BrowserTarget("test_id", "missing"))
        with pytest.raises(BrowserError, match="TARGET_AMBIGUOUS"):
            await worker.click("session-action", BrowserTarget("test_id", "dup"))
        with pytest.raises(BrowserError, match="TARGET_NOT_INTERACTABLE"):
            await worker.click("session-action", BrowserTarget("test_id", "hidden"))

        status = await worker.click("session-action", BrowserTarget("test_id", "toggle"))
        assert status == "OK"
        assert "Revealed Text" in await worker.observe("session-action")

        sub_status = await worker.submit(
            "session-action",
            BrowserTarget("test_id", "send"),
            inputs=(FormInput(BrowserTarget("label", "Order Note"), "qty=2"),),
        )
        assert sub_status == "OK"

        await worker.render(
            "session-action",
            "<form data-auth-wall='true'><input type='password'><button data-testid='go'>Login</button></form>",
        )
        assert await worker.click("session-action", BrowserTarget("test_id", "go")) == "NEEDS_LOGIN"
    finally:
        await worker.shutdown()
