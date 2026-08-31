"""Self-check verification script for Anti modules and API flow."""

import asyncio
from app.core.browser import pool
from app.core.cookie_store import store
from app.core.fetcher import httpcloak_fetch


async def test_cookie_store() -> None:
    """Verify cookie allowlist patterns, cookie limits, and truncation."""
    # Basic LRU operations
    store.set("example.com", {"session": "abc123xyz"}, ttl=10)
    assert store.get("example.com") == {"session": "abc123xyz"}, "Failed to retrieve cached cookies"
    assert store.domain_of("https://sub.example.com/path") == "example.com", "eTLD+1 extraction mismatch"

    # Simulate 69 bloated cookies from movielinkbd.li
    raw_cookies = {
        "cf_clearance": "LbxZ6uEOnjFdTQzfNh_token",
        "__cf_bm": "cf_bot_management_token",
        "_ga": "GA1.1.123",
        "_ga_TEST": "GS2.1.123",
        "mlbd_v76_10_ntc": "shown",
        "movielinkbd_vote_browser_v57": "token_vote_123",
        "PHPSESSID": "sess_123",
    }
    for i in range(62):
        raw_cookies[f"mlbd_v76_10_view_{i:032x}"] = "1"

    assert len(raw_cookies) == 69

    # 1. Default allowlist test: keeps cf_clearance, __cf_bm, PHPSESSID; drops tracking & view cookies
    filtered_default, was_truncated = store.filter_and_limit(raw_cookies)
    assert "cf_clearance" in filtered_default
    assert "__cf_bm" in filtered_default
    assert "PHPSESSID" in filtered_default
    assert "_ga" not in filtered_default
    assert not any(k.startswith("mlbd_v76_10_view_") for k in filtered_default)
    assert was_truncated is True

    # 2. Custom allowlist with wildcard pattern matching
    custom_allowlist = ["cf_*", "movielinkbd_vote_*", "PHPSESSID"]
    filtered_custom, _ = store.filter_and_limit(raw_cookies, allowlist=custom_allowlist)
    assert "cf_clearance" in filtered_custom
    assert "movielinkbd_vote_browser_v57" in filtered_custom
    assert "PHPSESSID" in filtered_custom
    assert "__cf_bm" not in filtered_custom  # Not in custom allowlist

    # 3. Cookie limit truncation test
    filtered_limit, was_truncated_limit = store.filter_and_limit(
        {"cf_clearance": "tok", "s1": "1", "s2": "2", "s3": "3", "s4": "4"},
        allowlist=["*"],
        max_cookies=3,
    )
    assert len(filtered_limit) == 3
    assert "cf_clearance" in filtered_limit
    assert was_truncated_limit is True

    # 4. User inject_cookies preservation test
    filtered_injected, _ = store.filter_and_limit(
        {"custom_header_cookie": "val"},
        allowlist=["cf_*"],
        inject_cookies={"custom_header_cookie": "val"},
    )
    assert "custom_header_cookie" in filtered_injected

    print("[OK] CookieStore allowlist & limit self-checks passed")


async def test_httpcloak() -> None:
    """Verify httpcloak stealth TLS/HTTP requests."""
    res = await httpcloak_fetch("https://httpbin.org/get", timeout=15)
    assert res.status_code == 200, f"httpbin returned {res.status_code}"
    print("[OK] httpcloak self-check passed")


async def test_browser_pool() -> None:
    """Verify browser pool initialization, execution, and worker recycling."""
    await pool.start()
    res = await pool.solve_and_fetch("https://httpbin.org/get", cf_wait=10.0)
    assert res.status_code == 200, f"Browser returned status {res.status_code}"
    assert "origin" in res.body, "Response body does not contain expected payload"

    # Test worker recycling trigger
    first_browser = pool._browser
    assert first_browser is not None
    pool._browser_created_at = 0.0  # Force age expiry
    assert pool._should_recycle() is True

    # Next request should recycle and re-spawn a fresh browser
    res2 = await pool.solve_and_fetch("https://httpbin.org/get", cf_wait=10.0)
    assert res2.status_code == 200
    assert pool._browser is not None
    assert pool._browser is not first_browser, "Browser was not recycled as expected"

    await pool.stop()
    print("[OK] Browser pool & worker recycling self-checks passed")


async def main() -> None:
    await test_cookie_store()
    await test_httpcloak()
    await test_browser_pool()
    print("All checks passed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
