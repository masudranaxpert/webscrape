"""Proxy support: validation, masking, routing, per-identity cookie namespaces and the keyed browser pool."""

import asyncio
import pytest
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError

import app.core.proxy as proxy_mod
from app.core.browser import BrowserPool, BrowserPoolBusy, ProxySolvingDisabled, _Worker
from app.core.cookie_store import CookieStore
from app.core.proxy import (
    browser_proxy,
    is_proxy_error,
    mask_proxy,
    normalize_proxy,
    proxy_key,
    redact,
    resolve_proxy,
)
from app.schemas.scraping import FetchRequest

SOCKS = "socks5://user:p@ss=w0rd@1.2.3.4:1080"


# Validation
def test_normalize_accepts_supported_schemes():
    assert normalize_proxy("http://1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert normalize_proxy(" HTTPS://proxy.example:443 ") == "https://proxy.example:443"
    assert normalize_proxy("socks5h://u:p@h.example:1080") == "socks5h://u:p@h.example:1080"
    assert normalize_proxy("http://[2001:db8::1]:3128") == "http://[2001:db8::1]:3128"
    assert normalize_proxy(None) is None
    assert normalize_proxy("  ") is None


def test_normalize_reencodes_special_char_credentials():
    # Chromium's --proxy-server parser truncates raw passwords at '=' and similar characters
    assert normalize_proxy(SOCKS) == "socks5://user:p%40ss%3Dw0rd@1.2.3.4:1080"
    # Already-encoded input is not double-encoded
    assert normalize_proxy("socks5://user:p%40ss%3Dw0rd@1.2.3.4:1080") == "socks5://user:p%40ss%3Dw0rd@1.2.3.4:1080"


def test_normalize_rejects_unusable_proxies():
    bad_proxies = [
        "ftp://1.2.3.4:21",
        "1.2.3.4:8080",
        "http://1.2.3.4",
        "http://1.2.3.4:99999",
        "http://1.2.3.4:8080/path",
        "http://user@1.2.3.4:8080",
        "masque://1.2.3.4:443",
    ]
    for bad in bad_proxies:
        try:
            normalize_proxy(bad)
            assert False, f"Expected ValueError for {bad}"
        except ValueError:
            pass


def test_schema_rejects_invalid_proxy_with_422_semantics():
    try:
        FetchRequest(url="https://example.com", proxy="1.2.3.4:8080")
        assert False, "Expected ValidationError"
    except ValidationError:
        pass
    assert FetchRequest(url="https://example.com", proxy="DIRECT").proxy == "direct"
    assert FetchRequest(url="https://example.com", proxy="none").proxy is None
    assert FetchRequest(url="https://example.com", proxy=SOCKS).proxy == normalize_proxy(SOCKS)


# Credential masking and logs safety
def test_mask_hides_credentials():
    assert mask_proxy(normalize_proxy(SOCKS)) == "socks5://***@1.2.3.4:1080"
    assert mask_proxy("http://1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert mask_proxy(None) == "direct"


def test_redact_strips_credentials_from_error_text():
    msg = "Browser launch failed: chrome --proxy-server=socks5://user:p%40ss@1.2.3.4:1080 --headless"
    out = redact(msg)
    assert "p%40ss" not in out and "user" not in out
    assert "socks5://***@1.2.3.4:1080" in out


def test_proxy_key_is_stable_and_non_reversible():
    p = normalize_proxy(SOCKS)
    assert proxy_key(p) == proxy_key(p)
    assert proxy_key(p) != proxy_key("http://1.2.3.4:8080")
    assert proxy_key(None) == "direct"
    assert "user" not in proxy_key(p) and "1.2.3.4" not in proxy_key(p)


def test_browser_proxy_maps_socks5h():
    assert browser_proxy("socks5h://u:p@h:1") == "socks5://u:p@h:1"
    assert browser_proxy("http://h:1") == "http://h:1"


def test_proxy_error_detection():
    assert is_proxy_error("net::ERR_PROXY_CONNECTION_FAILED at https://x")
    assert is_proxy_error("net::ERR_TUNNEL_CONNECTION_FAILED")
    assert is_proxy_error("socks connect failed")
    assert not is_proxy_error("net::ERR_NAME_NOT_RESOLVED")
    assert not is_proxy_error(None)


# WARP_PROXY default egress: omission routes through it, 'direct'/'off' bypass, explicit wins
def test_resolve_proxy_routing():
    orig = proxy_mod._DEFAULT_EGRESS
    try:
        assert resolve_proxy(None, "sub.target-site.example") is None
        assert resolve_proxy("direct", "sub.target-site.example") is None
        assert resolve_proxy("off", "sub.target-site.example") is None
        assert resolve_proxy("http://u:p@res.example:8000", "example.com") == "http://u:p@res.example:8000"
        assert resolve_proxy("http://other.example:1", "sub.target-site.example") == "http://other.example:1"

        proxy_mod._DEFAULT_EGRESS = "http://warp:8080"
        assert resolve_proxy(None, "example.com") == "http://warp:8080"
        assert resolve_proxy("direct", "example.com") is None
        assert resolve_proxy("http://other.example:1", "example.com") == "http://other.example:1"
    finally:
        proxy_mod._DEFAULT_EGRESS = orig


# Cookie namespaces per egress identity
def test_cookie_cache_never_crosses_identities():
    s = CookieStore()
    pa, pb = proxy_key("http://a.example:1"), proxy_key("http://b.example:1")
    direct_key = s.cache_key("sub.target-site.example")
    a_key = s.cache_key("sub.target-site.example", identity=pa)
    b_key = s.cache_key("sub.target-site.example", identity=pb)
    assert len({direct_key, a_key, b_key}) == 3
    assert s.cache_key("sub.target-site.example", identity="direct") == direct_key

    s.set(a_key, {"cf_clearance": "A", "ci_session": "sA"})
    s.set(b_key, {"cf_clearance": "B"})
    assert s.get(a_key)["cf_clearance"] == "A"
    assert s.get(b_key)["cf_clearance"] == "B"
    assert s.get(direct_key) is None
    # Subdomain cookies are strictly host-scoped and do not pollute root domain or other subdomains
    assert s.get(s.cache_key("target-site.example", identity=pa)) is None
    assert s.get(s.cache_key("other.target-site.example", identity=pa)) is None
    assert s.get(s.cache_key("other.target-site.example")) is None


def test_session_keys_with_identity():
    s = CookieStore()
    pa = proxy_key("http://a.example:1")
    key = s.cache_key("sub.target-site.example", "flow1", pa)
    assert key == f"{pa}@sub.target-site.example#flow1"
    s.set(key, {"cf_clearance": "S"})
    assert s.get(key) == {"cf_clearance": "S"}
    assert s.get(s.cache_key("sub.target-site.example", "flow1")) is None
    # Invalidation targets specific entry
    s.set(s.cache_key("x.example.com", identity=pa), {"cf_clearance": "R"})
    s.invalidate(s.cache_key("x.example.com", identity=pa), rejected_clearance="R")
    assert s.get(s.cache_key("x.example.com", identity=pa)) is None


# Keyed browser pool
class _FakeBrowser:
    def __init__(self):
        self.closed = False

    def is_connected(self):
        return not self.closed

    async def close(self):
        self.closed = True


def _far() -> float:
    """Queue deadline far enough away that these tests never hit it."""
    return time.monotonic() + 60


def _fake_pool(max_browsers: int) -> tuple[BrowserPool, list[str | None]]:
    pool = BrowserPool(max_tabs=10, headless=True, max_lifetime_sec=3600, max_browsers=max_browsers, geoip=False)
    launched: list[str | None] = []

    async def fake_launch(key, proxy):
        launched.append(proxy)
        return _Worker(key=key, proxy=proxy, browser=_FakeBrowser(), created_at=0.0, last_used=0.0,
                       licensed=proxy is None)

    pool._launch_worker = fake_launch
    return pool, launched


def test_pool_reuses_worker_per_identity():
    async def run():
        pool, launched = _fake_pool(max_browsers=2)
        w1 = await pool._acquire(None, _far())
        w2 = await pool._acquire(None, _far())
        w3 = await pool._acquire("http://a.example:1", _far())
        assert w1 is w2 and w1 is not w3
        assert launched == [None, "http://a.example:1"]
        # One licensed seat (direct) + one keyless proxy worker
        stats = pool.stats()
        assert stats["browsers"] == 2 and stats["licensed_browsers"] == 1
        assert stats["active_tabs"] == 3 and stats["queued"] == 0 and stats["proxy_queue_bytes"] == 0

    asyncio.run(run())


def test_proxied_browsers_never_evict_the_direct_browser():
    async def run():
        pool, launched = _fake_pool(max_browsers=1)
        w_direct = await pool._acquire(None, _far())
        await pool._release(w_direct)
        # A proxied request opens its own browser; the direct licensed one stays untouched
        w_proxy = await pool._acquire("http://a.example:1", _far())
        assert not w_direct.browser.closed and not w_proxy.browser.closed
        assert launched == [None, "http://a.example:1"]

        # A second proxy with all proxy slots busy recycles the idle proxied worker only
        await pool._release(w_proxy)
        w_other = await pool._acquire("http://b.example:2", _far())
        await asyncio.gather(*pool._closing)  # retired processes close in the background, off the pool lock
        assert w_proxy.browser.closed and not w_direct.browser.closed
        assert launched == [None, "http://a.example:1", "http://b.example:2"]
        await pool._release(w_other)

    asyncio.run(run())


def test_proxied_solving_is_independent_of_the_direct_browser():
    async def run():
        pool, launched = _fake_pool(max_browsers=1)
        w_direct = await pool._acquire(None, _far())

        # A proxied request must not wait for the busy direct browser: separate resources
        proxied = asyncio.create_task(pool._acquire("http://a.example:1", _far()))
        await asyncio.sleep(0.05)
        assert proxied.done(), "proxied traffic must not queue behind the direct browser"
        w_proxy = proxied.result()
        assert w_proxy.proxy == "http://a.example:1" and not w_direct.browser.closed

        # The second proxy queues for the single proxied slot
        queued = asyncio.create_task(pool._acquire("http://b.example:2", _far()))
        await asyncio.sleep(0.05)
        assert not queued.done()

        await pool._release(w_proxy)
        w_b = await asyncio.wait_for(queued, 1)
        await asyncio.gather(*pool._closing)
        assert w_b.proxy == "http://b.example:2" and w_proxy.browser.closed
        await pool._release(w_b)
        assert launched == [None, "http://a.example:1", "http://b.example:2"]

    asyncio.run(run())


def test_proxy_queue_caps_buffered_request_bodies():
    async def run():
        pool, launched = _fake_pool(max_browsers=1)
        w = await pool._acquire("http://a.example:1", _far())

        # An oversized queued body is rejected outright, nothing is buffered
        from app.core.config import PROXY_QUEUE_BYTE_LIMIT
        over = asyncio.create_task(pool._acquire("http://b.example:2", _far(), payload_bytes=PROXY_QUEUE_BYTE_LIMIT + 1))
        await asyncio.sleep(0.05)
        assert over.done()
        try:
            await over
            raise AssertionError("queue cap must reject oversized queued bodies")
        except BrowserPoolBusy as exc:
            assert "Proxy queue full" in str(exc)
        assert pool._proxy_queue_bytes == 0

        # A small body queues (charged against the cap) and clears once it gets its slot
        ok = asyncio.create_task(pool._acquire("http://c.example:3", _far(), payload_bytes=1000))
        await asyncio.sleep(0.05)
        assert not ok.done() and pool._proxy_queue_bytes == 1000
        await pool._release(w)
        got = await asyncio.wait_for(ok, 1)
        assert got.proxy == "http://c.example:3" and pool._proxy_queue_bytes == 0
        await pool._release(got)

    asyncio.run(run())


def test_pool_replaces_disconnected_worker():
    async def run():
        pool, launched = _fake_pool(max_browsers=1)
        w = await pool._acquire(None, _far())
        await pool._release(w)
        w.browser.closed = True
        w2 = await pool._acquire(None, _far())
        assert w2 is not w and launched == [None, None]

    asyncio.run(run())


def run_all():
    test_normalize_accepts_supported_schemes()
    test_normalize_reencodes_special_char_credentials()
    test_normalize_rejects_unusable_proxies()
    test_schema_rejects_invalid_proxy_with_422_semantics()
    test_mask_hides_credentials()
    test_redact_strips_credentials_from_error_text()
    test_proxy_key_is_stable_and_non_reversible()
    test_browser_proxy_maps_socks5h()
    test_proxy_error_detection()
    test_resolve_proxy_routing()
    test_cookie_cache_never_crosses_identities()
    test_session_keys_with_identity()
    test_pool_reuses_worker_per_identity()
    test_proxied_browsers_never_evict_the_direct_browser()
    test_proxied_solving_is_independent_of_the_direct_browser()
    test_proxy_queue_caps_buffered_request_bodies()
    test_pool_replaces_disconnected_worker()
    print("[OK] All proxy & browser pool self-checks passed successfully.")


if __name__ == "__main__":
    run_all()


def test_slow_proxy_launch_never_blocks_other_identities():
    """A dead proxy's launch (GeoIP lookups time out through it) must not freeze the direct browser or releases."""
    async def run():
        pool, launched = _fake_pool(max_browsers=1)
        w_direct = await pool._acquire(None, _far())
        await pool._release(w_direct)
        gate = asyncio.Event()
        fast_launch = pool._launch_worker

        async def slow_launch(key, proxy):
            if proxy:
                await gate.wait()
            return await fast_launch(key, proxy)

        pool._launch_worker = slow_launch
        proxied = asyncio.create_task(pool._acquire("http://dead.example:1", _far()))
        await asyncio.sleep(0.05)
        assert not proxied.done()

        # While the proxy launch hangs, direct traffic acquires and releases instantly
        w = await asyncio.wait_for(pool._acquire(None, _far()), 0.5)
        await asyncio.wait_for(pool._release(w), 0.5)
        # A second request for the launching identity waits for that launch instead of starting another
        second = asyncio.create_task(pool._acquire("http://dead.example:1", _far()))
        await asyncio.sleep(0.05)
        gate.set()
        a, b = await asyncio.wait_for(asyncio.gather(proxied, second), 1)
        assert a is b and launched.count("http://dead.example:1") == 1
        assert pool.stats()["active_tabs"] == 2 and pool._launching == {}

    asyncio.run(run())


def test_failed_launch_frees_its_reservation():
    async def run():
        pool, launched = _fake_pool(max_browsers=1)

        async def boom(key, proxy):
            raise RuntimeError("Proxy unreachable")

        pool._launch_worker = boom
        with pytest.raises(RuntimeError):
            await pool._acquire("http://dead.example:1", _far())
        assert pool._launching == {} and pool.stats()["active_tabs"] == 0

    asyncio.run(run())
