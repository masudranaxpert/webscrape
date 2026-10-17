"""FastAPI application factory and lifecycle management."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.v1.router import api_router
from app.core import cache_persist
from app.core.browser import pool
from app.core.config import API_KEY, DEBUG, PORT, TEMPLATES_DIR, WARP_PROXY, configure_logging
from app.web.router import web_router

configure_logging()
logger = logging.getLogger("anti")

TAGS_METADATA = [
    {
        "name": "Scraping Pipeline",
        "description": "Dual-engine scraping with automated Turnstile resolution and TLS fingerprinting.",
    },
    {
        "name": "Turnstile Solver",
        "description": "Standalone Turnstile token minting (sitekey + domain binding).",
    },
    {
        "name": "Cookie Management",
        "description": "In-memory LRU cookie store inspection and invalidation.",
    },
    {
        "name": "System Diagnostics",
        "description": "Engine health status and cache metrics.",
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not DEBUG and not API_KEY:
        raise RuntimeError("FATAL: API_KEY must be configured in environment for production.")
    logger.info("Anti API starting (DEBUG=%s, PORT=%d)", DEBUG, PORT)
    cache_persist.load()
    persist_task = asyncio.create_task(cache_persist.run())
    await pool.start()
    warp_task = None
    if WARP_PROXY:
        from app.core import warp

        warp_task = asyncio.create_task(warp.monitor())
        logger.info("WARP egress monitor started (%s)", WARP_PROXY)
    yield
    if warp_task:
        warp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await warp_task
    persist_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await persist_task
    await cache_persist.save(force=True)
    await pool.stop()
    logger.info("Anti API stopped")


app = FastAPI(
    title="Anti API",
    description="High-speed stealth web scraping service with automated Turnstile challenge bypass.",
    version="2.0.0",
    lifespan=lifespan,
    openapi_tags=TAGS_METADATA,
    docs_url="/docs",
    redoc_url="/redoc",
)

class FreshStaticFiles(StaticFiles):
    """Revalidate /static on every request: templates change between deploys, and a
    stale edge-cached JS file silently ships the previous UI (Cloudflare default-caches
    .js/.css for hours when the origin sends no Cache-Control)."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


if TEMPLATES_DIR.exists():
    app.mount("/static", FreshStaticFiles(directory=TEMPLATES_DIR), name="static")

app.include_router(web_router)
app.include_router(api_router)
