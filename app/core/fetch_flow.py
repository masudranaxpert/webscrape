"""The /fetch browser pipeline: navigate, clear the CF wall, harvest page state.

Runs on a pool worker tab. Kept separate from the pool class: this is request
pipeline, not slot machinery. The pool instance is passed in for the click seam
(tests monkeypatch pool._click_turnstile_once).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any
from urllib.parse import urljoin

from app.core.browser import BrowserResult
from app.core.fetcher import _skip_reason
from app.core.proxy import is_proxy_error, mask_proxy, redact
from app.core.utils.playwright import cancel_download as _cancel_download
from app.core.utils.turnstile_js import FIND_CHECKBOX_JS as _FIND_CHECKBOX_JS
from app.core.utils.challenge import await_click_outcome, has_selector, is_bypassed
from app.core.utils.playwright import bounded as _bounded
from app.core.utils.turnstile_js import CLICK_ACTION_JS as _CLICK_ACTION_JS

logger = logging.getLogger(__name__)

_MAX_CHAIN = 25
# Origin statuses that mean "past Cloudflare, but the site itself refuses" (e.g. a per-IP rate limit answers 429):
# waiting out cf_wait for a selector on such a page only wastes the budget
_ORIGIN_REFUSAL_STATUSES = (429,)
# Chromium net errors that mean the target is unreachable (no page, no challenge to solve)
_FATAL_NET_ERRORS = (
    "ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION_REFUSED", "ERR_ADDRESS_UNREACHABLE", "ERR_CERT_",
    "ERR_SSL_PROTOCOL_ERROR", "ERR_INVALID_URL", "ERR_UNSAFE_PORT",
)


async def run_fetch_flow(
    pool,
    worker,
    url: str,
    cf_wait: float,
    proxy: str | None,
    page_load_state: str,
    cookies: dict[str, str] | None,
    wait_for_selector: str | None = None,
    click_selector: str | None = None,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
) -> BrowserResult:
    t0 = time.monotonic()
    # Map API load-state names onto Playwright wait_until values
    load_state = {"complete": "load", "interactive": "domcontentloaded"}.get(
        (page_load_state or "").lower(), "domcontentloaded"
    )

    context = None
    selector = (wait_for_selector or "").strip() or None
    notes: list[str] = []
    harvested: dict[str, str] = {}
    final_url: str = url
    status_code = 200
    status: int = 200
    resp_headers: dict[str, str] = {}
    user_agent: str | None = pool._default_browser_ua
    initial_clearance: str | None = None
    attempted_clearance_reload: bool = False
    origin_refused = False

    try:
        context_kwargs: dict[str, Any] = {"viewport": {"width": 1280, "height": 720}}
        if not pool._geoip:
            context_kwargs["locale"] = "en-US"

        context = await worker.browser.new_context(**context_kwargs)

        page = context.pages[0] if context.pages else await context.new_page()
        timeout_ms = max(int(cf_wait * 1000), 30000)
        page.set_default_timeout(timeout_ms)
        page.set_default_navigation_timeout(timeout_ms)

        # Track the last top-level document response: the wall often clears via in-page navigation, so the first
        # goto() response (403 + cf-mitigated) would otherwise be reported for the final page. Challenge responses
        # are skipped; an origin's own 403 is kept so it is never reported as 200
        last_doc: dict[str, Any] = {}

        def _on_response(r: Any) -> None:
            try:
                if (
                    r.request.resource_type == "document" and r.frame == page.main_frame
                    and "challenge" not in str((r.headers or {}).get("cf-mitigated", "")).lower()
                ):
                    last_doc["status"] = r.status
                    last_doc["headers"] = dict(r.headers or {})
            except Exception:
                pass

        page.on("response", _on_response)

        # Every main-frame navigation (redirect hops included) and any file download the page starts. A download is
        # cancelled right away: Anti reports where the file lives (its URL), it never fetches it
        chain: list[dict[str, Any]] = []
        downloads: list[dict[str, str]] = []

        def _on_nav_response(r: Any) -> None:
            try:
                if r.request.resource_type == "document" and r.frame == page.main_frame and len(chain) < _MAX_CHAIN:
                    location = (r.headers or {}).get("location")
                    chain.append({
                        "url": r.url,
                        "status": r.status,
                        "location": urljoin(r.url, location) if location else None,
                    })
            except Exception:
                pass

        def _on_download(download: Any) -> None:
            downloads.append({"url": download.url, "filename": download.suggested_filename})
            asyncio.ensure_future(_cancel_download(download))

        page.on("response", _on_nav_response)
        page.on("download", _on_download)

        # Pre-inject cached cookies into context
        if cookies:
            cookie_list = [
                {"name": k, "value": v, "url": url}
                for k, v in cookies.items()
            ]
            with contextlib.suppress(Exception):
                await context.add_cookies(cookie_list)

        if headers:
            with contextlib.suppress(Exception):
                await page.set_extra_http_headers(headers)

        nav_method = (method or "GET").upper()
        if nav_method not in ("GET", "HEAD"):
            # Navigations are always GET: rewrite the ones to the target URL into the requested method and body.
            # A predicate, not page.route(url): a URL with '*', '?' or '{' would be read as a glob and never match
            target = url.split("#")[0]
            request_body = body or ""

            async def _handle_initial_method(route: Any) -> None:
                req = route.request
                if req.is_navigation_request() and req.url.split("#")[0] == target:
                    await route.continue_(method=nav_method, post_data=request_body)
                else:
                    await route.continue_()
            with contextlib.suppress(Exception):
                await page.route(lambda u: u.split("#")[0] == target, _handle_initial_method)

        logger.info("cloakbrowser: navigating -> %s (timeout=%.1fs)", url, timeout_ms / 1000)
        try:
            resp = await page.goto(url, wait_until=load_state, timeout=timeout_ms)
            if resp is not None and getattr(resp, "status", None):
                status_code = resp.status
                resp_headers = dict(getattr(resp, "headers", None) or {})
        except Exception as nav_err:
            if proxy and is_proxy_error(str(nav_err)):
                raise RuntimeError(f"Proxy {mask_proxy(proxy)} failed: {redact(str(nav_err).splitlines()[0])}") from nav_err
            if "Download is starting" in str(nav_err) or downloads:
                logger.info("cloakbrowser: %s is a file download; not fetching it", url)
            elif any(code in str(nav_err) for code in _FATAL_NET_ERRORS):
                # The host cannot be reached at all: waiting out cf_wait on Chrome's error page only burns a tab
                raise RuntimeError(f"Navigation failed: {redact(str(nav_err).splitlines()[0])}") from nav_err
            else:
                logger.warning("cloakbrowser navigation warning on %s: %s", url, redact(str(nav_err)))

        # Settle period for challenge scripts to execute
        await asyncio.sleep(2.5)
        # Check for interstitial wall or a not-yet-rendered target selector.
        # Embedded Turnstile token minting is NOT this engine's job — use /solve-turnstile.
        wall = not await is_bypassed(page)
        missing = selector is not None and not await has_selector(page, selector)

        if wall or missing:
            reasons = [
                r for r, on in (
                    ("interstitial wall", wall),
                    (f"selector '{selector}' not rendered", missing),
                ) if on
            ]
            logger.info("Solver loop entered: %s", ", ".join(reasons))
            notes.append(f"Solver triggered: {', '.join(reasons)}")

            last_click = 0.0
            deadline = time.monotonic() + cf_wait
            fresh_clearance_at: float | None = None

            # Snapshot clearance present before solver execution (e.g. from pre-injected cookies)
            raw_init_cookies = await _bounded(context.cookies())
            initial_clearance = next(
                (c.get("value") for c in raw_init_cookies if c.get("name") == "cf_clearance"),
                None,
            )
            if initial_clearance and wall:
                logger.info("Pre-injected cf_clearance did not bypass challenge wall; awaiting fresh Turnstile solve")

            while not (await is_bypassed(page) and (selector is None or await has_selector(page, selector))):
                if time.monotonic() >= deadline:
                    break
                if last_doc.get("status") in _ORIGIN_REFUSAL_STATUSES and await is_bypassed(page):
                    origin_refused = True
                    notes.append(f"Origin answered HTTP {last_doc['status']} past the challenge -> solver stopped early")
                    logger.info("Origin refused %s with HTTP %s; stopping solver early", url, last_doc["status"])
                    break

                # 1. Real-time fresh cf_clearance detection
                raw_cookies = await _bounded(context.cookies())
                current_clearance = next(
                    (c.get("value") for c in raw_cookies if c.get("name") == "cf_clearance" and len(c.get("value", "")) > 10),
                    None,
                )
                is_fresh_clearance = bool(
                    current_clearance and (initial_clearance is None or current_clearance != initial_clearance)
                )

                if is_fresh_clearance:
                    if fresh_clearance_at is None:
                        fresh_clearance_at = time.monotonic()
                        logger.info("Fresh cf_clearance cookie obtained during solve loop")
                        notes.append("Fresh cf_clearance obtained")

                    # Give Cloudflare client script 3.5s to complete its natural auto-redirect.
                    # If still on challenge page after 3.5s, execute at most ONE reload to target URL.
                    title = (await _bounded(page.title())).lower()
                    is_challenge = any(w in title for w in ("just a moment", "verify you are human", "attention required", "security check"))
                    if is_challenge and not attempted_clearance_reload and (time.monotonic() - fresh_clearance_at >= 3.5):
                        attempted_clearance_reload = True
                        logger.info("Fresh clearance present but still on challenge page -> executing single reload")
                        notes.append("Navigating to destination with fresh clearance")
                        with contextlib.suppress(Exception):
                            resp = await page.goto(url, wait_until=load_state, timeout=15000)
                            if resp is not None and getattr(resp, "status", None):
                                status_code = resp.status
                                resp_headers = dict(getattr(resp, "headers", None) or {})
                        await asyncio.sleep(1.0)
                        if await is_bypassed(page):
                            logger.info("Page successfully cleared after single clearance reload!")
                            break

                # 2. Throttled interactive click attempt (clears CF's interactive wall checkbox)
                if time.monotonic() - last_click >= 2.5:
                    if await pool._click_turnstile_checkbox(page):
                        last_click = time.monotonic()

                await asyncio.sleep(0.8)

            # Post-loop clearance navigation check (only if fresh clearance exists and never reloaded)
            raw_cookies = await _bounded(context.cookies())
            final_clearance = next(
                (c.get("value") for c in raw_cookies if c.get("name") == "cf_clearance" and len(c.get("value", "")) > 10),
                None,
            )
            is_fresh_final = bool(
                final_clearance and (initial_clearance is None or final_clearance != initial_clearance)
            )
            if is_fresh_final and not await is_bypassed(page) and not attempted_clearance_reload:
                attempted_clearance_reload = True
                logger.info("Fresh clearance present at loop exit -> executing single reload to target URL")
                notes.append("Executing reload with clearance cookie")
                with contextlib.suppress(Exception):
                    resp = await page.goto(url, wait_until=load_state, timeout=15000)
                    if resp is not None and getattr(resp, "status", None):
                        status_code = resp.status
                        resp_headers = dict(getattr(resp, "headers", None) or {})
                await asyncio.sleep(1.0)

            if await is_bypassed(page) and (selector is None or await has_selector(page, selector)):
                notes.append("Solver finished: page ready")
                logger.info("Challenge wall resolved successfully")
            else:
                try:
                    title = await _bounded(page.title())
                    frames = [f.url[:90] for f in page.frames if "challenges.cloudflare" in (f.url or "")]
                    cbs = []
                    for f in page.frames:
                        if "challenges.cloudflare" in (f.url or ""):
                            try:
                                cbs.append(await _bounded(f.evaluate(_FIND_CHECKBOX_JS)))
                            except Exception as fe:
                                cbs.append(f"err:{fe}"[:80])
                    notes.append(f"debug: title={title!r} cf_frames={frames} checkboxes={cbs}")
                except Exception as de:
                    notes.append(f"debug failed: {de}")

        # Final state capture. Chrome's own error page (timeout, reset, proxy drop) carries no challenge markers and
        # would otherwise be reported as a solved 200
        if (page.url or "").startswith("chrome-error://") and not downloads:
            raise RuntimeError(f"Navigation to {url} failed: the page never loaded (network error or timeout)")
        bypassed = await is_bypassed(page)
        if not bypassed and not attempted_clearance_reload:
            raw_cookies = await _bounded(context.cookies())
            final_clearance = next(
                (c.get("value") for c in raw_cookies if c.get("name") == "cf_clearance" and len(c.get("value", "")) > 10),
                None,
            )
            if final_clearance and (initial_clearance is None or final_clearance != initial_clearance):
                attempted_clearance_reload = True
                logger.info("Fresh cf_clearance exists; executing recovery reload to target...")
                with contextlib.suppress(Exception):
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=12000)
                    if resp is not None and getattr(resp, "status", None):
                        status_code = resp.status
                        resp_headers = dict(getattr(resp, "headers", None) or {})
                await asyncio.sleep(1.0)
                bypassed = await is_bypassed(page)

        if bypassed and last_doc:
            status_code = last_doc["status"]
            resp_headers = last_doc["headers"]
        # Body is already decoded text: encoding/length headers from the wire would mislead API clients
        resp_headers = {
            k: v for k, v in resp_headers.items()
            if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
        }
        if bypassed:
            status = status_code if (last_doc or status_code != 403) else 200
        else:
            status = status_code if status_code != 200 else 403
        if bypassed and selector and not origin_refused and not await has_selector(page, selector):
            notes.append(f"Timed out waiting for selector '{selector}' (cf_wait={cf_wait}s)")
            logger.warning("Selector '%s' never appeared on %s within cf_wait budget", selector, url)
        final_url = page.url or url

        if click_selector and not origin_refused:
            logger.info("Handling click_selector '%s'...", click_selector)
            # Fail-fast: only wait for disabled flag to clear if selector actually exists in DOM
            has_target = False
            with contextlib.suppress(Exception):
                has_target = bool(await _bounded(page.eval_on_selector(click_selector, "el => true")))

            if has_target:
                # A button gated by a challenge enables itself once the widget is solved: wait for that
                for _ in range(15):
                    btn_disabled = True
                    with contextlib.suppress(Exception):
                        btn_disabled = await _bounded(page.eval_on_selector(click_selector, "el => el.disabled"))
                    if not btn_disabled:
                        break
                    await asyncio.sleep(1.0)
            else:
                logger.debug("Selector '%s' not present in DOM; skipping disabled-wait loop", click_selector)

            clicked = False
            try:
                clicked = await _bounded(page.evaluate(_CLICK_ACTION_JS, click_selector))
            except Exception as ce:
                logger.warning("Click evaluation failed: %s", ce)

            if clicked:
                notes.append(f"Clicked selector: {click_selector}")
                outcome = await await_click_outcome(page, chain, downloads)
                notes.append(f"After click: {outcome}")
                final_url = page.url or final_url

        body = ""
        result_note: str | None = None
        # A binary main document (video, archive, attachment...) is the same case as a download: same rule as the
        # HTTP engine. Chromium opens playable media in its own player page and keeps buffering it, so stop here
        doc_headers = {str(k).lower(): str(v) for k, v in (last_doc.get("headers") or {}).items()}
        media_reason = None if downloads else _skip_reason("GET", doc_headers, None, 1 << 62)
        if downloads or media_reason:
            if downloads:
                final_url = downloads[-1]["url"]
                result_note = f"navigation became a file download ({downloads[-1]['filename'] or 'file'}); file not fetched"
            else:
                final_url = page.url or final_url
                result_note = f"{media_reason.replace(' not read', '')}; file not fetched"
            with contextlib.suppress(Exception):
                await _bounded(page.goto("about:blank", timeout=5000))  # stop the player buffering the file
            notes.append(result_note)
            if status_code in (0, 301, 302, 303, 307, 308):
                status = 200
        else:
            for _ in range(5):
                try:
                    body = await _bounded(page.content())
                    final_url = page.url or final_url
                    break
                except Exception:
                    await asyncio.sleep(1.0)

        raw_cookies = await _bounded(context.cookies())
        harvested = {c["name"]: c["value"] for c in raw_cookies}

        with contextlib.suppress(Exception):
            ua = await _bounded(page.evaluate("navigator.userAgent"))
            if ua and isinstance(ua, str) and len(ua) > 10:
                user_agent = ua
                if not pool._default_browser_ua:
                    pool._default_browser_ua = ua

    finally:
        if context is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(context.close()), timeout=5.0)

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
        redirect_chain=chain if len(chain) > 1 or any(h["location"] for h in chain) else [],
        note=result_note,
    )


