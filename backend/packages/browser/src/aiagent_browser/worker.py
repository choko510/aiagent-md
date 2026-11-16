"""Only this worker imports/owns Playwright BrowserContext and Page objects."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .contracts import BrowserError, BrowserTarget, FormInput

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Locator, Page, Playwright, Route


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
            context: BrowserContext | None = None
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
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass
                raise BrowserError("BROWSER_START_BLOCKED") from exc
            self._pages[session_id] = _PrivatePage(context=context, page=page)

    def _page(self, session_id: str) -> Page:
        obj = self._pages.get(session_id)
        if obj is None or obj.page.is_closed() or self._browser is None or not self._browser.is_connected():
            raise BrowserError("BROWSER_CRASHED")
        return obj.page

    def _classify_error(
        self,
        session_id: str,
        exc: Exception,
        *,
        timeout_code: str = "BROWSER_TIMEOUT",
        default_code: str = "BROWSER_CRASHED",
    ) -> BrowserError:
        obj = self._pages.get(session_id)
        if obj is None or obj.page.is_closed() or self._browser is None or not self._browser.is_connected():
            return BrowserError("BROWSER_CRASHED")
        if isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutError":
            return BrowserError(timeout_code)
        return BrowserError(default_code)

    @staticmethod
    def _locator(page: Page, target: BrowserTarget) -> Locator:
        if target.kind == "role":
            role_arg: Any = target.selector
            if target.role_name is not None:
                return page.get_by_role(role_arg, name=target.role_name, exact=True)
            return page.get_by_role(role_arg)
        if target.kind == "label":
            return page.get_by_label(target.selector, exact=True)
        if target.kind == "test_id":
            return page.get_by_test_id(target.selector)
        return page.locator(target.selector)

    async def _strict_target(self, page: Page, target: BrowserTarget) -> Locator:
        loc = self._locator(page, target)
        count = await loc.count()
        if count == 0:
            raise BrowserError("TARGET_NOT_FOUND")
        if count > 1:
            raise BrowserError("TARGET_AMBIGUOUS")
        if not await loc.is_visible() or not await loc.is_enabled():
            raise BrowserError("TARGET_NOT_INTERACTABLE")
        return loc

    @staticmethod
    async def _has_auth_wall(page: Page) -> bool:
        return await page.locator("[data-auth-wall='true'], input[type='password']").count() > 0

    async def render(self, session_id: str, html: str) -> None:
        async with self._lock:
            try:
                await self._page(session_id).set_content(html, wait_until="domcontentloaded", timeout=10_000)
            except BrowserError:
                raise
            except Exception as exc:
                raise self._classify_error(session_id, exc) from exc

    async def observe(self, session_id: str) -> str:
        async with self._lock:
            try:
                return await self._page(session_id).locator("body").inner_text(timeout=10_000)
            except BrowserError:
                raise
            except Exception as exc:
                raise self._classify_error(session_id, exc) from exc

    async def click(self, session_id: str, target: BrowserTarget, *, timeout_ms: int = 5000) -> str:
        async with self._lock:
            try:
                page = self._page(session_id)
                loc = await self._strict_target(page, target)
                await loc.click(timeout=timeout_ms)
                return "NEEDS_LOGIN" if await self._has_auth_wall(page) else "OK"
            except BrowserError:
                raise
            except Exception as exc:
                raise self._classify_error(
                    session_id, exc, timeout_code="ACTION_TIMEOUT", default_code="ACTION_FAILED"
                ) from exc

    async def submit(
        self,
        session_id: str,
        target: BrowserTarget,
        inputs: tuple[FormInput, ...] = (),
        *,
        timeout_ms: int = 5000,
    ) -> str:
        async with self._lock:
            try:
                page = self._page(session_id)
                for item in inputs:
                    field_loc = await self._strict_target(page, item.target)
                    await field_loc.fill(item.value, timeout=timeout_ms)
                submit_loc = await self._strict_target(page, target)
                await submit_loc.click(timeout=timeout_ms)
                return "NEEDS_LOGIN" if await self._has_auth_wall(page) else "OK"
            except BrowserError:
                raise
            except Exception as exc:
                raise self._classify_error(
                    session_id, exc, timeout_code="ACTION_TIMEOUT", default_code="ACTION_FAILED"
                ) from exc

    async def _close_locked(self, session_id: str) -> None:
        obj = self._pages.pop(session_id, None)
        if obj is not None:
            try:
                await obj.context.close()
            except Exception:
                pass  # crash cleanup: no secrets or raw exception to caller

    async def close(self, session_id: str) -> None:
        async with self._lock:
            await self._close_locked(session_id)

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
        async with self._lock:
            for session_id in tuple(self._pages):
                await self._close_locked(session_id)
            await self._shutdown_browser()
