"""Stealth browser automation engine backed by CloakBrowser with automated Turnstile solving."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import re
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
    const inputs = document.querySelectorAll(
        'input[name*="turnstile-response"], textarea[name*="turnstile-response"]'
    );
    // Widgets rendered via turnstile.render() live in a shadow tree the selectors above cannot see, but they
    // always leave their hidden response input behind in the light DOM
    if(!widgets.length && !inputs.length) return {present:false, solved:false};
    const solved = inputs.length > 0 && Array.from(inputs).every(i => i.value && i.value.length > 0);
    return {present:true, solved:solved};
}"""

# Reads the Turnstile token: known response inputs, caller-supplied names/globs, then turnstile.getResponse()
_TURNSTILE_TOKEN_JS = """(names) => {
    const toRe = g => new RegExp('^' + g.replace(/[.+^${}()|[\\]\\\\]/g, '\\\\$&').replace(/\\*/g, '.*') + '$', 'i');
    const pats = (names || []).map(toRe);
    let token = null;
    const fields = document.querySelectorAll('input[name], textarea[name]');
    for(const el of fields){
        const n = el.name || '';
        if(!el.value) continue;
        if(/turnstile-response/i.test(n) || pats.some(p => p.test(n))){ token = el.value; break; }
    }
    if(!token && window.turnstile && typeof window.turnstile.getResponse === 'function'){
        try { token = window.turnstile.getResponse() || null; } catch(e){}
    }
    const w = document.querySelector('.cf-turnstile[data-sitekey], [data-sitekey]');
    let sitekey = w ? w.getAttribute('data-sitekey') : null;
    if(!sitekey){
        const f = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
        const m = f && f.src.match(/\\/(0x[0-9A-Za-z_-]+)/);
        if(m) sitekey = m[1];
    }
    return {token, sitekey};
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
    turnstile_token: str | None = None
    turnstile_sitekey: str | None = None
    turnstile_solved_at: float | None = None
    token_submissions: list[dict[str, Any]] = dataclasses.field(default_factory=list)


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
        """Check if active browser worker has exceeded max lifetime TTL and has no active contexts."""
        if self._browser is None:
            return False
        if self._active_contexts > 0:
            return False
        return (time.monotonic() - self._browser_created_at) >= self._max_lifetime_sec

    async def _close_browser_safely(self, browser: Any) -> None:
        """Safely close a browser instance and release process resources."""
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
            self._retiring_browsers.discard(browser)

    async def _launch_browser(self, launch_kwargs: dict[str, Any]) -> Any:
        """Launch CloakBrowser instance with automatic fallback to unauthenticated mode on license error."""
        try:
            return await cb.launch_async(**launch_kwargs)
        except Exception as err:
            err_msg = str(err).lower()
            if "license" in err_msg or "session limit" in err_msg or "exit code 76" in err_msg or "exitcode=76" in err_msg:
                logger.warning(
                    "CloakBrowser license failure (%s); falling back to unauthenticated engine",
                    err,
                )
                fallback_kwargs = dict(launch_kwargs)
                fallback_kwargs["license_key"] = None
                saved_key = os.environ.pop("CLOAKBROWSER_LICENSE_KEY", None)
                try:
                    return await cb.launch_async(**fallback_kwargs)
                finally:
                    if saved_key:
                        os.environ["CLOAKBROWSER_LICENSE_KEY"] = saved_key
            raise

    async def _get_browser(self, launch_kwargs: dict[str, Any]) -> Any:
        """Acquire healthy browser instance, cleanly recycling idle aged workers."""
        if self._launch_lock is None:
            self._launch_lock = asyncio.Lock()

        async with self._launch_lock:
            # Check if existing browser disconnected or terminated
            if self._browser is not None:
                try:
                    if not self._browser.is_connected():
                        logger.warning("Browser disconnected or terminated; cleaning up for restart")
                        old_b = self._browser
                        self._browser = None
                        await self._close_browser_safely(old_b)
                except Exception:
                    self._browser = None

            if self._should_recycle():
                age = time.monotonic() - self._browser_created_at
                logger.info(
                    "Recycling browser worker (age=%.1fs, active_contexts=%d) before serving new request",
                    age,
                    self._active_contexts,
                )
                old_browser = self._browser
                self._browser = None
                if old_browser:
                    await self._close_browser_safely(old_browser)

            if self._browser is None:
                self._browser = await self._launch_browser(launch_kwargs)
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
        capture_token: bool = False,
        token_field_names: list[str] | None = None,
        click_selector: str | None = None,
    ) -> BrowserResult:
        """Acquire a slot, navigate to target, solve challenge if present, and harvest tokens."""
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_tabs)
        if self._launch_lock is None:
            self._launch_lock = asyncio.Lock()

        async with self._sem:
            return await self._run_context(
                url,
                cf_wait,
                proxy,
                page_load_state,
                cookies,
                wait_for_selector,
                capture_token,
                token_field_names,
                click_selector,
            )

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
            if await page.query_selector(selector) is not None:
                return True
            if selector.startswith("."):
                cls_name = selector[1:]
                return await page.query_selector(f"[class*='{cls_name}']") is not None
            return False
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
                frame_el = await frame.frame_element()
                box = await frame_el.bounding_box()
                if box and box["width"] >= 20:
                    click_x = box["x"] + 30
                    click_y = box["y"] + box["height"] / 2
                    logger.info("Turnstile checkbox located: clicking at (%.1f, %.1f)", click_x, click_y)
                    await page.mouse.click(click_x, click_y)
                    await asyncio.sleep(0.5)
                    return True
            except Exception as e:
                logger.debug("Turnstile frame bounding box exception: %s", e)

            try:
                info = await frame.evaluate(_FIND_CHECKBOX_JS)
                if info and info.get("found") and info.get("w", 0) > 0 and not info.get("checked"):
                    frame_el = await frame.frame_element()
                    box = await frame_el.bounding_box()
                    if box:
                        click_x = box["x"] + info["x"]
                        click_y = box["y"] + info["y"]
                        await page.mouse.click(click_x, click_y)
                        await asyncio.sleep(0.5)
                        return True
            except Exception as e:
                logger.debug("Turnstile frame evaluate exception: %s", e)
        return False

    async def _run_context(
        self,
        url: str,
        cf_wait: float,
        proxy: str | None,
        page_load_state: str,
        cookies: dict[str, str] | None,
        wait_for_selector: str | None = None,
        capture_token: bool = False,
        token_field_names: list[str] | None = None,
        click_selector: str | None = None,
    ) -> BrowserResult:
        t0 = time.monotonic()
        proxy_cfg = _parse_proxy(proxy)
        # Map API load-state names onto Playwright wait_until values
        load_state = {"complete": "load", "interactive": "domcontentloaded"}.get(
            (page_load_state or "").lower(), "domcontentloaded"
        )

        launch_kwargs: dict[str, Any] = {
            "headless": self._headless,
            "humanize": True,
            "_suppress_maximize": True,
            "args": [
                FAKE_SHADOW_ARG,
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--lang=en-US,en",
                "--no-crashpad",
                "--disable-crash-reporter",
                "--window-size=1280,720",
            ],
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
        ts_token: str | None = None
        ts_sitekey: str | None = None
        ts_solved_at: float | None = None

        try:
            browser = await self._get_browser(launch_kwargs)
            if self._launch_lock is not None:
                async with self._launch_lock:
                    self._active_contexts += 1

            context_kwargs: dict[str, Any] = {
                "locale": "en-US",
                "viewport": {"width": 1280, "height": 720},
            }
            if proxy_cfg:
                context_kwargs["proxy"] = proxy_cfg

            context = await browser.new_context(**context_kwargs)

            page = context.pages[0] if context.pages else await context.new_page()
            timeout_ms = max(int(cf_wait * 1000), 30000)
            page.set_default_timeout(timeout_ms)
            page.set_default_navigation_timeout(timeout_ms)

            # Track the last real top-level document response: the wall often clears via in-page navigation, so the
            # first goto() response (403 + cf-mitigated) would otherwise be reported for the final page
            last_doc: dict[str, Any] = {}

            def _on_response(r: Any) -> None:
                try:
                    if r.request.resource_type == "document" and r.frame == page.main_frame and r.status != 403:
                        last_doc["status"] = r.status
                        last_doc["headers"] = dict(r.headers or {})
                except Exception:
                    pass

            page.on("response", _on_response)

            # Sites often POST the token themselves the instant it is issued, then wipe the widget: record those
            # submissions (token + what the server answered) since the DOM no longer has them
            submissions: list[dict[str, Any]] = []
            token_re = re.compile(
                r"(?:turnstile-response|cf_turnstile|"
                + "|".join(re.escape(n) for n in (token_field_names or []))
                + r")[\"']?\s*[=:]\s*[\"']?([A-Za-z0-9._\-]{40,})"
                if token_field_names else
                r"(?:turnstile-response|cf_turnstile)[\"']?\s*[=:]\s*[\"']?([A-Za-z0-9._\-]{40,})",
                re.I,
            )

            async def _on_submit_response(r: Any) -> None:
                try:
                    req = r.request
                    m = token_re.search(req.post_data or "")
                    if not m or len(submissions) >= 5:
                        return
                    try:
                        text = (await r.text())[:4000]
                    except Exception:
                        text = ""
                    submissions.append({
                        "url": req.url,
                        "method": req.method,
                        "token": m.group(1),
                        "response_status": r.status,
                        "response_content_type": r.headers.get("content-type"),
                        "response_body": text,
                    })
                except Exception:
                    pass

            page.on("response", lambda r: asyncio.ensure_future(_on_submit_response(r)))

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

                last_click = 0.0
                refreshed = False
                extended = False
                deadline = time.monotonic() + cf_wait
                while not await self._is_ready(page, selector):
                    if time.monotonic() >= deadline:
                        break
                    # Retry on a throttle: the interstitial and a later embedded widget each need their own click
                    if time.monotonic() - last_click >= 3.0:
                        if await self._click_turnstile_checkbox(page):
                            last_click = time.monotonic()
                            # Sites consume the token in their own callback within a second: grab it as it appears
                            for _ in range(25 if await self._is_bypassed(page) else 0):
                                await asyncio.sleep(0.2)
                                with contextlib.suppress(Exception):
                                    tok = (await page.evaluate(_TURNSTILE_TOKEN_JS, token_field_names or [])).get("token")
                                    if tok:
                                        ts_token = tok
                                        break
                    # The wall cleared but a second (embedded) widget appeared: its checkbox takes ~5s to render
                    # and ~2s to verify, so grant it a fresh window instead of the exhausted wall budget
                    if not extended and await self._is_bypassed(page) and await self._turnstile_pending(page):
                        extended = True
                        deadline = max(deadline, time.monotonic() + 20.0)
                        notes.append("Embedded widget after wall -> solve window extended")
                    # Clearance issued but the wall page did not navigate on its own: reload once so the
                    # real page (and any embedded widget) can load inside the remaining solve window
                    if not refreshed and not await self._is_bypassed(page):
                        ck = {c["name"] for c in await context.cookies()}
                        if "cf_clearance" in ck:
                            refreshed = True
                            logger.info("Clearance cookie present mid-solve, reloading page...")
                            with contextlib.suppress(Exception):
                                resp = await page.goto(url, wait_until=load_state, timeout=15000)
                                if resp is not None and getattr(resp, "status", None):
                                    status_code = resp.status
                                    resp_headers = dict(getattr(resp, "headers", None) or {})
                            await asyncio.sleep(1.0)
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
                    try:
                        title = await page.title()
                        st = await page.evaluate(_TURNSTILE_STATE_JS)
                        frames = [f.url[:90] for f in page.frames if "challenges.cloudflare" in (f.url or "")]
                        cbs = []
                        for f in page.frames:
                            if "challenges.cloudflare" in (f.url or ""):
                                try:
                                    cbs.append(await f.evaluate(_FIND_CHECKBOX_JS))
                                except Exception as fe:
                                    cbs.append(f"err:{fe}"[:80])
                        notes.append(f"debug: title={title!r} state={st} cf_frames={frames} checkboxes={cbs}")
                    except Exception as de:
                        notes.append(f"debug failed: {de}")

            # Final state capture
            bypassed = await self._is_bypassed(page)
            if bypassed and last_doc:
                status_code = last_doc["status"]
                resp_headers = last_doc["headers"]
            # Body is already decoded text: encoding/length headers from the wire would mislead API clients
            resp_headers = {
                k: v for k, v in resp_headers.items()
                if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
            }
            status = 200 if bypassed else (status_code if status_code != 200 else 403)
            if bypassed and selector and not await self._has_selector(page, selector):
                status = 408
                notes.append(f"Timed out waiting for selector '{selector}' (cf_wait={cf_wait}s)")
                logger.warning("Selector '%s' never appeared on %s", selector, url)
            final_url = page.url or url

            widget_present = False
            # Embedded widgets are injected ~2s after the real page loads, so right after a wall solve "no widget yet"
            # is not final: give it a short window to appear before concluding the page has none
            widget_deadline = time.monotonic() + (5.0 if wall else 0.0)
            while True:
                with contextlib.suppress(Exception):
                    widget_present = bool((await page.evaluate(_TURNSTILE_STATE_JS)).get("present"))
                if widget_present or time.monotonic() >= widget_deadline:
                    break
                await asyncio.sleep(1.0)
            if ts_token and not capture_token:
                capture_token = True
                notes.append("Token caught right after solve -> capturing")
            if not widget_present and not capture_token and wall:
                # The widget may already be solved and gone from the DOM while its token input still holds the value
                with contextlib.suppress(Exception):
                    if (await page.evaluate(_TURNSTILE_TOKEN_JS, token_field_names or [])).get("token"):
                        capture_token = True
                        notes.append("Solved Turnstile token found in DOM -> capturing")
            if widget_present and not capture_token:
                capture_token = True
                notes.append("Turnstile widget detected -> auto-capturing token")
            if capture_token:
                async def _read_token(window: float) -> None:
                    nonlocal ts_token, ts_sitekey
                    until = time.monotonic() + window
                    last_try = 0.0
                    while True:
                        # The widget checkbox may still be waiting for a click (it renders a few seconds late)
                        if widget_present and not ts_token and time.monotonic() - last_try >= 3.0:
                            last_try = time.monotonic()
                            with contextlib.suppress(Exception):
                                await self._click_turnstile_checkbox(page)
                        try:
                            info = await page.evaluate(_TURNSTILE_TOKEN_JS, token_field_names or [])
                            ts_token = (info or {}).get("token") or ts_token
                            ts_sitekey = (info or {}).get("sitekey") or ts_sitekey
                        except Exception as te:
                            logger.debug("Turnstile token read failed: %s", te)
                        if ts_token or time.monotonic() >= until:
                            return
                        await asyncio.sleep(1.0)

                await _read_token(15.0 if widget_present else 3.0)
                if ts_token:
                    ts_solved_at = time.time()
                    notes.append("Turnstile token captured (single-use, not cached)")
                else:
                    notes.append("capture_token set but no Turnstile token found on page")

            # The site's own token POST + answer can lag the solve by a moment
            if (wall or embedded) and not submissions:
                await asyncio.sleep(1.5)
            if submissions and not ts_token:
                ts_token = submissions[0]["token"]
                ts_solved_at = time.time()
                notes.append("Token read from the site's own submit request (widget already removed from DOM)")
            if submissions:
                notes.append(f"Site submitted the token itself -> {len(submissions)} submission(s) captured in token_submissions")

            if click_selector:
                logger.info("Handling click_selector '%s'...", click_selector)
                for _ in range(15):
                    btn_disabled = True
                    note = ""
                    with contextlib.suppress(Exception):
                        note = (await page.eval_on_selector("#note", "el => el.innerText") or "").lower()
                    with contextlib.suppress(Exception):
                        btn_disabled = await page.eval_on_selector(click_selector, "el => el.disabled")
                    if not btn_disabled or "verified" in note or ts_token:
                        break
                    await asyncio.sleep(1.0)

                clicked = False
                try:
                    clicked = await page.evaluate(f"""() => {{
                        const btn = document.querySelector('{click_selector}');
                        if (btn) {{
                            btn.disabled = false;
                            btn.click();
                            return true;
                        }}
                        const f = document.querySelector('form');
                        if (f) {{
                            f.submit();
                            return true;
                        }}
                        return false;
                    }}""")
                except Exception as ce:
                    logger.warning("Click evaluation failed: %s", ce)

                if clicked:
                    notes.append(f"Clicked selector: {click_selector}")
                    with contextlib.suppress(Exception):
                        await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    for _ in range(15):
                        await asyncio.sleep(1.0)
                        final_url = page.url or final_url
                        if any(k in final_url for k in ("video-downloads", "googleusercontent", "r2.dev", "vidzer", ".mkv", ".mp4")):
                            break
                        try:
                            html_check = await page.content()
                            if any(k in html_check for k in ("video-downloads", "googleusercontent", "r2.dev", "vidzer", ".mkv", ".mp4")):
                                break
                        except Exception:
                            pass
                        if final_url != url:
                            break

            for _ in range(5):
                try:
                    body = await page.content()
                    final_url = page.url or final_url
                    break
                except Exception:
                    await asyncio.sleep(1.0)

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
                            await self._close_browser_safely(retiring)

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
            turnstile_token=ts_token,
            turnstile_sitekey=ts_sitekey,
            turnstile_solved_at=ts_solved_at,
            token_submissions=submissions,
        )


pool = BrowserPool()
