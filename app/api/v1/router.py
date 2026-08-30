"""API v1 router aggregating all endpoints with OpenAPI tags."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.v1.endpoints import cookies, fetch, health
from app.core.auth import verify_api_key

api_router = APIRouter()

api_router.include_router(
    fetch.router,
    tags=["Scraping Pipeline"],
    dependencies=[Depends(verify_api_key)],
)
api_router.include_router(
    cookies.router,
    prefix="/cookies",
    tags=["Cookie Management"],
    dependencies=[Depends(verify_api_key)],
)
api_router.include_router(
    health.router,
    tags=["System Diagnostics"],
)
