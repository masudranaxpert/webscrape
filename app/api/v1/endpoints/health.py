"""Service health and diagnostics endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.config import DEBUG
from app.core.cookie_store import store

router = APIRouter()


@router.get(
    "/health",
    summary="Service and engine health status",
    response_description="System status, active cache count, and diagnostic flags.",
)
async def health() -> JSONResponse:
    """Diagnostic endpoint checking service uptime and cookie store metrics."""
    cached = store.all_domains()
    return JSONResponse({
        "status": "ok",
        "debug": DEBUG,
        "domains_cached": len(cached),
        "cached_domains": list(cached.keys()),
    })
