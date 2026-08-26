"""Domain-aware LRU in-memory cookie store with TTL expiration."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class CookieStore:
    """
    In-memory LRU cookie cache with per-domain TTL expiration.
    ponytail: single-process in-memory store; migrate to Redis if multi-worker scaling is required.
    """

    def __init__(self, max_domains: int = 10) -> None:
        self._max = max_domains
        self._store: OrderedDict[str, dict] = OrderedDict()

    @staticmethod
    def domain_of(url: str) -> str:
        """Extract root domain (eTLD+1) from URL."""
        host = urlparse(url).hostname or url
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) > 1 else host

    def get(self, domain: str) -> dict[str, str] | None:
        """Get valid cookies for a domain, evicting if expired."""
        entry = self._store.get(domain)
        if entry is None:
            return None
        if time.time() > entry["expires_at"]:
            del self._store[domain]
            return None
        self._store.move_to_end(domain)
        return entry["cookies"]

    def get_ua(self, domain: str) -> str | None:
        """Get cached user_agent for a domain if still valid."""
        entry = self._store.get(domain)
        if entry is None or time.time() > entry["expires_at"]:
            return None
        return entry.get("user_agent")

    def set(self, domain: str, cookies: dict[str, str], ua: str | None = None, ttl: int = 3600) -> None:
        """Set or update cookies and user-agent for a domain, evicting LRU entry when full."""
        existing_ua = None
        if domain in self._store:
            existing_ua = self._store[domain].get("user_agent")
            self._store.move_to_end(domain)
        elif len(self._store) >= self._max:
            evicted, _ = self._store.popitem(last=False)
            logger.debug("cookie_store: evicted LRU domain %s", evicted)
        self._store[domain] = {
            "cookies": cookies,
            "user_agent": ua or existing_ua,
            "expires_at": time.time() + ttl,
        }

    def invalidate(self, domain: str) -> bool:
        """Remove a domain from cache."""
        return self._store.pop(domain, None) is not None

    def all_domains(self) -> dict[str, dict]:
        """Return non-expired domain entries with remaining TTL."""
        now = time.time()
        return {
            d: {
                "cookies": e["cookies"],
                "cookie_count": len(e["cookies"]),
                "user_agent": e.get("user_agent"),
                "ttl_remaining": max(0, round(e["expires_at"] - now)),
            }
            for d, e in self._store.items()
            if now <= e["expires_at"]
        }


from app.core.config import MAX_CACHED_DOMAINS
store = CookieStore(max_domains=MAX_CACHED_DOMAINS)
