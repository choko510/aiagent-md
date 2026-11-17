"""Only this worker imports/owns Playwright BrowserContext and Page objects."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.cookies import CookieError, SimpleCookie
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

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

    async def start(
        self,
        session_id: str,
        *,
        storage_state: dict[str, Any] | None = None,
        allow_js: bool = False,
    ) -> None:
        async with self._lock:
            if session_id in self._pages:
                raise BrowserError("DUPLICATE_SESSION")
            browser = await self._browser_or_start()
            context: BrowserContext | None = None
            try:
                ctx_kwargs: dict[str, Any] = {
                    "java_script_enabled": allow_js,
                    "service_workers": "block",
                    "accept_downloads": False,
                    "permissions": [],
                }
                if storage_state is not None:
                    ctx_kwargs["storage_state"] = storage_state
                context = await browser.new_context(**ctx_kwargs)

                # Nothing inside the browser may open a raw socket bypassing the gateway/SSRF layer.
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
    async def _detect_challenge(page: Page) -> str | None:
        if await page.locator("[data-auth-wall='true'], input[type='password']").count() > 0:
            return "NEEDS_LOGIN"
        if (
            await page.locator(
                "[data-captcha='true'], iframe[src*='recaptcha'], iframe[src*='hcaptcha'], "
                "iframe[src*='turnstile'], .g-recaptcha, .cf-turnstile"
            ).count()
            > 0
        ):
            return "CAPTCHA_CHALLENGE"
        return None

    async def render(
        self,
        session_id: str,
        html: str,
        *,
        origin_url: str | None = None,
        status: int = 200,
    ) -> str:
        async with self._lock:
            try:
                page = self._page(session_id)
                if origin_url is not None:
                    obj = self._pages[session_id]

                    async def fulfill_main_only(route: Route) -> None:
                        if route.request.url == origin_url and route.request.resource_type == "document":
                            await route.fulfill(status=status, body=html, content_type="text/html; charset=utf-8")
                        else:
                            await route.abort("blockedbyclient")

                    await obj.context.unroute("**/*")
                    await obj.context.route("**/*", fulfill_main_only)
                    try:
                        await page.goto(origin_url, wait_until="domcontentloaded", timeout=10_000)
                    finally:

                        async def reject_network(route: Route) -> None:
                            await route.abort("blockedbyclient")

                        await obj.context.unroute("**/*")
                        await obj.context.route("**/*", reject_network)
                else:
                    await page.set_content(html, wait_until="domcontentloaded", timeout=10_000)
                if status in (401, 403):
                    return "NEEDS_LOGIN"
                challenge = await self._detect_challenge(page)
                return challenge or "OK"
            except BrowserError:
                raise
            except Exception as exc:
                raise self._classify_error(session_id, exc) from exc

    async def export_storage_state(self, session_id: str) -> dict[str, Any]:
        async with self._lock:
            obj = self._pages.get(session_id)
            if obj is None or obj.page.is_closed():
                raise BrowserError("BROWSER_CRASHED")
            state = await obj.context.storage_state()
            return dict(state)

    async def cookies_for_url(self, session_id: str, url: str) -> str:
        async with self._lock:
            obj = self._pages.get(session_id)
            if obj is None or obj.page.is_closed():
                raise BrowserError("BROWSER_CRASHED")
            cookies = await obj.context.cookies([url])
            return "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))

    async def apply_set_cookies(self, session_id: str, origin_url: str, set_cookies: tuple[str, ...]) -> None:
        if not set_cookies:
            return
        async with self._lock:
            obj = self._pages.get(session_id)
            if obj is None or obj.page.is_closed():
                raise BrowserError("BROWSER_CRASHED")
            u = urlsplit(origin_url)
            host = (u.hostname or "").lower()
            is_https = u.scheme.lower() == "https"
            to_add: list[dict[str, Any]] = []
            for raw_hdr in set_cookies:
                jar = SimpleCookie()
                try:
                    jar.load(raw_hdr)
                except CookieError:
                    jar.clear()
                for name, morsel in jar.items():
                    if not name:
                        continue
                    secure = bool(morsel["secure"])
                    if secure and not is_https:
                        continue
                    domain_attr = str(morsel["domain"] or "").strip().lstrip(".").lower()
                    if domain_attr and not (host == domain_attr or host.endswith("." + domain_attr)):
                        continue
                    path_attr = str(morsel["path"] or "").strip() or "/"
                    cookie: dict[str, Any] = {
                        "name": name,
                        "value": morsel.value,
                        "httpOnly": bool(morsel["httponly"]),
                        "secure": secure,
                    }
                    if domain_attr:
                        cookie["domain"] = domain_attr
                        cookie["path"] = path_attr
                    else:
                        cookie["url"] = f"{u.scheme.lower()}://{u.netloc}{path_attr}"
                    max_age = str(morsel["max-age"] or "").strip()
                    expires_raw = str(morsel["expires"] or "").strip()
                    if max_age.isdigit():
                        cookie["expires"] = datetime.now(UTC).timestamp() + int(max_age)
                    elif expires_raw:
                        try:
                            cookie["expires"] = parsedate_to_datetime(expires_raw).timestamp()
                        except Exception:
                            pass
                    to_add.append(cookie)
            if to_add:
                await obj.context.add_cookies(to_add)  # type: ignore[arg-type]

    async def restore_storage_state(
        self,
        session_id: str,
        storage_state: dict[str, Any],
        *,
        allow_js: bool = False,
    ) -> None:
        async with self._lock:
            await self._close_locked(session_id)
        await self.start(session_id, storage_state=storage_state, allow_js=allow_js)

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
                challenge = await self._detect_challenge(page)
                return challenge or "OK"
            except BrowserError:
                raise
            except Exception as exc:
                raise self._classify_error(
                    session_id, exc, timeout_code="ACTION_TIMEOUT", default_code="ACTION_FAILED"
                ) from exc

    async def extract_click_navigation(
        self,
        session_id: str,
        target: BrowserTarget,
        *,
        current_url: str,
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        async with self._lock:
            try:
                page = self._page(session_id)
                loc = await self._strict_target(page, target)
                info = await loc.evaluate(
                    """el => {
                        const anchor = el.closest('a[href]');
                        if (!anchor) return { navUrl: null };
                        const raw = (anchor.getAttribute('href') || '').trim();
                        if (!raw || raw.startsWith('#')) return { navUrl: null };
                        return { navUrl: raw };
                    }""",
                    timeout=timeout_ms,
                )
                raw = info.get("navUrl") if isinstance(info, dict) else None
                if not isinstance(raw, str) or not raw:
                    return {"navUrl": None}
                return {"navUrl": urljoin(current_url, raw)}
            except BrowserError:
                raise
            except Exception as exc:
                raise self._classify_error(
                    session_id, exc, timeout_code="ACTION_TIMEOUT", default_code="ACTION_FAILED"
                ) from exc

    async def extract_form_submission(
        self,
        session_id: str,
        target: BrowserTarget,
        inputs: tuple[FormInput, ...] = (),
        *,
        current_url: str,
        timeout_ms: int = 5000,
        apply_inputs: bool = True,
    ) -> dict[str, Any]:
        async with self._lock:
            handles: list[Any] = []
            try:
                page = self._page(session_id)
                if apply_inputs:
                    for item in inputs:
                        field_loc = await self._strict_target(page, item.target)
                        await field_loc.fill(item.value, timeout=timeout_ms)
                    submit_loc = await self._strict_target(page, target)
                    info = await submit_loc.evaluate(
                        """el => {
                            const form = el.form || el.closest('form');
                            if (!form) return { hasForm: false };
                            const fields = [];
                            for (const ctrl of Array.from(form.elements)) {
                                if (!ctrl.name || ctrl.disabled) continue;
                                const typ = (ctrl.type || '').toLowerCase();
                                if ((typ === 'checkbox' || typ === 'radio') && !ctrl.checked) continue;
                                if (typ === 'submit' || typ === 'button') {
                                    if (ctrl !== el) continue;
                                }
                                fields.push([String(ctrl.name), String(ctrl.value ?? '')]);
                            }
                            return {
                                hasForm: true,
                                action: String(form.getAttribute('action') || ''),
                                method: String(form.getAttribute('method') || 'GET').toUpperCase(),
                                fields: fields,
                            };
                        }""",
                        timeout=timeout_ms,
                    )
                else:
                    # Non-mutating DOM preview: resolve elements strictly without writing .fill() values.
                    override_pairs: list[tuple[Any, str]] = []
                    for item in inputs:
                        field_loc = await self._strict_target(page, item.target)
                        fh = await field_loc.element_handle(timeout=timeout_ms)
                        if fh is None:
                            raise BrowserError("ACTION_FAILED")
                        handles.append(fh)
                        override_pairs.append((fh, item.value))
                    submit_loc = await self._strict_target(page, target)
                    submit_handle = await submit_loc.element_handle(timeout=timeout_ms)
                    if submit_handle is None:
                        raise BrowserError("ACTION_FAILED")
                    handles.append(submit_handle)
                    info = await asyncio.wait_for(
                        page.evaluate(
                            """([submitEl, overrides]) => {
                                const form = submitEl.form || submitEl.closest('form');
                                if (!form) return { hasForm: false };
                                const ov = new Map();
                                for (const pair of overrides) {
                                    ov.set(pair[0], String(pair[1] ?? ''));
                                }
                                const fields = [];
                                for (const ctrl of Array.from(form.elements)) {
                                    if (!ctrl.name || ctrl.disabled) continue;
                                    const typ = (ctrl.type || '').toLowerCase();
                                    if ((typ === 'checkbox' || typ === 'radio') && !ctrl.checked) continue;
                                    if (typ === 'submit' || typ === 'button') {
                                        if (ctrl !== submitEl) continue;
                                    }
                                    const val = ov.has(ctrl) ? ov.get(ctrl) : String(ctrl.value ?? '');
                                    fields.push([String(ctrl.name), val]);
                                }
                                return {
                                    hasForm: true,
                                    action: String(form.getAttribute('action') || ''),
                                    method: String(form.getAttribute('method') || 'GET').toUpperCase(),
                                    fields: fields,
                                };
                            }""",
                            [submit_handle, [[fh, val] for fh, val in override_pairs]],
                        ),
                        timeout=timeout_ms / 1000.0,
                    )
                if not isinstance(info, dict) or not info.get("hasForm"):
                    return {"hasForm": False}
                raw_action = str(info.get("action") or "").strip()
                action_url = urljoin(current_url, raw_action) if raw_action else current_url
                method = str(info.get("method") or "GET").upper()
                if method not in ("GET", "POST"):
                    method = "GET"
                pairs = [
                    (str(pair[0]), str(pair[1]))
                    for pair in (info.get("fields") or [])
                    if isinstance(pair, (list, tuple)) and len(pair) == 2
                ]
                return {"hasForm": True, "action": action_url, "method": method, "fields": pairs}
            except BrowserError:
                raise
            except Exception as exc:
                raise self._classify_error(
                    session_id, exc, timeout_code="ACTION_TIMEOUT", default_code="ACTION_FAILED"
                ) from exc
            finally:
                for fh in handles:
                    try:
                        await fh.dispose()
                    except Exception:
                        pass

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
                challenge = await self._detect_challenge(page)
                return challenge or "OK"
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
