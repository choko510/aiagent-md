"""Only this worker imports/owns Playwright BrowserContext and Page objects."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .contracts import BrowserError

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright, Route


@dataclass(slots=True)
class _PrivatePage:
    context: BrowserContext
    page: Page


class PlaywrightBrowserWorker:
    """Offline fixture renderer, not a production egress transport or privileged CDP broker."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._pages: dict[str, _PrivatePage] = {}
        self._lock = asyncio.Lock()

    async def _browser_or_start(self) -> Browser:
        # Import remains private so callers never obtain Page/CDP handles.
        from playwright.async_api import async_playwright

        if self._browser is not None and self._browser.is_connected():
            return self._browser
        if self._browser is not None or self._playwright is not None:
            # A crashed CDP connection is discarded; no storage state is restored.
            await self._shutdown_browser()
        try:
            self._playwright = await async_playwright().start()
            # No --no-sandbox fallback. An unsandboxed browser is NOT an acceptable production profile.
            self._browser = await self._playwright.chromium.launch(headless=True, chromium_sandbox=True)
        except Exception as exc:
            await self._shutdown_browser()
            raise BrowserError("BROWSER_START_BLOCKED") from exc
        return self._browser

    async def start(self, session_id: str) -> None:
        async with self._lock:
            if session_id in self._pages:
                raise BrowserError("DUPLICATE_SESSION")
            browser = await self._browser_or_start()
            try:
                context = await browser.new_context(
                    java_script_enabled=False,
                    service_workers="block",
                    accept_downloads=False,
                    permissions=[],
                )

                # Nothing in fixture HTML may open a socket (including images, fonts, redirects).
                async def reject_network(route: Route) -> None:
                    await route.abort("blockedbyclient")

                await context.route("**/*", reject_network)
                page = await context.new_page()
            except Exception as exc:
                if "context" in locals():
                    await context.close()
                raise BrowserError("BROWSER_START_BLOCKED") from exc
            self._pages[session_id] = _PrivatePage(context=context, page=page)

    def _page(self, session_id: str) -> Page:
        obj = self._pages.get(session_id)
        if obj is None or obj.page.is_closed() or self._browser is None or not self._browser.is_connected():
            raise BrowserError("BROWSER_CRASHED")
        return obj.page

    async def render(self, session_id: str, html: str) -> None:
        try:
            await self._page(session_id).set_content(html, wait_until="domcontentloaded", timeout=10_000)
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError("BROWSER_CRASHED") from exc

    async def observe(self, session_id: str) -> str:
        try:
            return await self._page(session_id).locator("body").inner_text(timeout=10_000)
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError("BROWSER_CRASHED") from exc

    async def close(self, session_id: str) -> None:
        async with self._lock:
            obj = self._pages.pop(session_id, None)
            if obj is not None:
                try:
                    await obj.context.close()
                except Exception:
                    pass  # crash cleanup: no secrets or raw exception to caller

    async def _shutdown_browser(self) -> None:
        browser, pw = self._browser, self._playwright
        self._browser, self._playwright = None, None
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass

    async def shutdown(self) -> None:
        for session_id in tuple(self._pages):
            await self.close(session_id)
        await self._shutdown_browser()
