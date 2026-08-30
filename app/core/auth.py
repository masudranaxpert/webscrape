"""Authentication and server-side in-memory daily playground rate limiting."""

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
    In-memory daily quota tracker for public playground/demo requests.
    Enforces a strict server-side ceiling without relying on client localStorage.
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
                    detail=f"Daily demo limit of {self.limit} requests reached for {today}. Provide a valid 'X-API-Key' for unlimited access.",
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
    Validate incoming request authentication.
    - If valid API_KEY provided -> Unlimited access.
    - If invalid API_KEY provided -> 401 Unauthorized.
    - If no API_KEY provided -> Tracked and capped by in-memory daily limit (50 req/day).
    """
    provided_key = header_key or (bearer_creds.credentials if bearer_creds else None)

    # 1. Check if user provided an API Key
    if provided_key:
        if API_KEY and secrets.compare_digest(provided_key.strip(), API_KEY.strip()):
            return provided_key.strip()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key. Provide a valid 'X-API-Key' header or Bearer token.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    # 2. If API_KEY is configured on server and no key sent, enforce in-memory server quota
    quota_manager.check_and_consume()
    return "demo_playground"
