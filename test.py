"""Self-check verification script for Anti modules and API flow."""

import asyncio
from app.core.browser import pool
from app.core.cookie_store import store
from app.core.fetcher import httpcloak_fetch


async def test_cookie_store() -> None:
    """Verify LRU cookie store operations."""
    store.set("example.com", {"session": "abc123xyz"}, ttl=10)
    assert store.get("example.com") == {"session": "abc123xyz"}, "Failed to retrieve cached cookies"
    assert store.domain_of("https://sub.example.com/path") == "example.com", "eTLD+1 extraction mismatch"
    print("[OK] CookieStore self-check passed")


async def test_httpcloak() -> None:
    """Verify httpcloak stealth TLS/HTTP requests."""
    res = await httpcloak_fetch("https://httpbin.org/get", timeout=15)
    assert res.status_code == 200, f"httpbin returned {res.status_code}"
    print("[OK] httpcloak self-check passed")


async def test_browser_pool() -> None:
    """Verify browser pool initialization and execution."""
    await pool.start()
    res = await pool.solve_and_fetch("https://httpbin.org/get", cf_wait=10.0)
    assert res.status_code == 200, f"Browser returned status {res.status_code}"
    assert "origin" in res.body, "Response body does not contain expected payload"
    await pool.stop()
    print("[OK] Browser pool self-check passed")


async def main() -> None:
    await test_cookie_store()
    await test_httpcloak()
    await test_browser_pool()
    print("All checks passed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
