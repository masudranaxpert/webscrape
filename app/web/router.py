"""Web interface router for documentation and live tester."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.core.config import DOCS_HTML_PATH

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


@web_router.get("/favicon.ico")
async def favicon() -> Response:
    """Empty favicon response to prevent 404 logs."""
    from fastapi import Response
    return Response(status_code=204)

