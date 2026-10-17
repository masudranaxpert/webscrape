"""Browser pool liveness: a hung tab must never freeze the pool (regression for a production outage).

Incident: one tab on the proxy browser never finished; with MAX_BROWSERS=1 every direct request queued for a
slot forever, the drain rule then blocked new proxy tabs too, and the whole service stopped serving browser requests.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.core.browser as browser_mod
from app.core.browser import BrowserJobTimeout, BrowserPool, BrowserPoolBusy, _Worker

PROXY = "http://a.example:1"


class _FakeBrowser:
    def __init__(self):
        self.closed = False

    def is_connected(self):
        return not self.closed

    async def close(self):
        self.closed = True


def _pool(max_browsers: int = 1, max_tabs: int = 5) -> tuple[BrowserPool, list[str | None]]:
    pool = BrowserPool(max_tabs=max_tabs, headless=True, max_lifetime_sec=3600, max_browsers=max_browsers, geoip=False)
    launched: list[str | None] = []

    async def fake_launch(key, proxy):
        launched.append(proxy)
        return _Worker(key=key, proxy=proxy, browser=_FakeBrowser(), created_at=time.monotonic(), last_used=0.0)

    pool._launch_worker = fake_launch
    return pool, launched


def _fake_jobs(pool: BrowserPool, hang_urls: set[str]) -> None:
    """Replace the real tab with a stub: URLs in `hang_urls` hang forever, like the stuck tab."""

    async def fake_run_context(worker, url, *args, **kwargs):
        if url in hang_urls:
            await asyncio.Event().wait()
        await asyncio.sleep(0.01)
        return f"ok:{url}"

    pool._run_context = fake_run_context


def _with_budget(job_overhead: float, queue_timeout: float):
    browser_mod.BROWSER_JOB_OVERHEAD_SEC = job_overhead
    browser_mod.BROWSER_QUEUE_TIMEOUT_SEC = queue_timeout


def test_hung_tab_hits_hard_deadline_and_frees_slot():
    async def run():
        _with_budget(job_overhead=0.3, queue_timeout=0.3)
        pool, _ = _pool()
        _fake_jobs(pool, {"https://hang"})
        t0 = time.monotonic()
        try:
            await pool.solve_and_fetch("https://hang", cf_wait=0, proxy=PROXY)
            raise AssertionError("hung job must not succeed")
        except BrowserJobTimeout:
            pass
        assert time.monotonic() - t0 < 1.5, "deadline did not fire"
        worker = pool._workers[proxy_id(PROXY)]
        assert worker.active == 0 and worker.poisoned, "slot not released / worker not flagged"

    asyncio.run(run())


def test_poisoned_worker_is_replaced_on_next_request():
    async def run():
        _with_budget(job_overhead=0.3, queue_timeout=0.3)
        pool, launched = _pool()
        _fake_jobs(pool, {"https://hang"})
        try:
            await pool.solve_and_fetch("https://hang", cf_wait=0, proxy=PROXY)
        except BrowserJobTimeout:
            pass
        first = pool._workers[proxy_id(PROXY)].browser
        assert await pool.solve_and_fetch("https://ok", cf_wait=0, proxy=PROXY) == "ok:https://ok"
        assert first.closed and launched == [PROXY, PROXY], "wedged process was not recycled"

    asyncio.run(run())


def test_outage_scenario_fails_fast_then_recovers():
    """Proxy tab hangs; direct and proxy traffic arrive: nothing may block past the budget, and the pool recovers."""

    async def run():
        _with_budget(job_overhead=0.6, queue_timeout=0.4)
        pool, _ = _pool(max_browsers=1)
        _fake_jobs(pool, {"https://target.example/stuck"})

        stuck = asyncio.create_task(pool.solve_and_fetch("https://target.example/stuck", cf_wait=0, proxy=PROXY))
        await asyncio.sleep(0.05)

        t0 = time.monotonic()
        direct = [asyncio.create_task(pool.solve_and_fetch(f"https://hmaal/{i}", cf_wait=0)) for i in range(4)]
        proxied = asyncio.create_task(pool.solve_and_fetch("https://target.example/next", cf_wait=0, proxy=PROXY))
        results = await asyncio.wait_for(asyncio.gather(*direct, proxied, stuck, return_exceptions=True), timeout=3)
        elapsed = time.monotonic() - t0

        assert elapsed < 2.0, f"requests were parked for {elapsed:.1f}s"
        assert isinstance(results[-1], BrowserJobTimeout), "stuck tab was not cancelled"
        for r in results[:-1]:
            assert isinstance(r, (BrowserPoolBusy, str)), f"unexpected outcome: {r!r}"
        assert pool.stats()["active_tabs"] == 0 and pool.stats()["queued"] == 0, "slots leaked"

        # Once the wedged tab is gone, both identities are served again
        assert await pool.solve_and_fetch("https://hmaal/after", cf_wait=0) == "ok:https://hmaal/after"
        assert await pool.solve_and_fetch("https://target.example/after", cf_wait=0, proxy=PROXY) == "ok:https://target.example/after"

    asyncio.run(run())


def test_queue_wait_is_bounded_when_pool_is_saturated():
    async def run():
        _with_budget(job_overhead=5.0, queue_timeout=0.3)
        pool, _ = _pool(max_browsers=1, max_tabs=1)
        _fake_jobs(pool, {"https://slow"})
        busy = asyncio.create_task(pool.solve_and_fetch("https://slow", cf_wait=0))
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        try:
            await pool.solve_and_fetch("https://other", cf_wait=0)
            raise AssertionError("saturated pool must reject with BrowserPoolBusy")
        except BrowserPoolBusy:
            pass
        assert time.monotonic() - t0 < 1.0
        assert pool.stats()["queued"] == 0
        busy.cancel()

    asyncio.run(run())


def test_tab_cap_spans_all_browsers():
    async def run():
        _with_budget(job_overhead=5.0, queue_timeout=0.2)
        pool, _ = _pool(max_browsers=2, max_tabs=2)
        w1 = await pool._acquire(None, time.monotonic() + 1)
        w2 = await pool._acquire(PROXY, time.monotonic() + 1)
        try:
            await pool._acquire(None, time.monotonic() + 0.2)
            raise AssertionError("third tab exceeded MAX_TABS")
        except BrowserPoolBusy:
            pass
        await pool._release(w1)
        w3 = await pool._acquire(None, time.monotonic() + 1)
        assert w3 is w1
        await pool._release(w2)
        await pool._release(w3)

    asyncio.run(run())


def test_stalled_page_call_is_bounded():
    async def run():
        t0 = time.monotonic()
        try:
            await browser_mod._bounded(asyncio.Event().wait(), timeout=0.2)
            raise AssertionError("_bounded did not time out")
        except asyncio.TimeoutError:
            pass
        assert time.monotonic() - t0 < 1.0

    asyncio.run(run())


def test_seat_busy_retries_licensed_launch_before_fallback():
    async def run():
        calls = []

        async def fake_launch_async(**kwargs):
            calls.append(kwargs.get("license_key", "licensed"))
            if len(calls) == 1:
                raise RuntimeError("CloakBrowser Pro: session limit reached for your plan.")
            return "browser"

        orig_launch, orig_delay = browser_mod.cb.launch_async, browser_mod._SEAT_RETRY_DELAY_SEC
        browser_mod.cb.launch_async, browser_mod._SEAT_RETRY_DELAY_SEC = fake_launch_async, 0.01
        try:
            pool, _ = _pool()
            assert await pool._launch_browser({}) == ("browser", True), "seat freed on retry: must stay licensed"
            assert calls == ["licensed", "licensed"]

            calls.clear()

            async def always_busy(**kwargs):
                calls.append(kwargs.get("license_key", "licensed"))
                if kwargs.get("license_key", "licensed") is None:
                    return "free-browser"
                raise RuntimeError("session limit reached")

            browser_mod.cb.launch_async = always_busy
            assert await pool._launch_browser({}) == ("free-browser", False)
            assert calls == ["licensed"] * (browser_mod._SEAT_RETRIES + 1) + [None]
        finally:
            browser_mod.cb.launch_async, browser_mod._SEAT_RETRY_DELAY_SEC = orig_launch, orig_delay

    asyncio.run(run())


def test_slow_humanized_click_is_not_cancelled_but_hung_click_is():
    async def run():
        orig = (browser_mod.BROWSER_CALL_TIMEOUT_SEC, browser_mod.BROWSER_CLICK_TIMEOUT_SEC)
        # Scaled down: page calls cap at 0.1s, clicks at 0.6s; a 0.3s click mirrors a 15s humanized click on the
        # server, which the old shared 10s cap cancelled mid-path
        browser_mod.BROWSER_CALL_TIMEOUT_SEC, browser_mod.BROWSER_CLICK_TIMEOUT_SEC = 0.1, 0.6
        try:
            pool, _ = _pool()

            async def slow_click(page):
                await asyncio.sleep(0.3)
                return True

            pool._click_turnstile_once = slow_click
            assert await pool._click_turnstile_checkbox(None) is True, "slow click was cancelled"

            async def hung_click(page):
                await asyncio.Event().wait()

            pool._click_turnstile_once = hung_click
            t0 = time.monotonic()
            assert await pool._click_turnstile_checkbox(None) is False
            assert time.monotonic() - t0 < 1.5, "hung click was not bounded"
        finally:
            browser_mod.BROWSER_CALL_TIMEOUT_SEC, browser_mod.BROWSER_CLICK_TIMEOUT_SEC = orig

    asyncio.run(run())


def proxy_id(proxy: str) -> str:
    from app.core.proxy import proxy_key

    return proxy_key(proxy)


def run_all():
    original = (browser_mod.BROWSER_JOB_OVERHEAD_SEC, browser_mod.BROWSER_QUEUE_TIMEOUT_SEC)
    try:
        test_hung_tab_hits_hard_deadline_and_frees_slot()
        test_poisoned_worker_is_replaced_on_next_request()
        test_outage_scenario_fails_fast_then_recovers()
        test_queue_wait_is_bounded_when_pool_is_saturated()
        test_tab_cap_spans_all_browsers()
        test_stalled_page_call_is_bounded()
        test_seat_busy_retries_licensed_launch_before_fallback()
        test_slow_humanized_click_is_not_cancelled_but_hung_click_is()
    finally:
        browser_mod.BROWSER_JOB_OVERHEAD_SEC, browser_mod.BROWSER_QUEUE_TIMEOUT_SEC = original
    print("[OK] Browser pool liveness checks passed.")


if __name__ == "__main__":
    run_all()
