"""Standalone Turnstile token minting backing POST /solve-turnstile.

cv3inx-style route interception: the target document is served a minimal host page from the siteurl's own origin
(sitekey domain binding holds), the widget renders there, and a click loop coaxes managed widgets until the callback
hands over the token.

Tokens are single-use and expire after 300s, so they can never be shared or cached for reuse. What scales is the
minting: each (sitekey, siteurl, egress) gets one minter that keeps a warm widget page open and calls
`turnstile.reset()` for the next token instead of opening a new context per request. Requests queue on that minter
(oldest first); under a burst it pre-mints a few tokens, each handed out once and dropped before it expires.
Same-sitekey minting stays serialised by default — parallel solves on one identity escalate Cloudflare difficulty.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import time

from app.core.browser import pool
from app.core.config import (
    BROWSER_QUEUE_TIMEOUT_SEC,
    TURNSTILE_MAX_QUEUE,
    TURNSTILE_MINTERS_PER_KEY,
    TURNSTILE_PREFETCH,
)
from app.core.utils import challenge
from app.core.utils.playwright import bounded

logger = logging.getLogger(__name__)

# The interactive widget shape: an explicit render with a callback-captured token.
# An implicit/invisible widget mints tokens the target's backend rejects even though siteverify passes.
_STUB = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"/>
<title>Turnstile</title>
<style>
html,body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#f5f5f5;font-family:system-ui,sans-serif}
.turnstile{min-width:300px;min-height:65px}
</style>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?onload=onloadTurnstileCallback" defer></script>
<script>
window.__tok = ''; window.__err = '';
window.onloadTurnstileCallback = function () {
  window.__wid = turnstile.render('.turnstile', {
    sitekey: '__SITEKEY__',
    size: 'normal',
    callback: function (token) { window.__tok = token; },
    'error-callback': function (code) { window.__err = String(code || 'unknown'); return true; },
    'expired-callback': function () { window.__tok = ''; }
  });
};
</script>
</head><body><div class="turnstile"></div></body></html>"""

# Read-and-clear, so one token is never handed out twice
_TAKE_JS = "() => { const t = window.__tok || ''; const e = window.__err || ''; window.__tok = ''; window.__err = ''; return [t, e]; }"
_RESET_JS = "() => { window.__tok = ''; window.__err = ''; try { turnstile.reset(window.__wid); return true; } catch (e) { return false; } }"

# Tokens live 300s at Cloudflare; never hand out one the caller cannot redeem in time
_TOKEN_TTL_SEC = 270.0
# One token attempt before the page is reloaded; three failed attempts in a row fail the queue
_MINT_ATTEMPT_SEC = 45.0
_MAX_FAILED_ATTEMPTS = 3
# How long a minter keeps its warm widget page after the last request (sequential callers reuse it)
_WARM_KEEP_SEC = 60.0
# A minter gives its browser tab back this often, so the worker can still be recycled under constant demand
_MINTER_SESSION_SEC = 300.0
# Demand window that sizes pre-minting
_DEMAND_WINDOW_SEC = 60.0
_MAX_KEYS = 512

# Cloudflare widget error codes surfaced through the error-callback, with what they mean for the caller.
_ERROR_DESCRIPTIONS = {
    "110100": "invalid sitekey",
    "110110": "sitekey not found",
    "110200": "domain not authorized for this sitekey",
    "110600": "challenge timed out",
    "110620": "interaction timed out",
    "200500": "turnstile iframe failed to load",
    "400020": "invalid sitekey",
}


def _is_fatal(code: str) -> bool:
    """Configuration errors (bad sitekey, unauthorized domain) never succeed on retry; challenge failures may."""
    return code.startswith(("1101", "1102", "4000"))


def _stub_html(sitekey: str) -> str:
    return _STUB.replace("__SITEKEY__", sitekey)


def _widget_error(code: str) -> RuntimeError:
    return RuntimeError(f"Cloudflare Turnstile error {code}: {_ERROR_DESCRIPTIONS.get(code, 'unknown Turnstile error')}")


class _Minter:
    """Token supply for one (sitekey, siteurl, egress): pooled tokens, queued requests and the tasks minting."""

    def __init__(self, sitekey: str, siteurl: str, proxy: str | None) -> None:
        self.sitekey, self.siteurl, self.proxy = sitekey, siteurl, proxy
        self.tokens: collections.deque[tuple[str, float]] = collections.deque()
        self.waiters: collections.deque[asyncio.Future] = collections.deque()
        self.demand: collections.deque[float] = collections.deque()
        self.tasks: set[asyncio.Task] = set()
        # Set when a request queues, to wake a minter idling on its warm page
        self.wakeup = asyncio.Event()

    # -- supply and demand -------------------------------------------------------------------------------------

    def _prune(self) -> None:
        now = time.monotonic()
        while self.tokens and now - self.tokens[0][1] > _TOKEN_TTL_SEC:
            self.tokens.popleft()
        while self.demand and now - self.demand[0] > _DEMAND_WINDOW_SEC:
            self.demand.popleft()
        while self.waiters and self.waiters[0].done():
            self.waiters.popleft()

    def take(self) -> str | None:
        self._prune()
        return self.tokens.popleft()[0] if self.tokens else None

    def live_waiters(self) -> int:
        return sum(1 for f in self.waiters if not f.done())

    def prefetch_target(self) -> int:
        # A lone request mints on demand only; a burst keeps a few tokens ready for the next requests
        return min(TURNSTILE_PREFETCH, max(0, len(self.demand) - 1))

    def needs_tokens(self) -> bool:
        self._prune()
        return self.live_waiters() > 0 or len(self.tokens) < self.prefetch_target()

    def deliver(self, token: str) -> None:
        while self.waiters:
            fut = self.waiters.popleft()
            if not fut.done():
                fut.set_result(token)
                return
        self.tokens.append((token, time.monotonic()))

    def fail_waiters(self, exc: BaseException) -> None:
        while self.waiters:
            fut = self.waiters.popleft()
            if not fut.done():
                fut.set_exception(exc)

    def idle(self) -> bool:
        self._prune()
        return not self.tasks and not self.tokens and not self.live_waiters()

    def ensure_minting(self) -> None:
        wanted = min(TURNSTILE_MINTERS_PER_KEY, max(1, self.live_waiters()))
        while len(self.tasks) < wanted:
            task = asyncio.create_task(self._run())
            self.tasks.add(task)
            task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.fail_waiters(task.exception())
        elif self.live_waiters() and not self.tasks:
            self.ensure_minting()  # demand arrived while this session was winding down
        if self.idle():
            _minters.pop(_key(self.sitekey, self.siteurl, self.proxy), None)

    # -- minting -----------------------------------------------------------------------------------------------

    async def _run(self) -> None:
        """Hold one browser tab and mint tokens on a warm widget page while anyone needs them."""
        async with pool.slot(self.proxy, time.monotonic() + BROWSER_QUEUE_TIMEOUT_SEC) as worker:
            context = await worker.browser.new_context()
            try:
                page = await context.new_page()
                await self._open(page)
                session_end = time.monotonic() + _MINTER_SESSION_SEC
                failures = 0
                used = False  # the widget's token was handed out: reset it before minting the next one
                while time.monotonic() < session_end:
                    if not self.needs_tokens():
                        # Keep the warm page a little while: the next request skips the context, page load and
                        # widget render and only waits for a reset. Give the tab back early if the pool is queuing
                        if not await self._wait_for_demand():
                            break
                        continue
                    if used:
                        await bounded(page.evaluate(_RESET_JS))
                        used = False
                    try:
                        token = await self._mint_one(page, worker)
                    except TimeoutError:
                        failures += 1
                        if failures >= _MAX_FAILED_ATTEMPTS:
                            raise TimeoutError(f"Turnstile token not obtained after {failures} attempts") from None
                        await self._open(page)  # a stuck widget: start over on a fresh load
                        continue
                    failures = 0
                    self.deliver(token)
                    used = True
            finally:
                try:
                    await asyncio.wait_for(context.close(), timeout=5.0)
                except Exception:
                    pass

    async def _wait_for_demand(self) -> bool:
        """True when a new request arrived within _WARM_KEEP_SEC; False when the page should be closed (idle long
        enough, or browser tabs are needed by queued requests)."""
        deadline = time.monotonic() + _WARM_KEEP_SEC
        self.wakeup.clear()
        while time.monotonic() < deadline:
            if pool.stats()["queued"]:
                return False
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=min(1.0, max(0.05, deadline - time.monotonic())))
                return True
            except asyncio.TimeoutError:
                continue
        return False

    async def _open(self, page) -> None:
        stub_body = _stub_html(self.sitekey)
        target = self.siteurl.rstrip("/")

        async def _serve_stub(route):
            await route.fulfill(body=stub_body, status=200, content_type="text/html; charset=utf-8")

        async def _abort(route):
            await route.abort()

        if not getattr(page, "_anti_routed", False):
            await page.route(lambda u: u.split("#")[0].rstrip("/") == target, _serve_stub)
            # CF posts solved-challenge telemetry to this endpoint; aborting keeps the stub origin silent
            await page.route("**/reports/v0/post**", _abort)
            page._anti_routed = True
        await page.goto(self.siteurl, wait_until="domcontentloaded", timeout=30000)

    async def _mint_one(self, page, worker) -> str:
        deadline = time.monotonic() + _MINT_ATTEMPT_SEC
        last_click = 0.0
        while True:
            try:
                token, code = await bounded(page.evaluate(_TAKE_JS), timeout=10)
            except asyncio.TimeoutError:
                # A 10s page.evaluate stall means the renderer is wedged: retire the process once it drains,
                # the same rule /fetch applies
                worker.poisoned = True
                raise RuntimeError("Browser stopped responding during the Turnstile solve") from None
            if token and len(token) > 10:
                return token
            if code:
                if _is_fatal(code):
                    raise _widget_error(code)
                logger.info("Turnstile widget error %s for %s; resetting", code, self.sitekey)
                await bounded(page.evaluate(_RESET_JS))
            now = time.monotonic()
            if now - last_click >= 2.5:
                if await challenge.click_turnstile_once(page):
                    last_click = now
            if now >= deadline:
                raise TimeoutError
            await asyncio.sleep(0.4)


_minters: dict[str, _Minter] = {}


def _key(sitekey: str, siteurl: str, proxy: str | None) -> str:
    return f"{sitekey}|{siteurl}|{proxy or 'direct'}"


def _evict_idle() -> None:
    if len(_minters) <= _MAX_KEYS:
        return
    for key in [k for k, m in _minters.items() if m.idle()]:
        _minters.pop(key, None)


async def solve(sitekey: str, siteurl: str, proxy: str | None = None, timeout: int = 30) -> dict:
    """Return one fresh, never-shared token: {token, elapsed, pooled}. Raises TimeoutError when none arrives in time,
    BrowserPoolBusy when the queue for this sitekey is full, RuntimeError on a widget/browser failure."""
    from app.core.browser import BrowserPoolBusy

    started = time.monotonic()
    key = _key(sitekey, siteurl, proxy)
    minter = _minters.get(key)
    if minter is None:
        _evict_idle()
        minter = _minters[key] = _Minter(sitekey, siteurl, proxy)
    minter.demand.append(started)

    token = minter.take()
    if token:
        if minter.needs_tokens():
            minter.ensure_minting()  # top the pool back up for the rest of the burst
        return {"token": token, "elapsed": round(time.monotonic() - started, 2), "pooled": True}

    if minter.live_waiters() >= TURNSTILE_MAX_QUEUE:
        raise BrowserPoolBusy(
            f"Turnstile queue for this sitekey is full ({TURNSTILE_MAX_QUEUE} waiting); retry shortly"
        )
    fut = asyncio.get_running_loop().create_future()
    minter.waiters.append(fut)
    minter.wakeup.set()
    minter.ensure_minting()
    try:
        token = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(f"Turnstile token not obtained within {timeout}s") from None
    finally:
        if minter.idle():
            _minters.pop(key, None)
    return {"token": token, "elapsed": round(time.monotonic() - started, 2), "pooled": False}


def stats() -> dict[str, int]:
    """Minter counts for /health (no sitekeys: the endpoint is unauthenticated)."""
    for m in _minters.values():
        m._prune()
    return {
        "sitekeys": len(_minters),
        "minting": sum(len(m.tasks) for m in _minters.values()),
        "pooled_tokens": sum(len(m.tokens) for m in _minters.values()),
        "queued": sum(m.live_waiters() for m in _minters.values()),
    }
