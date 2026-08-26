"""Persistent Chrome browser pool with concurrency control and Cloudflare bypass."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from urllib.parse import urlparse
import os
import urllib.request
import json

# --- Monkey patch pydoll to fix aiohttp ssl:default bug on localhost ---
import pydoll.connection.connection_handler

async def _patched_resolve_ws_address(self):
    if self._ws_address:
        return self._ws_address
    if not self._page_id:
        def fetch():
            with urllib.request.urlopen(f'http://127.0.0.1:{self._connection_port}/json/version', timeout=2) as resp:
                return json.loads(resp.read())['webSocketDebuggerUrl'].replace("localhost", "127.0.0.1")
        return await asyncio.to_thread(fetch)
    return f'ws://127.0.0.1:{self._connection_port}/devtools/page/{self._page_id}'

pydoll.connection.connection_handler.ConnectionHandler._resolve_ws_address = _patched_resolve_ws_address
# -----------------------------------------------------------------------

from pydoll.browser.chromium import Chrome
from pydoll.browser.options import ChromiumOptions
from pydoll.constants import PageLoadState
from pydoll.protocol.network.events import NetworkEvent

from app.core.config import BROWSER_HEADLESS, DEBUG, MAX_TABS

logger = logging.getLogger(__name__)

_PAGE_LOAD_MAP = {
    "complete": PageLoadState.COMPLETE,
    "interactive": PageLoadState.INTERACTIVE,
    "loading": PageLoadState.LOADING,
}

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
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


def _build_options(headless: bool = True) -> ChromiumOptions:
    options = ChromiumOptions()
    
    try:
        import cloakbrowser
        chrome_bin = cloakbrowser.ensure_binary()
        logger.info(f"Using cloakbrowser binary: {chrome_bin}")
        options.binary_location = chrome_bin
    except Exception as e:
        chrome_bin = os.getenv("CHROME_BIN", "/usr/bin/google-chrome")
        logger.warning(f"cloakbrowser not found ({e}), using {chrome_bin}")
        options.binary_location = chrome_bin

    has_display = bool(os.getenv("DISPLAY"))
    if has_display:
        logger.info(f"Display detected ({os.getenv('DISPLAY')}) - Running in HEADFUL mode")
    else:
        if headless:
            options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--enable-blink-features=FakeShadowRoot")
    options.add_argument("--lang=en-US,en")
    options.add_argument(f"--user-agent={_DEFAULT_UA}")

    options.start_timeout = 30
    options.block_notifications = True
    options.block_popups = True
    options.webrtc_leak_protection = True
    options.password_manager_enabled = False
    options.page_load_state = PageLoadState.COMPLETE

    return options


class BrowserPool:
    """
    Persistent Chromium process managing concurrent worker tabs.
    """

    def __init__(self, max_tabs: int = MAX_TABS, headless: bool = BROWSER_HEADLESS) -> None:
        self._max_tabs = max_tabs
        self._headless = headless
        self._chrome: Chrome | None = None
        self._sem: asyncio.Semaphore | None = None

    async def start(self) -> None:
        """Launch background Chrome process during server startup."""
        options = _build_options(headless=self._headless)
        self._chrome = Chrome(options=options)
        await self._chrome.__aenter__()
        await self._chrome.start()
        self._sem = asyncio.Semaphore(self._max_tabs)
        logger.info("BrowserPool initialized | max_tabs=%d | headless=%s", self._max_tabs, self._headless)

    async def stop(self) -> None:
        """Terminate Chrome process during server shutdown."""
        if self._chrome:
            with contextlib.suppress(Exception):
                await self._chrome.stop()
            with contextlib.suppress(Exception):
                await self._chrome.__aexit__(None, None, None)
            self._chrome = None
        logger.info("BrowserPool stopped")

    async def solve_and_fetch(
        self,
        url: str,
        cf_wait: float = 10.0,
        proxy: str | None = None,
        page_load_state: str = "complete",
        cookies: dict[str, str] | None = None,
    ) -> BrowserResult:
        """Acquire a tab slot, bypass challenge if present, harvest cookies and return result."""
        if self._chrome is None or self._sem is None:
            raise RuntimeError("BrowserPool is not running")

        async with self._sem:
            return await self._run_tab(url, cf_wait, proxy, page_load_state, cookies)

    async def _run_tab(
        self,
        url: str,
        cf_wait: float,
        proxy: str | None,
        page_load_state: str,
        cookies: dict[str, str] | None,
    ) -> BrowserResult:
        nav_status: dict = {"status": 200, "final_url": url}
        valid_proxy = proxy.strip() if (proxy and isinstance(proxy, str) and proxy.strip().startswith(("http://", "https://", "socks5://"))) else None

        # Isolate per-request proxy in a separate browser context
        context_id: str | None = None
        if valid_proxy:
            context_id = await self._chrome.create_browser_context(proxy_server=valid_proxy)
            tab = await self._chrome.new_tab(browser_context_id=context_id)
        else:
            tab = await self._chrome.new_tab()

        t0 = time.monotonic()
        harvested: dict[str, str] = {}
        body: str = ""
        final_url: str = url
        _cb_id: int | None = None

        try:
            await tab.enable_network_events()

            async def _on_response(event: dict) -> None:
                params = event.get("params", {})
                if params.get("type") == "Document":
                    resp = params.get("response", {})
                    resp_url = resp.get("url", "")
                    if "challenges.cloudflare.com" not in resp_url:
                        nav_status["status"] = resp.get("status", 200)
                        nav_status["final_url"] = resp_url or url

            _cb_id = await tab.on(NetworkEvent.RESPONSE_RECEIVED, _on_response)

            # Pre-inject cached cookies to skip Turnstile challenge
            if cookies:
                host = urlparse(url).hostname or ""
                domain = "." + ".".join(host.split(".")[-2:]) if host.count(".") >= 1 else host
                await tab.set_cookies([
                    {"name": k, "value": v, "domain": domain, "path": "/"}
                    for k, v in cookies.items()
                ])

            logger.info("pydoll: navigating -> %s (cf_wait=%.1fs)", url, cf_wait)

            try:
                async with tab.expect_and_bypass_cloudflare_captcha(time_to_wait_captcha=cf_wait):
                    await tab.go_to(url)

                # Allow post-bypass redirect / DOM update to settle
                await asyncio.sleep(1.0)
                title = (await tab.title).lower()
                final_url = await tab.current_url
                body = await tab.page_source
                harvested = {c["name"]: c["value"] for c in await tab.get_cookies()}

                is_challenge = (
                    "just a moment" in title
                    or "attention required" in title
                    or "<title>just a moment" in body.lower()
                    or 'id="challenge-running"' in body
                )
                nav_status["status"] = 403 if is_challenge else 200

                # Clearance may land after the wall renders; one reload often clears it
                if is_challenge:
                    logger.info("pydoll: challenge persists, reloading once")
                    with contextlib.suppress(Exception):
                        await tab.go_to(final_url)
                        await asyncio.sleep(2.0)
                        title = (await tab.title).lower()
                        final_url = await tab.current_url
                        body = await tab.page_source
                        harvested = {c["name"]: c["value"] for c in await tab.get_cookies()}
                        is_challenge = (
                            "just a moment" in title
                            or "attention required" in title
                            or "<title>just a moment" in body.lower()
                            or 'id="challenge-running"' in body
                        )
                    nav_status["status"] = 403 if is_challenge else 200

            except Exception as nav_err:
                logger.warning("pydoll: navigation error on %s: %s", url, nav_err)
                nav_status["status"] = 403
                with contextlib.suppress(Exception):
                    harvested = {c["name"]: c["value"] for c in await tab.get_cookies()}
                with contextlib.suppress(Exception):
                    body = await tab.page_source
                with contextlib.suppress(Exception):
                    final_url = await tab.current_url

            if not harvested:
                with contextlib.suppress(Exception):
                    harvested = {c["name"]: c["value"] for c in await tab.get_cookies()}
            if not body:
                with contextlib.suppress(Exception):
                    body = await tab.page_source

            if "just a moment" in body.lower() or "<title>just a moment" in body.lower():
                nav_status["status"] = 403

        finally:
            # Explicitly remove network callback before closing to prevent reference leak
            if _cb_id is not None:
                with contextlib.suppress(Exception):
                    await tab.remove_callback(_cb_id)
            with contextlib.suppress(Exception):
                await tab.close()
            if context_id:
                with contextlib.suppress(Exception):
                    await self._chrome.delete_browser_context(context_id)

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        status = int(nav_status.get("status", 200))

        logger.info("pydoll: done -> %s | status=%d | cookies=%d | elapsed=%dms", final_url, status, len(harvested), elapsed_ms)

        return BrowserResult(
            status_code=status,
            body=body,
            cookies=harvested,
            final_url=final_url,
            elapsed_ms=elapsed_ms,
        )


pool = BrowserPool()
