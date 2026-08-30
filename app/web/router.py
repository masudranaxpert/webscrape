"""Web interface router for documentation and live playground tester."""

from __future__ import annotations

from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse

from app.api.v1.endpoints.fetch import fetch
from app.core.auth import quota_manager
from app.core.config import DOCS_HTML_PATH
from app.schemas.scraping import FetchRequest, FetchResponse

web_router = APIRouter(include_in_schema=False)


@web_router.get("/", response_class=HTMLResponse)
async def home_docs() -> HTMLResponse:
    """Serve interactive web documentation and playground."""
    if DOCS_HTML_PATH.exists():
        return HTMLResponse(content=DOCS_HTML_PATH.read_text(encoding="utf-8"))
    return HTMLResponse(
        content="<h1>Anti API Documentation</h1><p>Documentation template not found.</p>",
        status_code=404,
    )


@web_router.post("/playground/fetch", response_model=FetchResponse)
async def playground_fetch(req: FetchRequest) -> FetchResponse:
    """Execute live playground request with strict 50 req/day in-memory server limit."""
    quota_manager.check_and_consume()
    return await fetch(req)


@web_router.get("/favicon.ico")
async def favicon() -> Response:
    """Empty favicon response to prevent 404 logs."""
    return Response(status_code=204)
