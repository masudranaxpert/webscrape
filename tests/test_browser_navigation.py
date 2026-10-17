"""Real-browser checks of generic click / redirect / download handling against a local site (no real sites).

Needs a CloakBrowser binary and a display, so it only runs with RUN_BROWSER_TESTS=1:
    RUN_BROWSER_TESTS=1 CLOAKBROWSER_LICENSE_KEY= xvfb-run -a python -m pytest tests/test_browser_navigation.py
(an empty license key keeps it off your licensed seat).
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

pytestmark = pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="set RUN_BROWSER_TESTS=1 (needs a browser)")


@pytest.fixture(scope="module")
def site():
    import browser_site

    srv, base = browser_site.start()
    yield srv, base
    srv.shutdown()


def _run(site, path, click=None):
    from app.core.browser import BrowserPool

    srv, base = site

    async def go():
        pool = BrowserPool(max_tabs=2, headless=False, max_lifetime_sec=300, max_browsers=1, geoip=False)
        await pool.start()
        try:
            srv.video_mb_sent = 0
            res = await pool.solve_and_fetch(base + path, cf_wait=8, click_selector=click)
            await asyncio.sleep(0.5)
            return res
        finally:
            await pool.stop()

    return asyncio.run(go())


def test_click_follows_a_redirect_chain_and_reports_every_hop(site):
    _, base = site
    res = _run(site, "/", "#go")
    assert res.final_url == base + "/final" and "arrived" in res.body
    hops = [(h["url"].replace(base, ""), h["status"], (h["location"] or "").replace(base, "")) for h in res.redirect_chain]
    assert hops == [("/", 200, ""), ("/hop1", 302, "/hop2"), ("/hop2", 301, "/final"), ("/final", 200, "")]


def test_click_into_a_huge_video_returns_its_url_and_never_the_file(site):
    srv, base = site
    res = _run(site, "/", "#dl")
    assert res.final_url == base + "/movie.mkv" and res.body == ""
    assert res.note and "video/x-matroska" in res.note
    assert [h["status"] for h in res.redirect_chain] == [200, 302, 200]
    assert srv.video_mb_sent < 60, f"browser kept streaming the video ({srv.video_mb_sent} MB)"


def test_direct_video_url_is_not_fetched(site):
    srv, base = site
    res = _run(site, "/movie.mkv")
    assert res.body == "" and res.final_url == base + "/movie.mkv" and res.note
    assert srv.video_mb_sent < 60


def test_click_that_triggers_nothing_reports_it(site):
    res = _run(site, "/", "#noop")
    assert res.redirect_chain == [] and any("no navigation" in n for n in res.notes)
