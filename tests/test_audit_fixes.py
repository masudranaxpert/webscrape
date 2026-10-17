"""Regression tests for the audit fixes: SSRF guard, UA-matched presets, selector validation, solver lock cleanup."""

import asyncio
import time

import pytest
from pydantic import ValidationError

from app.api.v1.endpoints import fetch as fetch_mod
from app.core import turnstile
from app.core.proxy import forbidden_target, is_proxy_error
from app.schemas.scraping import FetchRequest


@pytest.mark.parametrize("host", [
    "127.0.0.1", "localhost", "10.1.2.3", "192.168.0.10", "169.254.169.254", "::1", "::ffff:127.0.0.1",
    "warp", "metadata.google.internal", "printer.local", "0.0.0.0",
])
def test_internal_targets_are_blocked(host):
    assert forbidden_target(host)


@pytest.mark.parametrize("host", ["example.com", "1.1.1.1", "2606:4700:4700::1111", "sub.example.co.uk"])
def test_public_targets_are_allowed(host):
    assert forbidden_target(host) is None


def test_fetch_request_rejects_internal_and_file_urls():
    for url in ("file:///etc/passwd", "http://127.0.0.1:8000/health", "http://warp:8080/", "http://[::1]/"):
        with pytest.raises(ValidationError):
            FetchRequest(url=url)
    assert FetchRequest(url="https://example.com").url == "https://example.com"


def test_invalid_css_selector_is_422():
    with pytest.raises(ValidationError):
        FetchRequest(url="https://example.com", selector="h1[[[")
    assert FetchRequest(url="https://example.com", selector="h1.title").selector == "h1.title"


def test_body_is_never_rewritten():
    assert FetchRequest(url="https://example.com", method="POST", body="null").body == "null"
    assert FetchRequest(url="https://example.com", method="POST", body='{"a":1}\n').body == '{"a":1}\n'


def test_proxy_error_markers_do_not_match_urls():
    assert not is_proxy_error("net::ERR_TIMED_OUT at https://site.com/item/1407")
    assert not is_proxy_error("dial tcp 1.2.3.4:4407: i/o timeout")
    assert not is_proxy_error("https://socks.example.com/ ERR_NAME_NOT_RESOLVED")
    assert is_proxy_error("net::ERR_PROXY_CONNECTION_FAILED")
    assert is_proxy_error("net::ERR_TUNNEL_CONNECTION_FAILED")


def test_preset_matches_browser_chrome_major():
    ua_146_win = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ua_146_linux = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.7680.177 Safari/537.36"
    assert fetch_mod._preset_for_ua(ua_146_win) == "chrome-146-windows"
    assert fetch_mod._preset_for_ua(ua_146_linux) == "chrome-146-linux"
    assert fetch_mod._preset_for_ua("Mozilla/5.0 Firefox/148.0") == "firefox-latest"
    assert fetch_mod._preset_for_ua(None) is None
    # A browser newer than every preset falls back to the newest preset not newer than it
    assert fetch_mod._preset_for_ua(ua_146_win.replace("146", "999")).startswith("chrome-")


class _FakePage:
    """Widget stand-in: every reset/load issues the next token after a couple of polls."""

    def __init__(self, tokens, error=None):
        self._tokens, self._error, self._polls = tokens, error, 0

    async def route(self, *_a, **_k):
        pass

    async def goto(self, *_a, **_k):
        self._polls = 0

    async def evaluate(self, js, *_a):
        if js == turnstile._RESET_JS:
            self._polls = 0
            return True
        self._polls += 1
        if self._error:
            return ["", self._error]
        if self._polls >= 2 and self._tokens:
            return [self._tokens.pop(0), ""]
        return ["", ""]


class _FakeContext:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page

    async def close(self):
        pass


class _FakeWorker:
    def __init__(self, page):
        self.poisoned = False
        self.contexts = 0
        outer = self

        class _Browser:
            async def new_context(self_inner):
                outer.contexts += 1
                return _FakeContext(page)

        self.browser = _Browser()


def _fake_slot(worker):
    import contextlib

    @contextlib.asynccontextmanager
    async def slot(*_a, **_k):
        yield worker

    return slot


async def _settle():
    # Real sleep: the fake_widget fixture makes asyncio.sleep instant, which would end this wait immediately
    for _ in range(50):
        if not any(m.tasks for m in turnstile._minters.values()):
            return
        await _real_sleep(0.05)


@pytest.fixture
def fake_widget(monkeypatch):
    async def no_click(_page):
        return False

    monkeypatch.setattr(turnstile.challenge, "click_turnstile_once", no_click)
    monkeypatch.setattr(turnstile.asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(turnstile, "_WARM_KEEP_SEC", 0.05)
    turnstile._minters.clear()

    def install(tokens, error=None):
        worker = _FakeWorker(_FakePage(tokens, error))
        monkeypatch.setattr(turnstile.pool, "slot", _fake_slot(worker))
        return worker

    yield install
    turnstile._minters.clear()


_real_sleep = asyncio.sleep


async def _fast_sleep(_delay, *a, **k):
    await _real_sleep(0)


async def test_burst_on_one_sitekey_gets_distinct_tokens_from_one_warm_page(fake_widget, monkeypatch):
    monkeypatch.setattr(turnstile, "TURNSTILE_PREFETCH", 3)
    worker = fake_widget([f"tok-{i:02d}-xxxxxxxxxx" for i in range(20)])
    results = await asyncio.gather(*(turnstile.solve("0xkey", "https://example.com/", timeout=5) for _ in range(8)))
    tokens = [r["token"] for r in results]
    assert len(set(tokens)) == 8, "a token must never be handed to two requests"
    assert worker.contexts == 1, "the burst is served from one warm widget page (turnstile.reset), not 8 contexts"

    # With pre-minting on, the burst left spares: the next request is served instantly from the pool, still a unique token
    await _settle()
    nxt = await turnstile.solve("0xkey", "https://example.com/", timeout=5)
    assert nxt["pooled"] and nxt["token"] not in tokens


async def test_fatal_widget_error_fails_the_queue_and_cleans_up(fake_widget):
    fake_widget([], error="110200")
    with pytest.raises(RuntimeError, match="domain not authorized"):
        await turnstile.solve("0xbad", "https://example.com/", timeout=5)
    await _settle()
    assert turnstile._minters == {}


async def test_full_queue_is_rejected_immediately(fake_widget, monkeypatch):
    from app.core.browser import BrowserPoolBusy

    fake_widget([])  # never issues a token
    monkeypatch.setattr(turnstile, "TURNSTILE_MAX_QUEUE", 2)
    waiting = [asyncio.create_task(turnstile.solve("0xq", "https://example.com/", timeout=5)) for _ in range(2)]
    await _real_sleep(0.05)
    with pytest.raises(BrowserPoolBusy):
        await turnstile.solve("0xq", "https://example.com/", timeout=5)
    for t in waiting:
        t.cancel()
    await asyncio.gather(*waiting, return_exceptions=True)


async def test_expired_pooled_tokens_are_never_handed_out(fake_widget, monkeypatch):
    fake_widget(["old-token-xxxxxxxx", "new-token-xxxxxxxx"])
    m = turnstile._minters["0xe|https://example.com/|direct"] = turnstile._Minter("0xe", "https://example.com/", None)
    m.tokens.append(("stale-token-xxxxxx", time.monotonic() - 400))
    r = await turnstile.solve("0xe", "https://example.com/", timeout=5)
    assert r["token"] != "stale-token-xxxxxx" and not r["pooled"]


async def test_hybrid_egress_http_direct_until_warp_browser_earns_clearance(monkeypatch):
    """No proxy in the request: HTTP goes direct, the browser escalates through WARP, and once WARP earned
    clearance for a host, later HTTP requests for that host follow it (clearance is bound to the WARP IP)."""
    import app.core.proxy as proxy_mod
    from app.core.browser import BrowserResult
    from app.core.cookie_store import store
    from app.core.fetcher import FetcherResult

    warp = "http://warp:8080"
    monkeypatch.setattr(proxy_mod, "_DEFAULT_EGRESS", warp)
    http_calls, browser_calls = [], []

    async def fake_http(url, **kw):
        http_calls.append((url, kw["proxy"], dict(kw["cookies"])))
        walled = "cf_clearance" not in kw["cookies"] and "walled" in url
        return FetcherResult(status_code=403 if walled else 200, headers={}, body="ok", cookies={},
                             final_url=url, cf_wall=walled)

    async def fake_browser(url, **kw):
        browser_calls.append((url, kw["proxy"]))
        return BrowserResult(status_code=200, body="solved", cookies={"cf_clearance": "x" * 20}, final_url=url,
                             user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/146.0.0.0 Safari/537.36")

    monkeypatch.setattr(fetch_mod, "httpcloak_fetch", fake_http)
    monkeypatch.setattr(fetch_mod.pool, "solve_and_fetch", fake_browser)
    for h in ("walled.example.com", "plain.example.com"):
        for ident in ("direct", proxy_mod.proxy_key(warp)):
            store.invalidate(store.cache_key(h, None, ident))

    r1 = await fetch_mod.fetch(FetchRequest(url="https://walled.example.com/a"))
    assert r1.meta.via == "browser"
    assert http_calls[-1][1] is None, "first HTTP attempt must go direct"
    assert browser_calls[-1][1] == warp, "browser escalation uses the WARP default egress"

    r2 = await fetch_mod.fetch(FetchRequest(url="https://walled.example.com/b"))
    assert r2.meta.via == "http" and r2.meta.preset == "chrome-146-windows"
    assert http_calls[-1][1] == warp and "cf_clearance" in http_calls[-1][2], "HTTP sticks to the IP that earned clearance"

    await fetch_mod.fetch(FetchRequest(url="https://plain.example.com/"))
    assert http_calls[-1][1] is None, "hosts WARP never solved stay direct"

    n = len(browser_calls)
    await fetch_mod.fetch(FetchRequest(url="https://walled.example.com/c", proxy="direct", force_browser=True))
    assert browser_calls[-1][1] is None and len(browser_calls) == n + 1, "proxy='direct' keeps the browser direct too"


async def test_cookie_cache_survives_restart_and_respects_size_cap(tmp_path, monkeypatch):
    import os
    from app.core import cache_persist
    from app.core.cookie_store import CookieStore

    src = CookieStore(max_domains=100, max_sessions=10)
    for i in range(50):
        src.set(f"px-abc@site{i}.example", {"cf_clearance": f"clr{i}-" + "x" * 200}, ua="UA", preset="chrome-146-windows")
    src.set("expired.example", {"cf_clearance": "old" * 10}, ttl=-1)

    # Size cap keeps the most recently used entries and drops the oldest
    small = src.snapshot(max_bytes=3000)
    dst = CookieStore(max_domains=100, max_sessions=10)
    n = dst.restore(small)
    assert 0 < n < 50 and len(small) <= 3000
    assert dst.get("px-abc@site49.example") and dst.get("px-abc@site0.example") is None
    assert dst.get("expired.example") is None
    assert dst.get_identity("px-abc@site49.example")["preset"] == "chrome-146-windows"

    # End to end through the persistence module: atomic file, mode 0600, reloaded into the global store
    path = tmp_path / "data" / "cookie_cache.json"
    monkeypatch.setattr(cache_persist, "COOKIE_CACHE_FILE", str(path))
    monkeypatch.setattr(cache_persist, "store", src)
    src.dirty = True
    await cache_persist.save()
    assert path.exists() and oct(os.stat(path).st_mode & 0o777) == "0o600" and not src.dirty
    fresh = CookieStore(max_domains=100, max_sessions=10)
    monkeypatch.setattr(cache_persist, "store", fresh)
    cache_persist.load()
    assert fresh.get("px-abc@site10.example") == {"cf_clearance": "clr10-" + "x" * 200}

    # A corrupt file never breaks startup
    path.write_bytes(b"{not json")
    cache_persist.load()


async def test_default_mints_strictly_on_demand(fake_widget):
    """TURNSTILE_PREFETCH defaults to 0: a burst mints exactly one token per request, nothing is left over."""
    assert turnstile.TURNSTILE_PREFETCH == 0
    fake_widget([f"tok-{i:02d}-xxxxxxxxxx" for i in range(20)])
    await asyncio.gather(*(turnstile.solve("0xdemand", "https://example.com/", timeout=5) for _ in range(4)))
    await _settle()
    assert turnstile._minters == {}, "no spare tokens and no minter left running"


async def test_replayed_browser_ua_forces_matching_preset_even_if_request_sends_one(monkeypatch):
    """The playground always sends preset=chrome-latest-windows; replaying a cached Chrome/146 UA must still use
    chrome-146-*, and a caller that brings its own UA keeps its own preset."""
    from app.core.cookie_store import store
    from app.core.fetcher import FetcherResult

    calls = []

    async def fake_http(url, **kw):
        calls.append((kw["preset"], kw["headers"].get("user-agent")))
        return FetcherResult(status_code=200, headers={}, body="ok", cookies={}, final_url=url)

    monkeypatch.setattr(fetch_mod, "httpcloak_fetch", fake_http)
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    key = store.cache_key("preset.example.com", None, "direct")
    store.set(key, {"cf_clearance": "c" * 20}, ua=ua, preset="chrome-latest-windows")

    r = await fetch_mod.fetch(FetchRequest(url="https://preset.example.com/", preset="chrome-latest-windows", proxy="direct"))
    assert calls[-1] == ("chrome-146-windows", ua) and r.meta.preset == "chrome-146-windows"
    assert store.get_identity(key)["preset"] in (None, "chrome-latest-windows", "chrome-146-windows")

    await fetch_mod.fetch(FetchRequest(url="https://preset.example.com/", preset="firefox-latest",
                                       user_agent="Mozilla/5.0 Firefox/148.0", proxy="direct"))
    assert calls[-1][0] == "firefox-latest"
    store.invalidate(key)


async def test_settle_navigation_waits_for_redirects_to_finish():
    from app.core import fetch_flow

    last_nav = [time.monotonic()]

    class _Page:
        async def wait_for_load_state(self, *_a, **_k):
            pass

    async def redirect_later():
        await asyncio.sleep(0.3)
        last_nav[0] = time.monotonic()  # a meta/JS redirect fires after the solve

    t0 = time.monotonic()
    task = asyncio.create_task(redirect_later())
    await fetch_flow._settle_navigation(_Page(), last_nav, "load")
    await task
    waited = time.monotonic() - t0
    assert waited >= 0.3 + fetch_flow._NAV_QUIET_SEC - 0.05, "must not read the page while it is still navigating"

    # A page that stopped navigating long ago is read at once
    last_nav[0] = time.monotonic() - 5
    t0 = time.monotonic()
    await fetch_flow._settle_navigation(_Page(), last_nav, "load")
    assert time.monotonic() - t0 < 0.1


def test_single_license_seat_goes_to_the_default_browser_only(monkeypatch):
    import app.core.config as config_mod
    from app.core.browser import BrowserPool

    pool = BrowserPool(max_tabs=5, headless=True, max_browsers=1, geoip=False)
    monkeypatch.setattr(config_mod, "WARP_PROXY", "")
    assert pool._may_license(None) and not pool._may_license("http://other.example:1")

    monkeypatch.setattr(config_mod, "WARP_PROXY", "http://warp:8080")
    assert pool._may_license("http://warp:8080"), "WARP is the default browser: it holds the seat"
    assert not pool._may_license(None), "direct must not fight WARP for the one seat (~9s of session-limit retries)"
    assert not pool._may_license("http://other.example:1")


def test_binary_prune_keeps_current_keyless_pro_and_running_builds(tmp_path, monkeypatch):
    from cloakbrowser import download, license as cb_license
    from cloakbrowser.config import get_binary_dir, get_platform_tag
    from app.core import binaries

    monkeypatch.setenv("CLOAKBROWSER_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", "cb_test_key")
    current = get_binary_dir()
    for d in (current, tmp_path / "chromium-140.0.0.1", tmp_path / "chromium-150.0.0.1-pro",
              tmp_path / "chromium-154.0.0.1-pro", tmp_path / "chromium-120.0.0.1"):
        d.mkdir(parents=True)
    pro_bin = tmp_path / "chromium-154.0.0.1-pro" / "chrome"
    pro_bin.write_text("#!/bin/sh\n")
    pro_bin.chmod(0o755)
    (tmp_path / f"latest_pro_version_{get_platform_tag()}").write_text("154.0.0.1")

    seen_env = {}

    def fake_ensure(license_key=None, **_):
        seen_env["key_visible"] = "CLOAKBROWSER_LICENSE_KEY" in __import__("os").environ
        return str(current / "chrome")

    monkeypatch.setattr(download, "ensure_binary", fake_ensure)
    monkeypatch.setattr(download, "check_for_pro_update", lambda key, *a, **k: None)
    monkeypatch.setattr(cb_license, "validate_license", lambda key: type("I", (), {"valid": True, "plan": "free"})())
    # A browser still running an old build must never lose its files
    monkeypatch.setattr(binaries, "_in_use", lambda: {tmp_path / "chromium-120.0.0.1"})

    report = binaries._prepare_sync()
    left = sorted(p.name for p in tmp_path.glob("chromium-*"))
    assert left == sorted([current.name, "chromium-120.0.0.1", "chromium-154.0.0.1-pro"]), left
    assert sorted(report["pruned"]) == ["chromium-140.0.0.1", "chromium-150.0.0.1-pro"]
    assert report["pro"] == "154.0.0.1"
    assert seen_env["key_visible"] is False, "keyless ensure must not see the key (it would take the Pro path)"
    assert __import__("os").environ["CLOAKBROWSER_LICENSE_KEY"] == "cb_test_key", "key restored afterwards"


async def test_sequential_requests_reuse_the_warm_widget_page(fake_widget, monkeypatch):
    """A request arriving while the minter idles on its warm page is served by a reset, not a new context."""
    monkeypatch.setattr(turnstile, "_WARM_KEEP_SEC", 2.0)
    worker = fake_widget([f"tok-{i:02d}-xxxxxxxxxx" for i in range(10)])
    first = await turnstile.solve("0xwarm", "https://example.com/", timeout=5)
    await _real_sleep(0.2)  # the caller is gone; the minter keeps the page
    second = await turnstile.solve("0xwarm", "https://example.com/", timeout=5)
    assert first["token"] != second["token"]
    assert worker.contexts == 1, "second request must reuse the warm page"
    for _ in range(80):  # after _WARM_KEEP_SEC with no demand the page is closed and the minter forgotten
        if not turnstile._minters:
            break
        await _real_sleep(0.05)
    assert turnstile._minters == {}
