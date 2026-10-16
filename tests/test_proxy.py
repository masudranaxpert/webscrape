"""Proxy support: validation, masking, routing, per-identity cookie namespaces and the keyed browser pool."""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError

import app.core.proxy as proxy_mod
from app.core.browser import BrowserPool, _Worker
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


# Default proxy routing
def test_resolve_proxy_routing():
    orig_default = proxy_mod._DEFAULT_PROXY
    orig_domains = proxy_mod._DEFAULT_PROXY_DOMAINS
    try:
        default = "http://u:p@res.example:8000"
        proxy_mod._DEFAULT_PROXY = default
        proxy_mod._DEFAULT_PROXY_DOMAINS = ("target-site.site",)

        assert resolve_proxy(None, "sub.target-site.example") == default
        assert resolve_proxy(None, "target-site.site") == default
        assert resolve_proxy(None, "nottarget-site.site") is None
        assert resolve_proxy(None, "example.com") is None
        assert resolve_proxy("direct", "sub.target-site.example") is None
        assert resolve_proxy("http://other.example:1", "sub.target-site.example") == "http://other.example:1"

        proxy_mod._DEFAULT_PROXY_DOMAINS = ()
        assert resolve_proxy(None, "example.com") == default
    finally:
        proxy_mod._DEFAULT_PROXY = orig_default
        proxy_mod._DEFAULT_PROXY_DOMAINS = orig_domains


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
    assert s.get(s.cache_key("target-site.site", identity=pa)) is None
    assert s.get(s.cache_key("other.target-site.site", identity=pa)) is None
    assert s.get(s.cache_key("other.target-site.site")) is None


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
        return _Worker(key=key, proxy=proxy, browser=_FakeBrowser(), created_at=0.0, last_used=0.0)

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
        assert pool.stats() == {"browsers": 2, "max_browsers": 2, "active_tabs": 3, "max_tabs": 10, "queued": 0, "licensed_browsers": 2}

    asyncio.run(run())


def test_pool_evicts_idle_lru_when_full():
    async def run():
        pool, launched = _fake_pool(max_browsers=1)
        w_direct = await pool._acquire(None, _far())
        await pool._release(w_direct)
        w_proxy = await pool._acquire("http://a.example:1", _far())
        assert w_direct.browser.closed and not w_proxy.browser.closed
        assert launched == [None, "http://a.example:1"]

    asyncio.run(run())


def test_pool_waits_for_busy_identity_and_does_not_starve():
    async def run():
        pool, launched = _fake_pool(max_browsers=1)
        w_direct = await pool._acquire(None, _far())

        proxied = asyncio.create_task(pool._acquire("http://a.example:1", _far()))
        await asyncio.sleep(0.05)
        assert not proxied.done()

        # A new direct request must not join the busy process while the proxy waits for a slot
        late_direct = asyncio.create_task(pool._acquire(None, _far()))
        await asyncio.sleep(0.05)
        assert not late_direct.done()

        await pool._release(w_direct)
        w_proxy = await asyncio.wait_for(proxied, 1)
        assert w_proxy.proxy == "http://a.example:1" and w_direct.browser.closed

        await pool._release(w_proxy)
        w_direct2 = await asyncio.wait_for(late_direct, 1)
        assert w_direct2.proxy is None
        assert launched == [None, "http://a.example:1", None]

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
    test_pool_evicts_idle_lru_when_full()
    test_pool_waits_for_busy_identity_and_does_not_starve()
    test_pool_replaces_disconnected_worker()
    print("[OK] All proxy & browser pool self-checks passed successfully.")


if __name__ == "__main__":
    run_all()
