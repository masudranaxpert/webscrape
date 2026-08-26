"""FastAPI application factory and lifecycle management."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.v1.router import api_router
from app.core.browser import pool
from app.core.config import DEBUG, PORT, TEMPLATES_DIR, configure_logging
from app.web.router import web_router

configure_logging()
logger = logging.getLogger("anti")

TAGS_METADATA = [
    {
        "name": "Scraping Pipeline",
        "description": "Dual-engine scraping with automated Turnstile resolution and TLS fingerprinting.",
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
    logger.info("Anti API starting (DEBUG=%s, PORT=%d)", DEBUG, PORT)
    await pool.start()
    yield
    await pool.stop()
    logger.info("Anti API stopped")


app = FastAPI(
    title="Anti API",
    description="High-speed stealth web scraping service with automated Turnstile challenge bypass.",
    version="1.0.0",
    lifespan=lifespan,
    openapi_tags=TAGS_METADATA,
    docs_url="/docs",
    redoc_url="/redoc",
)

if TEMPLATES_DIR.exists():
    app.mount("/static", StaticFiles(directory=TEMPLATES_DIR), name="static")

app.include_router(web_router)
app.include_router(api_router)
