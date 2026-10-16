"""License-seat placement, warm licensed worker, and exit-IP rotation handling in the browser pool."""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.core.browser as browser_mod
from app.core.browser import BrowserPool, _Worker
from app.core.cookie_store import store
from app.core.proxy import proxy_key

PROXY = "http://a.example:1"


class _FakeBrowser:
    def __init__(self):
        self.closed = False

    def is_connected(self):
        return not self.closed

    async def close(self):
        self.closed = True


def _pool(**kw) -> BrowserPool:
    opts = dict(max_tabs=5, headless=True, max_lifetime_sec=300, max_browsers=2, geoip=False)
    opts.update(kw)
    return BrowserPool(**opts)


def test_license_policy_routes_seat_to_proxy_identity():
    pool = _pool(licensed_identity="proxy")
    assert pool._may_license(PROXY) and not pool._may_license(None)
    pool = _pool(licensed_identity="direct")
    assert pool._may_license(None) and not pool._may_license(PROXY)
    pool = _pool(licensed_identity="bogus")
    assert pool._may_license(None) and pool._may_license(PROXY)


def test_direct_identity_launches_keyless_without_touching_the_seat():
    async def run():
        seen = []

        async def fake_launch_async(**kwargs):
            seen.append(kwargs.get("license_key", "licensed"))
            return _FakeBrowser()

        orig = browser_mod.cb.launch_async
        browser_mod.cb.launch_async = fake_launch_async
        try:
            pool = _pool(licensed_identity="proxy")
            direct = await pool._launch_worker(proxy_key(None), None)
            proxied = await pool._launch_worker(proxy_key(PROXY), PROXY)
            assert (direct.licensed, proxied.licensed) == (False, True)
            assert seen == [None, "licensed"], "direct must never attempt the licensed seat"
        finally:
            browser_mod.cb.launch_async = orig

    asyncio.run(run())


def test_licensed_worker_stays_warm_past_unlicensed_lifetime():
    pool = _pool(max_lifetime_sec=300, licensed_lifetime_sec=3600)
    aged = time.monotonic() - 600
    warm = _Worker(key="px", proxy=PROXY, browser=_FakeBrowser(), created_at=aged, last_used=aged, licensed=True)
    cold = _Worker(key="direct", proxy=None, browser=_FakeBrowser(), created_at=aged, last_used=aged, licensed=False)
    assert not pool._is_stale(warm), "licensed worker recycled after only 10 minutes"
    assert pool._is_stale(cold)
    warm.created_at = time.monotonic() - 4000
    assert pool._is_stale(warm), "licensed worker never recycled"


def test_eviction_keeps_the_licensed_worker():
    async def run():
        pool = _pool(max_browsers=2)
        launched = []

        async def fake_launch(key, proxy):
            launched.append(proxy)
            return _Worker(key=key, proxy=proxy, browser=_FakeBrowser(), created_at=time.monotonic(),
                           last_used=0.0, licensed=proxy == PROXY)

        pool._launch_worker = fake_launch
        far = time.monotonic() + 5
        w_proxy = await pool._acquire(PROXY, far)
        await pool._release(w_proxy)
        w_direct = await pool._acquire(None, far)
        await pool._release(w_direct)
        w_direct.last_used = time.monotonic()  # direct is the most recently used, still the one to go
        w_other = await pool._acquire("http://b.example:2", far)
        assert w_direct.browser.closed and not w_proxy.browser.closed, "evicted the licensed worker"
        await pool._release(w_other)

    asyncio.run(run())


async def _probe_now(pool: BrowserPool, worker: _Worker) -> None:
    """Force a due exit-IP probe and wait for it to finish."""
    worker.exit_ip_checked_at = 0.0
    pool._schedule_exit_ip_probe(worker)
    task = pool._probes.get(worker.key)
    if task:
        await task


def test_same_exit_ip_keeps_the_browser():
    async def run():
        pool = _pool()
        orig = browser_mod.resolve_proxy_exit_ip
        browser_mod.resolve_proxy_exit_ip = lambda proxy: "1.1.1.1"
        try:
            w = _Worker(key=proxy_key(PROXY), proxy=PROXY, browser=_FakeBrowser(), created_at=time.monotonic(),
                        last_used=0.0, exit_ip="1.1.1.1")
            pool._workers[w.key] = w
            for _ in range(3):
                await _probe_now(pool, w)
            assert not w.poisoned and not w.browser.closed
        finally:
            browser_mod.resolve_proxy_exit_ip = orig

    asyncio.run(run())


def test_probe_is_rate_limited_and_never_blocks_acquire():
    async def run():
        pool = _pool()
        calls = []
        orig = browser_mod.resolve_proxy_exit_ip

        def slow(proxy):
            calls.append(proxy)
            time.sleep(0.5)  # a slow proxy hop, run in a worker thread
            return "1.1.1.1"

        browser_mod.resolve_proxy_exit_ip = slow
        try:
            w = _Worker(key=proxy_key(PROXY), proxy=PROXY, browser=_FakeBrowser(), created_at=time.monotonic(),
                        last_used=0.0, exit_ip="1.1.1.1", exit_ip_checked_at=0.0)
            pool._workers[w.key] = w
            t0 = time.monotonic()
            got = await pool._acquire(PROXY, time.monotonic() + 5)
            assert got is w and time.monotonic() - t0 < 0.2, "acquire waited on the exit-IP probe"
            await pool._release(got)
            again = await pool._acquire(PROXY, time.monotonic() + 5)   # probe still in flight / not due
            await pool._release(again)
            await asyncio.gather(*pool._probes.values())
            assert len(calls) == 1, "probe not rate-limited"
        finally:
            browser_mod.resolve_proxy_exit_ip = orig

    asyncio.run(run())


def test_exit_ip_change_drains_retires_and_drops_old_clearance():
    async def run():
        pool = _pool(max_browsers=1)
        launched = []

        async def fake_launch(key, proxy):
            launched.append(proxy)
            return _Worker(key=key, proxy=proxy, browser=_FakeBrowser(), created_at=time.monotonic(),
                           last_used=0.0, exit_ip="1.1.1.1")

        pool._launch_worker = fake_launch
        ident = proxy_key(PROXY)
        store.set(store.cache_key("sub.target-site.example", None, ident), {"cf_clearance": "old-ip"})

        orig = browser_mod.resolve_proxy_exit_ip
        browser_mod.resolve_proxy_exit_ip = lambda proxy: "2.2.2.2"
        try:
            far = time.monotonic() + 5
            first = await pool._acquire(PROXY, far)          # tab A open on the old IP
            await _probe_now(pool, first)                     # proxy now exits from a new IP

            pending = asyncio.create_task(pool._acquire(PROXY, far))
            await asyncio.sleep(0.05)
            assert first.poisoned and not first.browser.closed, "must drain tab A before closing (grace)"
            assert not pending.done(), "new tab joined a retired process"
            assert store.get(store.cache_key("sub.target-site.example", None, ident)) is None, "old-IP clearance kept"

            await pool._release(first)                         # tab A finishes -> process replaced
            second = await asyncio.wait_for(pending, 1)
            assert first.browser.closed and second is not first and launched == [PROXY, PROXY]
            await pool._release(second)
        finally:
            browser_mod.resolve_proxy_exit_ip = orig

    asyncio.run(run())


def test_direct_workers_never_probe_exit_ip():
    async def run():
        pool = _pool()
        w = _Worker(key="direct", proxy=None, browser=_FakeBrowser(), created_at=time.monotonic(), last_used=0.0,
                    exit_ip_checked_at=0.0)
        pool._schedule_exit_ip_probe(w)
        assert not pool._probes, "direct worker scheduled an exit-IP probe"

    asyncio.run(run())


def run_all():
    test_license_policy_routes_seat_to_proxy_identity()
    test_direct_identity_launches_keyless_without_touching_the_seat()
    test_licensed_worker_stays_warm_past_unlicensed_lifetime()
    test_eviction_keeps_the_licensed_worker()
    test_same_exit_ip_keeps_the_browser()
    test_probe_is_rate_limited_and_never_blocks_acquire()
    test_exit_ip_change_drains_retires_and_drops_old_clearance()
    test_direct_workers_never_probe_exit_ip()
    print("[OK] Browser pool identity checks passed.")


if __name__ == "__main__":
    run_all()
