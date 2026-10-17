"""Service health and diagnostics endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core import turnstile
from app.core.browser import pool
from app.core.config import DEBUG, WARP_PROXY
from app.core.cookie_store import store

router = APIRouter()


@router.get(
    "/health",
    summary="Service and engine health status",
    response_description="System status, active cache count, and diagnostic flags.",
)
async def health() -> JSONResponse:
    """Diagnostic endpoint checking service uptime, cookie metrics, and browser pool status."""
    cached = store.all_domains()
    body = {
        "status": "ok",
        "debug": DEBUG,
        "domains_cached": len(cached),
        "browser_pool": pool.stats(),
        "turnstile": turnstile.stats(),
    }
    if WARP_PROXY:
        from app.core import warp

        body["warp"] = warp.state
    return JSONResponse(body)
