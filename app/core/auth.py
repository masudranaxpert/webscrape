"""Authentication dependency and in-memory daily playground rate limiter."""

from __future__ import annotations

import datetime
import secrets
import threading
from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import API_KEY

PLAYGROUND_DAILY_LIMIT = 50

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
http_bearer = HTTPBearer(auto_error=False)


class PlaygroundQuotaManager:
    """
    In-memory daily quota tracker for the web playground demo (50 req/day limit).
    """

    def __init__(self, limit: int = PLAYGROUND_DAILY_LIMIT) -> None:
        self.limit = limit
        self._current_date = datetime.date.today()
        self._count = 0
        self._lock = threading.Lock()

    def check_and_consume(self) -> tuple[int, int]:
        """Check and consume one quota unit. Raises 429 when daily limit is exhausted."""
        with self._lock:
            today = datetime.date.today()
            if today != self._current_date:
                self._current_date = today
                self._count = 0

            if self._count >= self.limit:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Daily demo limit ({self.limit} requests/day) reached for {today}. Provide a valid 'X-API-Key' for direct API access.",
                )
            self._count += 1
            return self._count, max(0, self.limit - self._count)

    def get_status(self) -> dict[str, int]:
        """Return current in-memory usage metrics."""
        with self._lock:
            today = datetime.date.today()
            if today != self._current_date:
                self._current_date = today
                self._count = 0
            return {
                "limit": self.limit,
                "used": self._count,
                "remaining": max(0, self.limit - self._count),
            }


quota_manager = PlaygroundQuotaManager()


async def verify_api_key(
    header_key: str | None = Security(api_key_header),
    bearer_creds: HTTPAuthorizationCredentials | None = Security(http_bearer),
) -> str:
    """
    Strictly validate API Key for API endpoints. 100% required.
    Returns 401 Unauthorized if API key is missing or invalid.
    """
    if not API_KEY:
        # If API_KEY is not configured in .env, permit execution
        return ""

    provided_key = header_key or (bearer_creds.credentials if bearer_creds else None)

    if not provided_key or not secrets.compare_digest(provided_key.strip(), API_KEY.strip()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key. API requests require a valid 'X-API-Key' header or Bearer token.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    return provided_key.strip()
