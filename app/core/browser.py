"""Stealth browser automation engine backed by CloakBrowser with automated Turnstile solving."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import cloakbrowser as cb
from cloakbrowser.geoip import resolve_proxy_exit_ip

from app.core.config import (
    BROWSER_CALL_TIMEOUT_SEC,
    BROWSER_EXIT_IP_CHECK_SEC,
    BROWSER_LICENSED_LIFETIME_SEC,
    BROWSER_CLICK_TIMEOUT_SEC,
    BROWSER_GEOIP,
    BROWSER_JOB_OVERHEAD_SEC,
    BROWSER_QUEUE_TIMEOUT_SEC,
    BROWSER_HEADLESS,
    BROWSER_MAX_LIFETIME_SEC,
    MAX_BROWSERS,
    MAX_TABS,
    PROXY_QUEUE_BYTE_LIMIT,
)
from app.core.cookie_store import store
from app.core.utils.challenge import click_turnstile_once
from app.core.utils.playwright import bounded as _bounded  # noqa: F401  (tests call browser_mod._bounded)
from app.core.proxy import browser_proxy, is_proxy_error, mask_proxy, proxy_key, redact

logger = logging.getLogger(__name__)

# Native closed-shadow-root access via patched Chromium
FAKE_SHADOW_ARG = "--enable-blink-features=FakeShadowRoot"


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
    # Every main-frame navigation the browser made: {"url", "status", "location"} per hop (redirects included)
    redirect_chain: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    # Set when the page turned into a file download: the file was not fetched, final_url is its address
    note: str | None = None


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


class ProxySolvingDisabled(RuntimeError):
    """Proxied solving is turned off (MAX_BROWSERS=0): no proxy browser may open (HTTP 409)."""


class BrowserJobTimeout(RuntimeError):
    """A browser job exceeded its hard deadline and was cancelled (HTTP 504)."""


# Licensed-launch retries when the single seat is still held by a just-closed process
_SEAT_RETRIES = 2
_SEAT_RETRY_DELAY_SEC = 2.5
# Ceiling for one worker launch (GeoIP lookups + seat retries + Chromium start)
_LAUNCH_TIMEOUT_SEC = 90.0
# After an "invalid or expired" license answer, retry the licensed build only this often. Kept short: the free plan
# reports a seat held by another machine the same way, and that clears on its own
_LICENSE_DENIED_RETRY_SEC = 600.0


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
        licensed_lifetime_sec: int = BROWSER_LICENSED_LIFETIME_SEC,
        exit_ip_check_sec: float = BROWSER_EXIT_IP_CHECK_SEC,
    ) -> None:
        self._max_tabs = max_tabs
        self._headless = headless
        self._max_lifetime_sec = max_lifetime_sec
        # Bounds concurrent PROXIED browsers; the direct licensed PRO browser lives outside this budget.
        self._max_browsers = max_browsers
        self._geoip = geoip
        self._licensed_lifetime_sec = licensed_lifetime_sec
        self._exit_ip_check_sec = exit_ip_check_sec
        self._cond: asyncio.Condition | None = None
        self._workers: dict[str, _Worker] = {}
        self._waiting: dict[str, int] = {}
        self._probes: dict[str, asyncio.Task] = {}
        # Identities whose browser is being launched right now (key -> proxy), and retired processes still closing
        self._launching: dict[str, str | None] = {}
        self._closing: set[asyncio.Task] = set()
        # Serializes only the Chromium start itself (it edits the process environment), never the pool
        self._launch_lock = asyncio.Lock()
        # Total request-body bytes of proxied requests currently queued for a proxy slot.
        self._proxy_queue_bytes = 0
        self._default_browser_ua: str | None = None
        # Set when the license server rejected the key outright: launch keyless until then
        self._license_denied_until = 0.0

    async def start(self) -> None:
        """Pre-warm browser environment and initialize concurrency primitives."""
        self._cond = asyncio.Condition()
        # Binaries are ensured, updated and pruned in the background (app.core.binaries) under the launch lock, so
        # start-up never blocks the event loop on a download and no launch races one
        from app.core import binaries

        self._binaries_task = asyncio.create_task(binaries.run(self._launch_lock))

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
            "BrowserPool initialized | max_concurrent=%d | max_proxied_browsers=%d | headless=%s | geoip=%s | max_lifetime=%ds",
            self._max_tabs,
            self._max_browsers,
            self._headless,
            self._geoip,
            self._max_lifetime_sec,
        )

    def stats(self) -> dict[str, int]:
        """Process counts for diagnostics (no proxy details: /health is unauthenticated)."""
        return {
            "browsers": len(self._workers),
            "max_browsers": self._max_browsers,
            "proxy_queue_bytes": self._proxy_queue_bytes,
            "active_tabs": sum(w.active for w in self._workers.values()),
            "max_tabs": self._max_tabs,
            "queued": sum(self._waiting.values()),
            "licensed_browsers": sum(1 for w in self._workers.values() if w.licensed),
        }

    def _may_license(self, proxy: str | None) -> bool:
        """The single license seat goes to the default browser: the WARP one when WARP_PROXY is set, otherwise the
        direct one. Never both: the second licensed launch only hits "session limit", waits out the seat retries
        (~9s measured) and ends up keyless anyway. Every other proxy always runs keyless."""
        from app.core.config import WARP_PROXY

        if proxy is None:
            return not WARP_PROXY
        if not WARP_PROXY:
            return False
        try:
            from app.core.proxy import normalize_proxy
            return normalize_proxy(proxy) == normalize_proxy(WARP_PROXY)
        except Exception:
            return proxy == WARP_PROXY

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
        """Close a browser; if it does not answer within 5s, kill its Playwright driver process. Chromium runs on a
        debugging pipe owned by that driver and exits when the pipe closes, so a wedged process is never orphaned."""
        if browser is None:
            return
        try:
            await asyncio.wait_for(browser.close(), timeout=5.0)
            return
        except Exception as err:
            logger.warning("Browser close did not finish (%s); killing its driver process", type(err).__name__)
        with contextlib.suppress(Exception):
            browser._impl_obj._connection._transport._proc.kill()

    async def _launch_keyless(self, launch_kwargs: dict[str, Any]) -> Any:
        """Launch the keyless build (v146): needs no license seat. The key is hidden from the environment for the
        duration of the call, under the launch lock so a concurrent licensed launch never sees it missing."""
        fallback_kwargs = dict(launch_kwargs)
        fallback_kwargs["license_key"] = None
        async with self._launch_lock:
            saved_key = os.environ.pop("CLOAKBROWSER_LICENSE_KEY", None)
            try:
                return await cb.launch_async(**fallback_kwargs)
            finally:
                if saved_key:
                    os.environ["CLOAKBROWSER_LICENSE_KEY"] = saved_key

    async def _launch_browser(self, launch_kwargs: dict[str, Any], use_license: bool = True) -> tuple[Any, bool]:
        """Launch CloakBrowser; on a license/seat error fall back to the unauthenticated engine. Returns (browser, licensed)."""
        if not use_license or time.monotonic() < self._license_denied_until:
            return await self._launch_keyless(launch_kwargs), False
        last_err: Exception | None = None
        for attempt in range(_SEAT_RETRIES + 1):
            try:
                async with self._launch_lock:
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
            if "invalid or expired" in err_msg:
                # A dead key stays dead: stop re-validating it on every launch and recycling the keyless fallback
                # after a minute to retry the seat (that churn costs a cold Cloudflare clearance each time)
                self._license_denied_until = time.monotonic() + _LICENSE_DENIED_RETRY_SEC
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
                if proxy and (is_proxy_error(str(err)) or "timed out" in str(err).lower()):
                    # The GeoIP lookup goes through the proxy: a timeout means the proxy is dead, and a second
                    # 20s exit-IP lookup below would only hold the pool lock longer
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
        if use_license and not licensed and now >= self._license_denied_until:
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

    async def _acquire(self, proxy: str | None, deadline: float, payload_bytes: int = 0) -> _Worker:
        """Reserve a tab for this egress identity, launching or evicting processes as needed.

        Waits are bounded by `deadline`; MAX_BROWSERS bounds proxied browsers only; proxied waiters charge
        `payload_bytes` against PROXY_QUEUE_BYTE_LIMIT so a request flood cannot buffer unbounded memory.
        """
        if self._cond is None:
            self._cond = asyncio.Condition()
        if proxy is not None and self._max_browsers == 0:
            raise ProxySolvingDisabled(
                "Proxy solving is disabled on this server (MAX_BROWSERS=0): no proxy browser may be opened. "
                "Send the request without a proxy field, or with proxy='direct'."
            )
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
                    self._close_in_background(worker.browser)
                    worker = None

                if worker is not None:
                    self._schedule_exit_ip_probe(worker)

                proxied_workers = [w for w in self._workers.values() if w.proxy is not None]
                # A launch in flight holds its process slot and one tab, so nothing over-commits while it runs
                proxied_count = len(proxied_workers) + sum(1 for p in self._launching.values() if p is not None)
                # Only proxied traffic is bounded by MAX_BROWSERS; the direct licensed browser always fits.
                full = proxy is not None and proxied_count >= self._max_browsers
                tabs_used = sum(w.active for w in self._workers.values()) + len(self._launching)
                tabs_free = tabs_used < self._max_tabs
                launching = key in self._launching
                if worker is not None:
                    # Drain instead of joining while another identity waits for a slot, so it cannot starve;
                    # a poisoned process takes no new tabs
                    if tabs_free and not worker.poisoned and not (full and self._others_waiting(key)):
                        worker.active += 1
                        return worker
                elif launching:
                    pass  # another request is starting this identity's browser: wait for it below
                elif not full:
                    if tabs_free:
                        self._launching[key] = proxy
                        break  # launch outside the lock (below)
                else:
                    idle = [w for w in proxied_workers if w.active == 0]
                    if idle:
                        # Evict the least recently used idle proxied browser to make room for the new identity;
                        # the direct licensed browser is never a victim.
                        victim = min(idle, key=lambda w: w.last_used)
                        logger.info("Proxy browser slots full: closing idle worker for %s to serve %s", mask_proxy(victim.proxy), mask_proxy(proxy))
                        del self._workers[victim.key]
                        self._close_in_background(victim.browser)
                        continue

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BrowserPoolBusy(
                        f"No browser slot for {mask_proxy(proxy)} within the queue budget "
                        f"({tabs_used}/{self._max_tabs} tabs, "
                        f"{proxied_count}/{self._max_browsers} proxy browsers busy)"
                    )

                # Every process is busy (or draining for another identity): wait for a tab to finish
                waiting = worker is None
                charged = waiting and proxy is not None and payload_bytes > 0
                if charged and self._proxy_queue_bytes + payload_bytes > PROXY_QUEUE_BYTE_LIMIT:
                    raise BrowserPoolBusy(
                        f"Proxy queue full: waiting requests already hold {self._proxy_queue_bytes}/"
                        f"{PROXY_QUEUE_BYTE_LIMIT} bytes of request bodies; retry shortly"
                    )
                if waiting:
                    self._waiting[key] = self._waiting.get(key, 0) + 1
                    if charged:
                        self._proxy_queue_bytes += payload_bytes
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue  # loop once more: re-check, then raise BrowserPoolBusy
                finally:
                    if waiting:
                        self._waiting[key] -= 1
                        if not self._waiting[key]:
                            del self._waiting[key]
                        if charged:
                            self._proxy_queue_bytes -= payload_bytes

        # The launch (GeoIP lookups through the proxy, seat retries, Chromium start) runs WITHOUT the pool lock: a
        # slow or dead proxy must not freeze tabs on other identities, nor the releases that free them
        worker = None
        try:
            worker = await asyncio.wait_for(self._launch_worker(key, proxy), timeout=_LAUNCH_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Browser launch for {mask_proxy(proxy)} did not finish within {_LAUNCH_TIMEOUT_SEC:.0f}s"
            ) from None
        finally:
            async with self._cond:
                self._launching.pop(key, None)
                if worker is not None:
                    if self._workers.get(key) is None:
                        self._workers[key] = worker
                        worker.active += 1
                    else:  # cannot happen while the reservation holds, but never leak a process
                        self._close_in_background(worker.browser)
                        worker = self._workers[key]
                        worker.active += 1
                self._cond.notify_all()
        return worker

    def _close_in_background(self, browser: Any) -> None:
        """Close a retired process without holding the pool lock (a wedged close takes up to 5s + kill)."""
        task = asyncio.create_task(self._close_browser_safely(browser))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    def _others_waiting(self, key: str) -> bool:
        return any(k != key and k not in self._workers for k in self._waiting)

    async def _release(self, worker: _Worker) -> None:
        if self._cond is None:
            self._cond = asyncio.Condition()
        async with self._cond:
            worker.active = max(0, worker.active - 1)
            worker.last_used = time.monotonic()
            self._cond.notify_all()

    @asynccontextmanager
    async def slot(self, proxy: str | None, deadline: float, payload_bytes: int = 0):
        """Reserve one worker tab for the duration of the block (used by the turnstile solver)."""
        worker = await self._acquire(proxy, deadline, payload_bytes=payload_bytes)
        try:
            yield worker
        finally:
            await self._release(worker)

    async def _run_context(self, worker, url, cf_wait, proxy, page_load_state, cookies,
                           wait_for_selector=None, click_selector=None, method="GET",
                           headers=None, body=None) -> BrowserResult:
        """Fetch pipeline delegate; tests monkeypatch this to stub out the tab work."""
        from app.core.fetch_flow import run_fetch_flow

        return await run_fetch_flow(
            self, worker, url, cf_wait, proxy, page_load_state, cookies,
            wait_for_selector, click_selector, method, headers, body,
        )

    async def stop(self) -> None:
        """Clean up all browser processes."""
        if self._cond is None:
            self._cond = asyncio.Condition()
        for task in list(self._probes.values()):
            task.cancel()
        if getattr(self, "_binaries_task", None):
            self._binaries_task.cancel()
        async with self._cond:
            workers = list(self._workers.values())
            self._workers.clear()
            self._cond.notify_all()
        await asyncio.gather(
            *(self._close_browser_safely(w.browser) for w in workers), *self._closing, return_exceptions=True
        )
        logger.info("BrowserPool stopped")

    async def solve_and_fetch(
        self,
        url: str,
        cf_wait: float = 25.0,
        proxy: str | None = None,
        page_load_state: str = "complete",
        cookies: dict[str, str] | None = None,
        wait_for_selector: str | None = None,
        click_selector: str | None = None,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: str | None = None,
        payload_bytes: int = 0,
    ) -> BrowserResult:
        """Acquire a slot, navigate to target, solve challenge if present, and harvest tokens.

        The whole job (queue wait included) is capped at cf_wait + BROWSER_JOB_OVERHEAD_SEC, so a hung Playwright call
        can never hold a tab, and with MAX_BROWSERS=1 the whole pool, indefinitely. `payload_bytes` is the request
        body size proxied requests charge against the 16MB proxy-queue cap while they wait for a slot.
        """
        budget = cf_wait + BROWSER_JOB_OVERHEAD_SEC
        started = time.monotonic()
        deadline = started + budget
        worker = await self._acquire(
            proxy, min(deadline, started + BROWSER_QUEUE_TIMEOUT_SEC), payload_bytes=payload_bytes
        )
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
        """Shadow-DOM + selector + bbox walk inside the CF iframe; see utils.challenge."""
        return await click_turnstile_once(page)

pool = BrowserPool()
