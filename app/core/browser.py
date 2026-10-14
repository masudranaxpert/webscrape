"""Stealth browser automation engine backed by CloakBrowser with automated Turnstile solving."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import time
from typing import Any
from urllib.parse import urlparse

import cloakbrowser as cb

from app.core.config import (
    BROWSER_HEADLESS,
    BROWSER_MAX_LIFETIME_SEC,
    MAX_TABS,
)

logger = logging.getLogger(__name__)

# Native closed-shadow-root access via patched Chromium
FAKE_SHADOW_ARG = "--enable-blink-features=FakeShadowRoot"

# Recursive shadow DOM walker to locate Turnstile checkbox within challenge iframes
_FIND_CHECKBOX_JS = """() => {
    function find(root){
        if(!root) return null;
        const direct = root.querySelector && root.querySelector('input[type=checkbox]');
        if(direct) return direct;
        for(const el of (root.querySelectorAll ? root.querySelectorAll('*') : [])){
            const sr = el.fakeShadowRoot || el.shadowRoot;
            if(sr){ const r = find(sr); if(r) return r; }
        }
        return null;
    }
    const cb = find(document);
    if(!cb) return {found:false};
    const r = cb.getBoundingClientRect();
    return {found:true, checked:cb.checked, x:r.x+r.width/2, y:r.y+r.height/2, w:r.width};
}"""

# Detects an embedded Turnstile widget (not a full-page interstitial) and whether it has issued a token
_TURNSTILE_STATE_JS = """() => {
    const widgets = document.querySelectorAll(
        '.cf-turnstile, iframe[src*="challenges.cloudflare.com"][src*="turnstile"]'
    );
    if(!widgets.length) return {present:false, solved:false};
    const inputs = document.querySelectorAll(
        'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
    );
    const solved = inputs.length > 0 && Array.from(inputs).every(i => i.value && i.value.length > 0);
    return {present:true, solved:solved};
}"""

_BLOCK_MARKERS = (
    "you have been blocked",
    "sorry, you have been blocked",
    "error 1020",
    "access denied",
)


@dataclasses.dataclass
class BrowserResult:
    status_code: int
    body: str
    cookies: dict[str, str]
    final_url: str
    headers: dict[str, str] = dataclasses.field(default_factory=dict)
    elapsed_ms: int = 0
    cf_bypass_attempted: bool = True
    user_agent: str | None = None
    notes: list[str] = dataclasses.field(default_factory=list)


def _parse_proxy(proxy: str | None) -> dict[str, str] | None:
    """Parse proxy URI into Playwright-compatible proxy dictionary."""
    if not proxy or not isinstance(proxy, str):
        return None
    try:
        parsed = urlparse(proxy.strip())
        if not parsed.hostname or not parsed.port:
            return None
        cfg = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
        if parsed.username and parsed.password:
            cfg["username"] = parsed.username
            cfg["password"] = parsed.password
        return cfg
    except Exception:
        return None


class BrowserPool:
    """
    Manages concurrent stealth browser contexts and automated Cloudflare challenge solving.
    Includes automated worker recycling to eliminate stale HTTP disk cache and memory bloat.
    """

    def __init__(
        self,
        max_tabs: int = MAX_TABS,
        headless: bool = BROWSER_HEADLESS,
        max_lifetime_sec: int = BROWSER_MAX_LIFETIME_SEC,
    ) -> None:
        self._max_tabs = max_tabs
        self._headless = headless
        self._max_lifetime_sec = max_lifetime_sec
        self._sem: asyncio.Semaphore | None = None
        self._launch_lock: asyncio.Lock | None = None
        self._browser: Any | None = None
        self._browser_created_at: float = 0.0
        self._active_contexts: int = 0
        self._retiring_browsers: set[Any] = set()
        self._default_browser_ua: str | None = None

    async def start(self) -> None:
        """Pre-warm browser environment and initialize concurrency semaphore."""
        self._sem = asyncio.Semaphore(self._max_tabs)
        self._launch_lock = asyncio.Lock()
        try:
            bin_path = cb.ensure_binary()
            logger.info("Using cloakbrowser binary: %s", bin_path)
        except Exception as err:
            logger.warning("cloakbrowser binary check warning: %s", err)

        has_display = bool(os.getenv("DISPLAY"))
        if has_display and not self._headless:
            logger.info("Display detected (%s) - Running in HEADFUL mode", os.getenv("DISPLAY"))

        logger.info(
            "BrowserPool initialized | max_concurrent=%d | headless=%s | max_lifetime=%ds",
            self._max_tabs,
            self._headless,
            self._max_lifetime_sec,
        )

    def _should_recycle(self) -> bool:
        """Check if active browser worker has exceeded max lifetime TTL."""
        if self._browser is None:
            return False
        return (time.monotonic() - self._browser_created_at) >= self._max_lifetime_sec

    async def _close_browser_safely(self, browser: Any) -> None:
        """Safely close a browser instance and release process resources."""
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
            self._retiring_browsers.discard(browser)

    async def _get_browser(self, launch_kwargs: dict[str, Any]) -> Any:
        """Acquire healthy browser instance, gracefully retiring aged workers."""
        if self._launch_lock is None:
            self._launch_lock = asyncio.Lock()

        async with self._launch_lock:
            if self._should_recycle():
                age = time.monotonic() - self._browser_created_at
                logger.info(
                    "Retiring browser worker (age=%.1fs, active_contexts=%d) for graceful recycle",
                    age,
                    self._active_contexts,
                )
                old_browser = self._browser
                self._browser = None
                if old_browser:
                    if self._active_contexts == 0:
                        asyncio.create_task(self._close_browser_safely(old_browser))
                    else:
                        self._retiring_browsers.add(old_browser)

            if self._browser is None:
                self._browser = await cb.launch_async(**launch_kwargs)
                self._browser_created_at = time.monotonic()

            return self._browser

    async def stop(self) -> None:
        """Clean up all active and retiring browser resources."""
        if self._launch_lock is None:
            self._launch_lock = asyncio.Lock()
        async with self._launch_lock:
            if self._browser:
                with contextlib.suppress(Exception):
                    await self._browser.close()
                self._browser = None
            for retiring in list(self._retiring_browsers):
                with contextlib.suppress(Exception):
                    await retiring.close()
            self._retiring_browsers.clear()
            self._active_contexts = 0
        logger.info("BrowserPool stopped")

    async def solve_and_fetch(
        self,
        url: str,
        cf_wait: float = 10.0,
        proxy: str | None = None,
        page_load_state: str = "complete",
        cookies: dict[str, str] | None = None,
        wait_for_selector: str | None = None,
    ) -> BrowserResult:
        """Acquire a slot, navigate to target, solve challenge if present, and harvest tokens."""
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_tabs)
        if self._launch_lock is None:
            self._launch_lock = asyncio.Lock()

        async with self._sem:
            return await self._run_context(url, cf_wait, proxy, page_load_state, cookies, wait_for_selector)

    async def _is_bypassed(self, page: Any) -> bool:
        """Check if challenge wall has cleared."""
        try:
            title = (await page.title()).lower()
            if "just a moment" in title or "attention required" in title or "security check" in title:
                return False

            html_content = (await page.content()).lower()
            if "please complete the captcha" in html_content or 'id="challenge-running"' in html_content:
                return False

            if any(marker in html_content for marker in _BLOCK_MARKERS):
                return False

            return True
        except Exception:
            return False

    async def _turnstile_pending(self, page: Any) -> bool:
        """True when an embedded Turnstile widget exists on the page and has not yet issued a token."""
        try:
            state = await page.evaluate(_TURNSTILE_STATE_JS)
            return bool(state and state.get("present") and not state.get("solved"))
        except Exception:
            return False

    async def _has_selector(self, page: Any, selector: str) -> bool:
        """True when the CSS selector currently matches an element in the live DOM."""
        try:
            return await page.query_selector(selector) is not None
        except Exception:
            return False

    async def _is_ready(self, page: Any, selector: str | None) -> bool:
        """Wall cleared, no pending embedded Turnstile, and (if requested) target selector rendered."""
        if not await self._is_bypassed(page):
            return False
        if await self._turnstile_pending(page):
            return False
        return selector is None or await self._has_selector(page, selector)

    async def _click_turnstile_checkbox(self, page: Any) -> bool:
        """Find Turnstile checkbox inside shadow DOM and click it via native mouse event."""
        cf_frames = [f for f in page.frames if "challenges.cloudflare" in (f.url or "")]
        for frame in cf_frames:
            try:
                info = await frame.evaluate(_FIND_CHECKBOX_JS)
                if not info or not info.get("found") or info.get("w", 0) <= 0 or info.get("checked"):
                    continue

                frame_el = await frame.frame_element()
                box = await frame_el.bounding_box()
                if not box:
                    continue

                click_x = box["x"] + info["x"]
                click_y = box["y"] + info["y"]
                logger.info("Turnstile checkbox located: clicking at (%.1f, %.1f)", click_x, click_y)
                await page.mouse.click(click_x, click_y)

                await asyncio.sleep(0.5)
                after = await frame.evaluate(_FIND_CHECKBOX_JS)
                if not after.get("found") or after.get("checked"):
                    logger.info("Turnstile checkbox click verified")
                    return True
            except Exception as e:
                logger.debug("Turnstile frame interaction exception: %s", e)
        return False

    async def _run_context(
        self,
        url: str,
        cf_wait: float,
        proxy: str | None,
        page_load_state: str,
        cookies: dict[str, str] | None,
        wait_for_selector: str | None = None,
    ) -> BrowserResult:
        t0 = time.monotonic()
        proxy_cfg = _parse_proxy(proxy)
        # Map API load-state names onto Playwright wait_until values
        load_state = {"complete": "load", "interactive": "domcontentloaded"}.get(
            (page_load_state or "").lower(), "domcontentloaded"
        )

        launch_kwargs: dict[str, Any] = {
            "headless": self._headless,
            "args": [
                FAKE_SHADOW_ARG,
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--lang=en-US,en",
                "--no-crashpad",
                "--disable-crash-reporter",
            ]
        }

        context = None
        selector = (wait_for_selector or "").strip() or None
        notes: list[str] = []
        harvested: dict[str, str] = {}
        body: str = ""
        final_url: str = url
        status_code = 200
        status: int = 200
        resp_headers: dict[str, str] = {}
        user_agent: str | None = self._default_browser_ua

        try:
            browser = await self._get_browser(launch_kwargs)
            if self._launch_lock is not None:
                async with self._launch_lock:
                    self._active_contexts += 1

            context_kwargs: dict[str, Any] = {"locale": "en-US"}
            if proxy_cfg:
                context_kwargs["proxy"] = proxy_cfg

            context = await browser.new_context(**context_kwargs)

            page = context.pages[0] if context.pages else await context.new_page()
            timeout_ms = max(int(cf_wait * 1000), 30000)
            page.set_default_timeout(timeout_ms)
            page.set_default_navigation_timeout(timeout_ms)

            # Pre-inject cached cookies into context
            if cookies:
                cookie_list = [
                    {"name": k, "value": v, "url": url}
                    for k, v in cookies.items()
                ]
                with contextlib.suppress(Exception):
                    await context.add_cookies(cookie_list)

            logger.info("cloakbrowser: navigating -> %s (timeout=%.1fs)", url, timeout_ms / 1000)
            try:
                resp = await page.goto(url, wait_until=load_state, timeout=timeout_ms)
                if resp is not None and getattr(resp, "status", None):
                    status_code = resp.status
                    resp_headers = dict(getattr(resp, "headers", None) or {})
            except Exception as nav_err:
                logger.warning("cloakbrowser navigation warning on %s: %s", url, nav_err)

            # Settle period for challenge scripts to execute
            await asyncio.sleep(2.5)
            # Check for interstitial wall, embedded Turnstile widget, or a not-yet-rendered target selector
            wall = not await self._is_bypassed(page)
            embedded = await self._turnstile_pending(page)
            missing = selector is not None and not await self._has_selector(page, selector)

            if wall or embedded or missing:
                reasons = [
                    r for r, on in (
                        ("interstitial wall", wall),
                        ("embedded Turnstile pending", embedded),
                        (f"selector '{selector}' not rendered", missing),
                    ) if on
                ]
                logger.info("Solver loop entered: %s", ", ".join(reasons))
                notes.append(f"Solver triggered: {', '.join(reasons)}")

                clicked = False
                deadline = time.monotonic() + cf_wait
                while not await self._is_ready(page, selector):
                    if time.monotonic() >= deadline:
                        break
                    if not clicked:
                        clicked = await self._click_turnstile_checkbox(page)
                    await asyncio.sleep(1.0)

                # If clearance was issued but page hasn't reloaded automatically
                raw_cookies = await context.cookies()
                cookie_map = {c["name"]: c["value"] for c in raw_cookies}
                if not await self._is_bypassed(page) and "cf_clearance" in cookie_map:
                    logger.info("Clearance cookie present, refreshing page...")
                    with contextlib.suppress(Exception):
                        resp = await page.goto(url, wait_until=load_state, timeout=15000)
                        if resp is not None and getattr(resp, "status", None):
                            status_code = resp.status
                            resp_headers = dict(getattr(resp, "headers", None) or {})
                    await asyncio.sleep(1.0)

                if await self._is_ready(page, selector):
                    notes.append("Solver finished: page ready")
                    logger.info("Challenge/widget resolved successfully")
                else:
                    if await self._turnstile_pending(page):
                        notes.append("Embedded Turnstile still unresolved at timeout")

            # Final state capture
            bypassed = await self._is_bypassed(page)
            status = 200 if bypassed else (status_code if status_code != 200 else 403)
            if bypassed and selector and not await self._has_selector(page, selector):
                status = 408
                notes.append(f"Timed out waiting for selector '{selector}' (cf_wait={cf_wait}s)")
                logger.warning("Selector '%s' never appeared on %s", selector, url)
            final_url = page.url or url

            with contextlib.suppress(Exception):
                body = await page.content()

            raw_cookies = await context.cookies()
            harvested = {c["name"]: c["value"] for c in raw_cookies}

            with contextlib.suppress(Exception):
                ua = await page.evaluate("navigator.userAgent")
                if ua and isinstance(ua, str) and len(ua) > 10:
                    user_agent = ua
                    if not self._default_browser_ua:
                        self._default_browser_ua = ua

        finally:
            if context is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(context.close()), timeout=5.0)

            if self._launch_lock is not None:
                async with self._launch_lock:
                    self._active_contexts = max(0, self._active_contexts - 1)
                    if self._active_contexts == 0 and self._retiring_browsers:
                        for retiring in list(self._retiring_browsers):
                            asyncio.create_task(self._close_browser_safely(retiring))

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.info(
                "cloakbrowser: done -> %s | status=%d | cookies=%d | elapsed=%dms",
                final_url, status, len(harvested), elapsed_ms,
            )

        return BrowserResult(
            status_code=status,
            body=body,
            cookies=harvested,
            final_url=final_url,
            headers=resp_headers,
            elapsed_ms=elapsed_ms,
            user_agent=user_agent,
            notes=notes,
        )


pool = BrowserPool()
