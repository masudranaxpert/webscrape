"""Cookie management and inspection endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.core.cookie_store import store

router = APIRouter()


@router.get(
    "",
    summary="List all cached domain cookie jars",
    response_description="Dictionary mapping root domains to their active cookies and remaining TTLs in seconds.",
)
async def list_cookies() -> JSONResponse:
    """Retrieve all active domain cookie jars stored in memory."""
    return JSONResponse(store.all_domains())


@router.get(
    "/{domain}",
    summary="Get cached cookies for specific domain",
    response_description="Domain cookie dictionary with current active cookies.",
)
async def get_cookies(domain: str) -> JSONResponse:
    """Retrieve cached cookies for a specific root domain (e.g. `example.com`)."""
    cookies = store.get(domain)
    if cookies is None:
        raise HTTPException(status_code=404, detail=f"No active cookies for domain: {domain}")
    return JSONResponse({"domain": domain, "cookies": cookies})


@router.delete(
    "/{domain}",
    summary="Invalidate domain cookie cache",
    response_description="Confirmation of domain invalidation.",
)
async def invalidate_cookies(domain: str) -> JSONResponse:
    """Manually evict stored cookies for a domain from in-memory LRU store."""
    removed = store.invalidate(domain)
    return JSONResponse({"domain": domain, "invalidated": removed})
