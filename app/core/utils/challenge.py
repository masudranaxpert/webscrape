"""Turnstile/challenge page probes and the checkbox clicker, independent of the browser pool.

All functions take a Playwright `page` and are safe to call repeatedly inside a solve loop.
"""
import asyncio
import contextlib
import logging
import time
from typing import Any

from app.core.utils.playwright import bounded as _bounded
from app.core.utils.turnstile_js import (
    FIND_CHECKBOX_JS as _FIND_CHECKBOX_JS,
    IS_BYPASSED_JS as _IS_BYPASSED_JS,
    TURNSTILE_STATE_JS as _TURNSTILE_STATE_JS,
)

logger = logging.getLogger(__name__)

# After a click: longest wait for the page's reaction, and how long navigation must stay quiet to count as finished
_POST_CLICK_MAX_SEC = 15.0
_POST_CLICK_SETTLE_SEC = 1.5


async def is_bypassed(page: Any) -> bool:
    """Check if challenge wall has cleared via fast in-browser DOM probe."""
    try:
        return bool(await _bounded(page.evaluate(_IS_BYPASSED_JS)))
    except Exception:
        return False


async def turnstile_pending(page: Any) -> bool:
    """True when an embedded Turnstile widget exists on the page and has not yet issued a token."""
    try:
        state = await _bounded(page.evaluate(_TURNSTILE_STATE_JS))
        return bool(state and state.get("present") and not state.get("solved"))
    except Exception:
        return False


async def has_selector(page: Any, selector: str) -> bool:
    """True when the CSS selector currently matches an element in the live DOM."""
    try:
        if await _bounded(page.query_selector(selector)) is not None:
            return True
        if selector.startswith("."):
            cls_name = selector[1:]
            return await _bounded(page.query_selector(f"[class*='{cls_name}']")) is not None
        return False
    except Exception:
        return False


async def is_ready(page: Any, selector: str | None) -> bool:
    """Wall cleared, no pending embedded Turnstile, and (if requested) target selector rendered."""
    if not await is_bypassed(page):
        return False
    if await turnstile_pending(page):
        return False
    return selector is None or await has_selector(page, selector)


async def click_turnstile_once(page: Any) -> bool:
    """Find Turnstile checkbox inside shadow DOM or frame and click it via native events."""
    cf_frames = [f for f in page.frames if "challenges.cloudflare" in (f.url or "")]
    for frame in cf_frames:
        # 1. Direct frame element selectors (CTP / Turnstile labels)
        for sel in ('[role="checkbox"]', '.ctp-checkbox-label', 'input[type="checkbox"]', '#challenge-stage div', '.cb-i'):
            with contextlib.suppress(Exception):
                el = frame.locator(sel).first
                if await el.is_visible(timeout=300):
                    await el.click(timeout=1000, force=True)
                    logger.info("Turnstile frame clicked selector: '%s'", sel)
                    await asyncio.sleep(0.5)
                    return True

        # 2. Recursive shadow DOM evaluation (exact center coordinate)
        try:
            info = await _bounded(frame.evaluate(_FIND_CHECKBOX_JS))
            if info and info.get("found") and info.get("w", 0) > 0 and not info.get("checked"):
                frame_el = await _bounded(frame.frame_element())
                box = await _bounded(frame_el.bounding_box())
                if box:
                    click_x = box["x"] + info["x"]
                    click_y = box["y"] + info["y"]
                    logger.info("Turnstile checkbox located via shadow DOM: clicking at (%.1f, %.1f)", click_x, click_y)
                    await page.mouse.move(click_x, click_y)
                    await asyncio.sleep(0.1)
                    await page.mouse.click(click_x, click_y)
                    await asyncio.sleep(0.5)
                    return True
        except Exception as e:
            logger.debug("Turnstile frame evaluate exception: %s", e)

        # 3. Heuristic coordinate click via bounding box fallback
        try:
            frame_el = await _bounded(frame.frame_element())
            box = await _bounded(frame_el.bounding_box())
            if box and box["width"] >= 20:
                click_x = box["x"] + min(28, box["width"] * 0.15)
                click_y = box["y"] + box["height"] / 2
                logger.info("Turnstile checkbox located via bounding box: clicking at (%.1f, %.1f)", click_x, click_y)
                await page.mouse.move(click_x, click_y)
                await asyncio.sleep(0.1)
                await page.mouse.click(click_x, click_y)
                await asyncio.sleep(0.5)
                return True
        except Exception as e:
            logger.debug("Turnstile frame bounding box exception: %s", e)

    # 4. Keyboard Tab+Space fallback
    with contextlib.suppress(Exception):
        await page.keyboard.press("Tab")
        await asyncio.sleep(0.1)
        await page.keyboard.press("Space")
        logger.debug("Turnstile keyboard Tab+Space attempted")

    return False


async def await_click_outcome(page: Any, chain: list, downloads: list) -> str:
    """After a click, wait for what the page does next: a download, one or more navigations, or nothing.

    Generic for any site: ends once a download starts, or once navigation has been quiet for a moment after the
    URL changed (so multi-hop redirects finish), or after a bounded wait if the click triggered nothing.
    """
    start_url = page.url
    seen = len(chain)
    last_change: float | None = None
    deadline = time.monotonic() + _POST_CLICK_MAX_SEC
    while time.monotonic() < deadline:
        await asyncio.sleep(0.4)
        if downloads:
            return "file download started"
        now = time.monotonic()
        if len(chain) != seen:
            seen, last_change = len(chain), now
        elif last_change is None and page.url != start_url:
            last_change = now
        if last_change is not None and now - last_change >= _POST_CLICK_SETTLE_SEC:
            return f"navigated to {page.url}"
    return "no navigation within the wait window"
