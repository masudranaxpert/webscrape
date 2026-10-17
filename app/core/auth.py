"""API key authentication dependency."""

from __future__ import annotations

import secrets
from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import API_KEY

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
http_bearer = HTTPBearer(auto_error=False)


async def verify_api_key(
    header_key: str | None = Security(api_key_header),
    bearer_creds: HTTPAuthorizationCredentials | None = Security(http_bearer),
) -> str:
    """
    Strictly validate API Key for API endpoints. 100% required.
    Returns 401 Unauthorized if API key is missing or invalid.
    """
    if not API_KEY:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error: API_KEY is not configured on the server.",
        )

    provided_key = header_key or (bearer_creds.credentials if bearer_creds else None)

    if not provided_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key. API requests require a valid 'X-API-Key' header or Bearer token.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    try:
        is_match = secrets.compare_digest(
            provided_key.strip().encode("utf-8"),
            API_KEY.strip().encode("utf-8"),
        )
    except Exception:
        is_match = False

    if not is_match:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key. API requests require a valid 'X-API-Key' header or Bearer token.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    return provided_key.strip()
