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

import cloakbrowser as cb
from cloakbrowser.geoip import resolve_proxy_exit_ip

from app.core.config import (
    BROWSER_CALL_TIMEOUT_SEC,
    BROWSER_EXIT_IP_CHECK_SEC,
    BROWSER_LICENSED_IDENTITY,
    BROWSER_LICENSED_LIFETIME_SEC,
    BROWSER_CLICK_TIMEOUT_SEC,
    BROWSER_GEOIP,
    BROWSER_JOB_OVERHEAD_SEC,
    BROWSER_QUEUE_TIMEOUT_SEC,
    BROWSER_HEADLESS,
    BROWSER_MAX_LIFETIME_SEC,
    MAX_BROWSERS,
    MAX_TABS,
)
from app.core.cookie_store import store
from app.core.proxy import browser_proxy, is_proxy_error, mask_proxy, proxy_key, redact

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

# Fast in-browser DOM probe to determine if Cloudflare challenge wall has cleared
_IS_BYPASSED_JS = """() => {
    try {
        const title = (document.title || '').toLowerCase();
        const challengeTitles = [
            'just a moment',
            'attention required',
            'security check',
            'checking your browser',
            'verifying you are human'
        ];
        if (challengeTitles.some(t => title.includes(t))) return false;

        // Interstitial challenge wall DOM containers
        if (document.querySelector(
            '#challenge-running, #challenge-form, form#challenge-form, #challenge-stage, #cf-wrapper, .cf-browser-verification, #trk_jschal_js'
        )) return false;

        // Challenge platform orchestrator globals
        if (window._cf_chl_opt || window.__cf_chl_opt || window._cf_chl_ctx) return false;

        // Challenge iframe inside challenge container
        if (document.querySelector('#challenge-stage iframe, #challenge-form iframe, #cf-wrapper iframe')) return false;

        // Explicit block / access denied messages in body text
        const bodyText = (document.body ? (document.body.innerText || '') : '').toLowerCase();
        if (
            bodyText.includes('please complete the captcha') ||
            bodyText.includes('you have been blocked') ||
            bodyText.includes('sorry, you have been blocked') ||
            bodyText.includes('error 1020') ||
            bodyText.includes('access denied')
        ) return false;

        return true;
    } catch (e) {
        return false;
    }
}"""



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


@dataclasses.dataclass
class _Worker:
    """One Chromium process bound to a single egress identity (direct or one proxy)."""

    key: str
    proxy: str | None
    browser: Any
    created_at: float
    last_used: float
    active: int = 0
    timezone: str | None = None
    # Set when a tab on this process blew its hard deadline: the process may be wedged, so it takes no new tabs and
    # is replaced as soon as its remaining tabs finish
    poisoned: bool = False
    # False when this process runs the keyless build (by policy, or because the license seat was busy)
    licensed: bool = True
    # Egress IP the process was fingerprinted for (timezone/locale/WebRTC); re-checked for proxied workers
    exit_ip: str | None = None
    exit_ip_checked_at: float = dataclasses.field(default_factory=time.monotonic)


class BrowserPoolBusy(RuntimeError):
    """No browser slot became free within the queue budget (HTTP 503)."""


class BrowserJobTimeout(RuntimeError):
    """A browser job exceeded its hard deadline and was cancelled (HTTP 504)."""


# Licensed-launch retries when the single seat is still held by a just-closed process
_SEAT_RETRIES = 2
# Origin statuses that mean "past Cloudflare, but the site itself refuses" (e.g. target-site's per-IP "Too many
# downloads" page is a 429): waiting out cf_wait for a selector or token on such a page only wastes the budget
_ORIGIN_REFUSAL_STATUSES = (429,)
_SEAT_RETRY_DELAY_SEC = 2.5


async def _bounded(awaitable: Any, timeout: float = BROWSER_CALL_TIMEOUT_SEC) -> Any:
    """Await a Playwright call that has no native timeout (evaluate, content, cookies) with a hard ceiling."""
    return await asyncio.wait_for(awaitable, timeout=timeout)


class BrowserPool:
    """
    Keyed pool of stealth browser processes, one per egress identity.

    CloakBrowser applies proxy, timezone, locale and WebRTC IP at launch and process-wide, so a proxied request needs
    its own process for its fingerprint to match its exit IP. Processes are bounded by MAX_BROWSERS (license seats):
    when full, the least recently used idle process is closed before launching another identity.
    """

    def __init__(
        self,
        max_tabs: int = MAX_TABS,
        headless: bool = BROWSER_HEADLESS,
        max_lifetime_sec: int = BROWSER_MAX_LIFETIME_SEC,
        max_browsers: int = MAX_BROWSERS,
        geoip: bool = BROWSER_GEOIP,
        licensed_identity: str = BROWSER_LICENSED_IDENTITY,
        licensed_lifetime_sec: int = BROWSER_LICENSED_LIFETIME_SEC,
        exit_ip_check_sec: float = BROWSER_EXIT_IP_CHECK_SEC,
    ) -> None:
        self._max_tabs = max_tabs
        self._headless = headless
        self._max_lifetime_sec = max_lifetime_sec
        self._max_browsers = max_browsers
        self._geoip = geoip
        self._licensed_identity = licensed_identity if licensed_identity in ("any", "proxy", "direct") else "any"
        self._licensed_lifetime_sec = licensed_lifetime_sec
        self._exit_ip_check_sec = exit_ip_check_sec
        self._cond: asyncio.Condition | None = None
        self._workers: dict[str, _Worker] = {}
        self._waiting: dict[str, int] = {}
        self._probes: dict[str, asyncio.Task] = {}
        self._default_browser_ua: str | None = None

    async def start(self) -> None:
        """Pre-warm browser environment and initialize concurrency primitives."""
        self._cond = asyncio.Condition()
        try:
            bin_path = cb.ensure_binary()
            logger.info("Using cloakbrowser binary: %s", bin_path)
        except Exception as err:
            logger.warning("cloakbrowser binary check warning: %s", err)

        loop_module = type(asyncio.get_running_loop()).__module__
        if loop_module.startswith("uvloop"):
            logger.warning(
                "Event loop is uvloop: browser launches fork() the process and fail with Errno 12 once a merged writable "
                "mapping exceeds RAM+swap. Run uvicorn with --loop asyncio (docker-entrypoint.sh does)."
            )

        has_display = bool(os.getenv("DISPLAY"))
        if has_display and not self._headless:
            logger.info("Display detected (%s) - Running in HEADFUL mode", os.getenv("DISPLAY"))

        logger.info(
            "BrowserPool initialized | max_concurrent=%d | max_browsers=%d | headless=%s | geoip=%s | max_lifetime=%ds"
            " | licensed_identity=%s",
            self._max_tabs,
            self._max_browsers,
            self._headless,
            self._geoip,
            self._max_lifetime_sec,
            self._licensed_identity,
        )

    def stats(self) -> dict[str, int]:
        """Process counts for diagnostics (no proxy details: /health is unauthenticated)."""
        return {
            "browsers": len(self._workers),
            "max_browsers": self._max_browsers,
            "active_tabs": sum(w.active for w in self._workers.values()),
            "max_tabs": self._max_tabs,
            "queued": sum(self._waiting.values()),
            "licensed_browsers": sum(1 for w in self._workers.values() if w.licensed),
        }

    def _may_license(self, proxy: str | None) -> bool:
        """Whether this egress identity may take the license seat under BROWSER_LICENSED_IDENTITY."""
        if self._licensed_identity == "proxy":
            return proxy is not None
        if self._licensed_identity == "direct":
            return proxy is None
        return True

    def _launch_args(self) -> list[str]:
        args = [
            FAKE_SHADOW_ARG,
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--no-crashpad",
            "--disable-crash-reporter",
            "--window-size=1280,720",
            "--fingerprint-allow-3p-cookies",
        ]
        if not self._geoip:
            # Without geoip the locale is pinned; with geoip CloakBrowser derives it from the exit IP
            args.append("--lang=en-US,en")
        if self._headless:
            args.append("--ozone-override-screen-size=1920,1080")
        return args

    async def _close_browser_safely(self, browser: Any) -> None:
        """Safely close a browser instance and release process resources."""
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()

    async def _launch_keyless(self, launch_kwargs: dict[str, Any]) -> Any:
        """Launch the keyless build (v146): needs no license seat. Launches are serialized under the pool lock, so
        hiding the key from the environment for the duration of this call cannot race another launch."""
        fallback_kwargs = dict(launch_kwargs)
        fallback_kwargs["license_key"] = None
        saved_key = os.environ.pop("CLOAKBROWSER_LICENSE_KEY", None)
        try:
            return await cb.launch_async(**fallback_kwargs)
        finally:
            if saved_key:
                os.environ["CLOAKBROWSER_LICENSE_KEY"] = saved_key

    async def _launch_browser(self, launch_kwargs: dict[str, Any], use_license: bool = True) -> tuple[Any, bool]:
        """Launch CloakBrowser; on a license/seat error fall back to the unauthenticated engine. Returns (browser, licensed)."""
        if not use_license:
            return await self._launch_keyless(launch_kwargs), False
        last_err: Exception | None = None
        for attempt in range(_SEAT_RETRIES + 1):
            try:
                return await cb.launch_async(**launch_kwargs), True
            except Exception as err:
                last_err = err
                # Swapping egress identities closes one process and launches the next immediately; the license
                # server can still count the closed session for a moment, so give the seat time to free up first
                if "session limit" in str(err).lower() and attempt < _SEAT_RETRIES:
                    logger.info("CloakBrowser seat still busy; retrying licensed launch in %.1fs", _SEAT_RETRY_DELAY_SEC)
                    await asyncio.sleep(_SEAT_RETRY_DELAY_SEC)
                    continue
                break

        err_msg = str(last_err).lower()
        if "license" in err_msg or "session limit" in err_msg or "exit code 76" in err_msg or "exitcode=76" in err_msg:
            logger.warning("CloakBrowser license failure (%s); falling back to unauthenticated engine", redact(str(last_err)))
            return await self._launch_keyless(launch_kwargs), False
        raise last_err

    async def _launch_worker(self, key: str, proxy: str | None) -> _Worker:
        """Launch a browser whose proxy, timezone, locale and WebRTC IP all match one egress identity."""
        launch_kwargs: dict[str, Any] = {
            "headless": self._headless,
            "humanize": True,
            "human_preset": "careful",
            "_suppress_maximize": True,
            "args": self._launch_args(),
        }
        if proxy:
            launch_kwargs["proxy"] = browser_proxy(proxy)

        timezone: str | None = None
        exit_ip: str | None = None
        if self._geoip:
            try:
                timezone, locale, exit_ip = await asyncio.to_thread(cb.maybe_resolve_geoip, True, launch_kwargs.get("proxy"), None, None, None)
                launch_kwargs.update(timezone=timezone, locale=locale)
                if exit_ip:
                    launch_kwargs["args"].append(f"--fingerprint-webrtc-ip={exit_ip}")
                logger.info("GeoIP for %s: tz=%s locale=%s", mask_proxy(proxy), timezone, locale)
            except Exception as err:
                if proxy and is_proxy_error(str(err)):
                    raise RuntimeError(f"Proxy {mask_proxy(proxy)} unreachable: {redact(str(err))}") from err
                logger.warning("GeoIP resolution failed for %s (%s); launching with default timezone/locale", mask_proxy(proxy), redact(str(err)))
                launch_kwargs["args"].append("--lang=en-US,en")
                if proxy:
                    # WebRTC must still report the proxy exit IP, otherwise STUN candidates leak the server IP
                    exit_ip = await asyncio.to_thread(resolve_proxy_exit_ip, launch_kwargs["proxy"])
                    if not exit_ip:
                        raise RuntimeError(f"Proxy {mask_proxy(proxy)} unreachable: could not resolve its exit IP") from err
                    launch_kwargs["args"].append(f"--fingerprint-webrtc-ip={exit_ip}")

        use_license = self._may_license(proxy)
        browser, licensed = await self._launch_browser(launch_kwargs, use_license=use_license)
        now = time.monotonic()
        created_at = now
        if use_license and not licensed:
            # A busy seat is usually a previous container still shutting down: retry the licensed build once this
            # fallback process has been idle for a minute instead of running unlicensed for a full lifetime
            created_at = now - max(0, self._max_lifetime_sec - 60)
        logger.info(
            "Launched browser worker for %s (%d/%d, licensed=%s)",
            mask_proxy(proxy), len(self._workers) + 1, self._max_browsers, licensed,
        )
        return _Worker(
            key=key, proxy=proxy, browser=browser, created_at=created_at, last_used=now, timezone=timezone,
            licensed=licensed, exit_ip=exit_ip, exit_ip_checked_at=now,
        )

    def _is_stale(self, worker: _Worker) -> bool:
        """Disconnected, or past its lifetime with no open tabs (recycling drops HTTP cache and memory bloat)."""
        try:
            if not worker.browser.is_connected():
                return True
        except Exception:
            return True
        if worker.active:
            return False
        # The process holding the license seat stays warm much longer: relaunching it costs a seat round-trip and
        # a cold Cloudflare clearance
        lifetime = self._licensed_lifetime_sec if worker.licensed else self._max_lifetime_sec
        return worker.poisoned or (time.monotonic() - worker.created_at) >= lifetime

    def _schedule_exit_ip_probe(self, worker: _Worker) -> None:
        """Re-check a proxied worker's exit IP at most every BROWSER_EXIT_IP_CHECK_SEC, in the background: the probe
        is a network call through the proxy and must never run while holding the pool lock."""
        if worker.proxy is None or worker.poisoned or worker.key in self._probes:
            return
        if time.monotonic() - worker.exit_ip_checked_at < self._exit_ip_check_sec:
            return
        worker.exit_ip_checked_at = time.monotonic()
        task = asyncio.create_task(self._probe_exit_ip(worker))
        self._probes[worker.key] = task
        task.add_done_callback(lambda _t, key=worker.key: self._probes.pop(key, None))

    async def _probe_exit_ip(self, worker: _Worker) -> None:
        """On an exit-IP change, retire the process gracefully (no new tabs, closed once its tabs finish) and drop the
        clearances earned on the old IP. Same IP: nothing changes and the warm browser keeps serving."""
        try:
            current = await asyncio.wait_for(
                asyncio.to_thread(resolve_proxy_exit_ip, browser_proxy(worker.proxy)), timeout=BROWSER_CALL_TIMEOUT_SEC
            )
        except Exception as err:
            logger.debug("Exit IP check failed for %s: %s", mask_proxy(worker.proxy), redact(str(err)))
            return
        if not current or current == worker.exit_ip:
            return
        if worker.exit_ip is None:
            worker.exit_ip = current
            return
        if self._cond is None:
            self._cond = asyncio.Condition()
        async with self._cond:
            if self._workers.get(worker.key) is not worker or worker.poisoned:
                return
            logger.info(
                "Exit IP for %s changed %s -> %s: retiring its browser after %d open tab(s), dropping %d cookie jar(s)",
                mask_proxy(worker.proxy), worker.exit_ip, current, worker.active, store.invalidate_identity(worker.key),
            )
            worker.poisoned = True
            self._cond.notify_all()

    async def _acquire(self, proxy: str | None, deadline: float) -> _Worker:
        """Reserve a tab on the browser for this egress identity, launching or evicting processes as needed.

        Every wait is bounded by `deadline` (monotonic): a busy or wedged pool fails fast with BrowserPoolBusy instead
        of parking requests forever.
        """
        if self._cond is None:
            self._cond = asyncio.Condition()
        key = proxy_key(proxy)
        async with self._cond:
            while True:
                worker = self._workers.get(key)
                if worker is not None and self._is_stale(worker):
                    logger.info(
                        "Recycling browser worker for %s (age=%.1fs, poisoned=%s)",
                        mask_proxy(proxy), time.monotonic() - worker.created_at, worker.poisoned,
                    )
                    del self._workers[key]
                    await self._close_browser_safely(worker.browser)
                    worker = None

                if worker is not None:
                    self._schedule_exit_ip_probe(worker)

                full = len(self._workers) >= self._max_browsers
                tabs_free = sum(w.active for w in self._workers.values()) < self._max_tabs
                if worker is not None:
                    # Drain instead of joining while another identity waits for a slot, so it cannot starve;
                    # a poisoned process takes no new tabs
                    if tabs_free and not worker.poisoned and not (full and self._others_waiting(key)):
                        worker.active += 1
                        return worker
                elif not full:
                    if tabs_free:
                        worker = await self._launch_worker(key, proxy)
                        self._workers[key] = worker
                        worker.active += 1
                        return worker
                else:
                    idle = [w for w in self._workers.values() if w.active == 0]
                    if idle:
                        # Keep the licensed (warm, seat-holding) process: evict keyless ones first, then LRU
                        victim = min(idle, key=lambda w: (w.licensed, w.last_used))
                        logger.info("Browser slots full: closing idle worker for %s to serve %s", mask_proxy(victim.proxy), mask_proxy(proxy))
                        del self._workers[victim.key]
                        await self._close_browser_safely(victim.browser)
                        continue

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BrowserPoolBusy(
                        f"No browser slot for {mask_proxy(proxy)} within the queue budget "
                        f"({sum(w.active for w in self._workers.values())}/{self._max_tabs} tabs, "
                        f"{len(self._workers)}/{self._max_browsers} browsers busy)"
                    )

                # Every process is busy (or draining for another identity): wait for a tab to finish
                waiting = worker is None
                if waiting:
                    self._waiting[key] = self._waiting.get(key, 0) + 1
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue  # loop once more: re-check, then raise BrowserPoolBusy
                finally:
                    if waiting:
                        self._waiting[key] -= 1
                        if not self._waiting[key]:
                            del self._waiting[key]

    def _others_waiting(self, key: str) -> bool:
        return any(k != key and k not in self._workers for k in self._waiting)

    async def _release(self, worker: _Worker) -> None:
        if self._cond is None:
            self._cond = asyncio.Condition()
        async with self._cond:
            worker.active = max(0, worker.active - 1)
            worker.last_used = time.monotonic()
            self._cond.notify_all()

    async def stop(self) -> None:
        """Clean up all browser processes."""
        if self._cond is None:
            self._cond = asyncio.Condition()
        for task in list(self._probes.values()):
            task.cancel()
        async with self._cond:
            for worker in list(self._workers.values()):
                await self._close_browser_safely(worker.browser)
            self._workers.clear()
            self._cond.notify_all()
        logger.info("BrowserPool stopped")

    async def solve_and_fetch(
        self,
        url: str,
        cf_wait: float = 25.0,
        proxy: str | None = None,
        page_load_state: str = "complete",
        cookies: dict[str, str] | None = None,
        wait_for_selector: str | None = None,
        capture_token: bool = False,
        token_field_names: list[str] | None = None,
        click_selector: str | None = None,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: str | None = None,
    ) -> BrowserResult:
        """Acquire a slot, navigate to target, solve challenge if present, and harvest tokens.

        The whole job (queue wait included) is capped at cf_wait + BROWSER_JOB_OVERHEAD_SEC, so a hung Playwright call
        can never hold a tab, and with MAX_BROWSERS=1 the whole pool, indefinitely.
        """
        budget = cf_wait + BROWSER_JOB_OVERHEAD_SEC
        started = time.monotonic()
        deadline = started + budget
        worker = await self._acquire(proxy, min(deadline, started + BROWSER_QUEUE_TIMEOUT_SEC))
        try:
            return await asyncio.wait_for(
                self._run_context(
                    worker,
                    url,
                    cf_wait,
                    proxy,
                    page_load_state,
                    cookies,
                    wait_for_selector,
                    capture_token,
                    token_field_names,
                    click_selector,
                    method,
                    headers,
                    body,
                ),
                timeout=max(1.0, deadline - time.monotonic()),
            )
        except asyncio.TimeoutError:
            # Either the job deadline fired, or a single Playwright call stalled past BROWSER_CALL_TIMEOUT_SEC: both
            # mean the renderer may be wedged, so the process takes no new tabs and is replaced once it drains
            worker.poisoned = True
            if time.monotonic() >= deadline - 0.5:
                logger.error(
                    "Browser job on %s exceeded %.0fs hard deadline; tab cancelled, worker for %s marked for replacement",
                    url, budget, mask_proxy(proxy),
                )
                raise BrowserJobTimeout(f"Browser job exceeded its {budget:.0f}s deadline (cf_wait={cf_wait:.0f}s)") from None
            logger.error("Browser call stalled on %s; worker for %s marked for replacement", url, mask_proxy(proxy))
            raise BrowserJobTimeout(f"Browser stopped responding (a page call exceeded {BROWSER_CALL_TIMEOUT_SEC:.0f}s)") from None
        finally:
            await self._release(worker)

    async def _is_bypassed(self, page: Any) -> bool:
        """Check if challenge wall has cleared via fast in-browser DOM probe."""
        try:
            return bool(await _bounded(page.evaluate(_IS_BYPASSED_JS)))
        except Exception:
            return False

    async def _turnstile_pending(self, page: Any) -> bool:
        """True when an embedded Turnstile widget exists on the page and has not yet issued a token."""
        try:
            state = await _bounded(page.evaluate(_TURNSTILE_STATE_JS))
            return bool(state and state.get("present") and not state.get("solved"))
        except Exception:
            return False

    async def _has_selector(self, page: Any, selector: str) -> bool:
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

    async def _is_ready(self, page: Any, selector: str | None) -> bool:
        """Wall cleared, no pending embedded Turnstile, and (if requested) target selector rendered."""
        if not await self._is_bypassed(page):
            return False
        if await self._turnstile_pending(page):
            return False
        return selector is None or await self._has_selector(page, selector)

    async def _click_turnstile_checkbox(self, page: Any) -> bool:
        """Click the Turnstile checkbox, bounded: humanized mouse moves on a wedged frame must not stall the solve loop."""
        started = time.monotonic()
        try:
            return await asyncio.wait_for(self._click_turnstile_once(page), timeout=BROWSER_CLICK_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            logger.warning("Turnstile click attempt timed out after %.0fs; continuing solve loop", BROWSER_CLICK_TIMEOUT_SEC)
            return False
        finally:
            took = time.monotonic() - started
            if took > 5.0:
                logger.info("Turnstile click attempt took %.1fs (host under load?)", took)

    async def _click_turnstile_once(self, page: Any) -> bool:
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

    async def _run_context(
        self,
        worker: _Worker,
        url: str,
        cf_wait: float,
        proxy: str | None,
        page_load_state: str,
        cookies: dict[str, str] | None,
        wait_for_selector: str | None = None,
        capture_token: bool = False,
        token_field_names: list[str] | None = None,
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
        user_agent: str | None = self._default_browser_ua
        ts_token: str | None = None
        ts_sitekey: str | None = None
        ts_solved_at: float | None = None
        initial_clearance: str | None = None
        attempted_clearance_reload: bool = False
        origin_refused = False

        try:
            context_kwargs: dict[str, Any] = {"viewport": {"width": 1280, "height": 720}}
            if not self._geoip:
                context_kwargs["locale"] = "en-US"

            context = await worker.browser.new_context(**context_kwargs)

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

            if headers:
                with contextlib.suppress(Exception):
                    await page.set_extra_http_headers(headers)

            if method and method.upper() == "POST":
                async def _handle_initial_post(route: Any) -> None:
                    req = route.request
                    if req.is_navigation_request() and req.url.split("#")[0] == url.split("#")[0]:
                        await route.continue_(method="POST", post_data=body or "")
                    else:
                        await route.continue_()
                with contextlib.suppress(Exception):
                    await page.route(url, _handle_initial_post)

            logger.info("cloakbrowser: navigating -> %s (timeout=%.1fs)", url, timeout_ms / 1000)
            try:
                resp = await page.goto(url, wait_until=load_state, timeout=timeout_ms)
                if resp is not None and getattr(resp, "status", None):
                    status_code = resp.status
                    resp_headers = dict(getattr(resp, "headers", None) or {})
            except Exception as nav_err:
                if proxy and is_proxy_error(str(nav_err)):
                    raise RuntimeError(f"Proxy {mask_proxy(proxy)} failed: {redact(str(nav_err).splitlines()[0])}") from nav_err
                logger.warning("cloakbrowser navigation warning on %s: %s", url, redact(str(nav_err)))

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
                extended = False
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

                while not await self._is_ready(page, selector):
                    if time.monotonic() >= deadline:
                        break
                    if last_doc.get("status") in _ORIGIN_REFUSAL_STATUSES and await self._is_bypassed(page):
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
                            if await self._is_bypassed(page):
                                logger.info("Page successfully cleared after single clearance reload!")
                                break

                    # 2. Throttled interactive click attempt
                    if time.monotonic() - last_click >= 2.5:
                        if await self._click_turnstile_checkbox(page):
                            last_click = time.monotonic()
                            for _ in range(15 if await self._is_bypassed(page) else 0):
                                await asyncio.sleep(0.2)
                                with contextlib.suppress(Exception):
                                    tok = (await _bounded(page.evaluate(_TURNSTILE_TOKEN_JS, token_field_names or []))).get("token")
                                    if tok:
                                        ts_token = tok
                                        break

                    # 3. Handle embedded widget after interstitial wall
                    if not extended and await self._is_bypassed(page) and await self._turnstile_pending(page):
                        extended = True
                        deadline = max(deadline, time.monotonic() + 20.0)
                        notes.append("Embedded widget after wall -> solve window extended")

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
                if is_fresh_final and not await self._is_bypassed(page) and not attempted_clearance_reload:
                    attempted_clearance_reload = True
                    logger.info("Fresh clearance present at loop exit -> executing single reload to target URL")
                    notes.append("Executing reload with clearance cookie")
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
                        title = await _bounded(page.title())
                        st = await _bounded(page.evaluate(_TURNSTILE_STATE_JS))
                        frames = [f.url[:90] for f in page.frames if "challenges.cloudflare" in (f.url or "")]
                        cbs = []
                        for f in page.frames:
                            if "challenges.cloudflare" in (f.url or ""):
                                try:
                                    cbs.append(await _bounded(f.evaluate(_FIND_CHECKBOX_JS)))
                                except Exception as fe:
                                    cbs.append(f"err:{fe}"[:80])
                        notes.append(f"debug: title={title!r} state={st} cf_frames={frames} checkboxes={cbs}")
                    except Exception as de:
                        notes.append(f"debug failed: {de}")

            # Final state capture
            bypassed = await self._is_bypassed(page)
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
                    bypassed = await self._is_bypassed(page)

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
            if bypassed and selector and not origin_refused and not await self._has_selector(page, selector):
                notes.append(f"Timed out waiting for selector '{selector}' (cf_wait={cf_wait}s)")
                logger.warning("Selector '%s' never appeared on %s within cf_wait budget", selector, url)
            final_url = page.url or url

            widget_present = False
            # Embedded widgets are injected ~2s after the real page loads, so right after a wall solve "no widget yet"
            # is not final: give it a short window to appear before concluding the page has none
            widget_deadline = time.monotonic() + (5.0 if wall and not origin_refused else 0.0)
            while True:
                with contextlib.suppress(Exception):
                    widget_present = bool((await _bounded(page.evaluate(_TURNSTILE_STATE_JS))).get("present"))
                if widget_present or time.monotonic() >= widget_deadline:
                    break
                await asyncio.sleep(1.0)
            if ts_token and not capture_token:
                capture_token = True
                notes.append("Token caught right after solve -> capturing")
            if not widget_present and not capture_token and wall:
                # The widget may already be solved and gone from the DOM while its token input still holds the value
                with contextlib.suppress(Exception):
                    if (await _bounded(page.evaluate(_TURNSTILE_TOKEN_JS, token_field_names or []))).get("token"):
                        capture_token = True
                        notes.append("Solved Turnstile token found in DOM -> capturing")
            if widget_present and not capture_token and not origin_refused:
                capture_token = True
                notes.append("Turnstile widget detected -> auto-capturing token")
            if capture_token and not origin_refused:
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
                            info = await _bounded(page.evaluate(_TURNSTILE_TOKEN_JS, token_field_names or []))
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

            if click_selector and not origin_refused:
                logger.info("Handling click_selector '%s'...", click_selector)
                # Fail-fast: only wait for disabled flag to clear if selector actually exists in DOM
                has_target = False
                with contextlib.suppress(Exception):
                    has_target = bool(await _bounded(page.eval_on_selector(click_selector, "el => true")))

                if has_target:
                    for _ in range(15):
                        btn_disabled = True
                        note = ""
                        with contextlib.suppress(Exception):
                            note = (await _bounded(page.eval_on_selector("#note", "el => el.innerText")) or "").lower()
                        with contextlib.suppress(Exception):
                            btn_disabled = await _bounded(page.eval_on_selector(click_selector, "el => el.disabled"))
                        if not btn_disabled or "verified" in note or ts_token:
                            break
                        await asyncio.sleep(1.0)
                else:
                    logger.debug("Selector '%s' not present in DOM; skipping disabled-wait loop", click_selector)

                clicked = False
                try:
                    _CLICK_ACTION_JS = """(sel) => {
                        const btn = document.querySelector(sel);
                        if (btn) {
                            btn.disabled = false;
                            btn.click();
                            return true;
                        }
                        const f = document.querySelector('form');
                        if (f) {
                            f.submit();
                            return true;
                        }
                        return false;
                    }"""
                    clicked = await _bounded(page.evaluate(_CLICK_ACTION_JS, click_selector))
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
                            html_check = await _bounded(page.content())
                            if any(k in html_check for k in ("video-downloads", "googleusercontent", "r2.dev", "vidzer", ".mkv", ".mp4")):
                                break
                        except Exception:
                            pass
                        if final_url != url:
                            break

            body = ""
            for _ in range(5):
                try:
                    body = await _bounded(page.content())
                    final_url = page.url or final_url
                    break
                except Exception:
                    await asyncio.sleep(1.0)

            if "too many download pages were opened from your network" in body.lower():
                status = 429
                notes.append("Origin IP download rate limit detected in body")

            raw_cookies = await _bounded(context.cookies())
            harvested = {c["name"]: c["value"] for c in raw_cookies}

            with contextlib.suppress(Exception):
                ua = await _bounded(page.evaluate("navigator.userAgent"))
                if ua and isinstance(ua, str) and len(ua) > 10:
                    user_agent = ua
                    if not self._default_browser_ua:
                        self._default_browser_ua = ua

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
            turnstile_token=ts_token,
            turnstile_sitekey=ts_sitekey,
            turnstile_solved_at=ts_solved_at,
            token_submissions=submissions,
        )


pool = BrowserPool()
