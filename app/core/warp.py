"""WARP egress proof: Cloudflare's trace endpoint, queried through WARP_PROXY itself.

Daemon up != tunnel usable; only warp=on proves real egress. State is telemetry for
/health — solve failures during an outage stay the caller's problem (no direct fallback).
"""
import asyncio
import logging

import aiohttp

from app.core.config import WARP_PROXY

logger = logging.getLogger(__name__)

state = "unknown"


async def refresh() -> None:
    global state
    if not WARP_PROXY:
        state = "unknown"
        return
    state = "off"
    try:
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get("https://www.cloudflare.com/cdn-cgi/trace", proxy=WARP_PROXY) as r:
                text = await r.text()
        for line in text.splitlines():
            if line.startswith("warp="):
                state = line.split("=", 1)[1] or "off"
                break
    except Exception:
        state = "unknown"
    if state != "on":
        logger.warning("WARP egress check through %s: %s", WARP_PROXY, state)


async def monitor(interval: int = 30) -> None:
    """Background loop; started from the app lifespan when WARP_PROXY is set."""
    while True:
        await refresh()
        await asyncio.sleep(interval)
