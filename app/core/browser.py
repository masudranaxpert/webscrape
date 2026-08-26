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

from app.core.config import BROWSER_HEADLESS, MAX_TABS

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
    """

    def __init__(self, max_tabs: int = MAX_TABS, headless: bool = BROWSER_HEADLESS) -> None:
        self._max_tabs = max_tabs
        self._headless = headless
        self._sem: asyncio.Semaphore | None = None
        self._default_browser_ua: str | None = None

    async def start(self) -> None:
        """Pre-warm browser environment and initialize concurrency semaphore."""
        self._sem = asyncio.Semaphore(self._max_tabs)
        try:
            bin_path = cb.ensure_binary()
            logger.info("Using cloakbrowser binary: %s", bin_path)
        except Exception as err:
            logger.warning("cloakbrowser binary check warning: %s", err)

        has_display = bool(os.getenv("DISPLAY"))
        if has_display:
            logger.info("Display detected (%s) - Running in HEADFUL mode", os.getenv("DISPLAY"))

        logger.info("BrowserPool initialized | max_concurrent=%d | headless=%s", self._max_tabs, self._headless)

    async def stop(self) -> None:
        """Clean up browser pool resources."""
        logger.info("BrowserPool stopped")

    async def solve_and_fetch(
        self,
        url: str,
        cf_wait: float = 10.0,
        proxy: str | None = None,
        page_load_state: str = "complete",
        cookies: dict[str, str] | None = None,
    ) -> BrowserResult:
        """Acquire a slot, navigate to target, solve challenge if present, and harvest tokens."""
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_tabs)

        async with self._sem:
            return await self._run_context(url, cf_wait, proxy, page_load_state, cookies)

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
    ) -> BrowserResult:
        t0 = time.monotonic()
        proxy_cfg = _parse_proxy(proxy)

        launch_kwargs: dict[str, Any] = {
            "headless": self._headless,
            "args": [
                FAKE_SHADOW_ARG,
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--lang=en-US,en",
            ],
            "locale": "en-US",
        }
        if proxy_cfg:
            launch_kwargs["proxy"] = proxy_cfg

        context = await cb.launch_context_async(**launch_kwargs)
        harvested: dict[str, str] = {}
        body: str = ""
        final_url: str = url
        status_code = 200
        user_agent: str | None = self._default_browser_ua

        try:
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
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if resp is not None and getattr(resp, "status", None):
                    status_code = resp.status
            except Exception as nav_err:
                logger.warning("cloakbrowser navigation warning on %s: %s", url, nav_err)

            # Settle period for challenge scripts to execute
            await asyncio.sleep(2.5)

            # Check for Cloudflare challenge
            if not await self._is_bypassed(page):
                logger.info("Cloudflare challenge detected, entering solver loop...")
                clicked = False
                max_retries = max(int(cf_wait / 2), 5)

                for _ in range(max_retries):
                    if await self._is_bypassed(page):
                        logger.info("Cloudflare challenge solved successfully")
                        break

                    if not clicked:
                        clicked = await self._click_turnstile_checkbox(page)

                    await asyncio.sleep(1.5)

                # If clearance was issued but page hasn't reloaded automatically
                raw_cookies = await context.cookies()
                cookie_map = {c["name"]: c["value"] for c in raw_cookies}
                if not await self._is_bypassed(page) and "cf_clearance" in cookie_map:
                    logger.info("Clearance cookie present, refreshing page...")
                    with contextlib.suppress(Exception):
                        resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                        if resp is not None and getattr(resp, "status", None):
                            status_code = resp.status
                    await asyncio.sleep(1.0)

            # Final state capture
            bypassed = await self._is_bypassed(page)
            status = 200 if bypassed else (status_code if status_code != 200 else 403)
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
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(context.close()), timeout=10.0)

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
            elapsed_ms=elapsed_ms,
            user_agent=user_agent,
        )


pool = BrowserPool()
