"""Domain-aware LRU in-memory cookie store with pattern allowlist filtering and capacity limits."""

from __future__ import annotations

import fnmatch
import logging
import re
import time
from collections import OrderedDict
from urllib.parse import urlparse

from app.core.config import DEFAULT_COOKIE_TTL, MAX_CACHED_DOMAINS

logger = logging.getLogger(__name__)

# Default patterns for bypass tokens and standard session identifiers
DEFAULT_COOKIE_ALLOWLIST = [
    "cf_*",
    "__cf*",
    "*session*",
    "phpsessid",
    "jsessionid",
    "*csrf*",
    "*xsrf*",
    "*token*",
    "*auth*",
    "*jwt*",
    "*key*",
    "*login*",
    "*user*",
]

# Hard ceiling to prevent HTTP 400 header overflow on standard Nginx/OpenResty servers
MAX_COOKIE_HEADER_BYTES = 3500


def _matches_any_pattern(name: str, patterns: list[str]) -> bool:
    """Check if cookie name matches any pattern via exact name, glob wildcard, or regex."""
    name_lower = name.lower()
    for pat in patterns:
        pat_lower = pat.lower()
        if name_lower == pat_lower:
            return True
        if any(c in pat for c in ("*", "?", "[")):
            if fnmatch.fnmatchcase(name_lower, pat_lower):
                return True
        else:
            try:
                if re.search(f"^{pat}$", name, re.IGNORECASE):
                    return True
            except Exception:
                pass
    return False


class CookieStore:
    """
    In-memory LRU cookie cache with pattern allowlist filtering and cookie capacity limits.
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

    @staticmethod
    def filter_and_limit(
        cookies: dict[str, str],
        allowlist: list[str] | None = None,
        inject_cookies: dict[str, str] | None = None,
        max_cookies: int = 35,
        max_bytes: int = MAX_COOKIE_HEADER_BYTES,
    ) -> tuple[dict[str, str], bool]:
        """
        Filter cookies by allowlist patterns (glob/regex) and cap count/bytes.
        Returns: (filtered_cookies, was_truncated)
        """
        if not cookies:
            return {}, False

        total_input_count = len(cookies)
        patterns = allowlist if allowlist is not None else DEFAULT_COOKIE_ALLOWLIST
        injected_names = set(inject_cookies or {})

        # 1. Apply allowlist matching
        allowed: dict[str, str] = {}
        for name, value in cookies.items():
            if name in injected_names or _matches_any_pattern(name, patterns):
                allowed[name] = value

        was_truncated = False

        # 2. Enforce count limit (retaining priority CF/session tokens first, then recent items)
        if len(allowed) > max_cookies:
            was_truncated = True
            priority = {
                k: v for k, v in allowed.items()
                if k in injected_names or k.lower().startswith(("cf_", "__cf"))
            }
            # Fill remaining slots with newest general cookies
            for k, v in reversed(list(allowed.items())):
                if len(priority) >= max_cookies:
                    break
                priority[k] = v
            allowed = priority

        # 3. Enforce header byte-size limit
        while allowed and sum(len(k) + len(v) + 2 for k, v in allowed.items()) > max_bytes:
            was_truncated = True
            # Evict non-essential first
            non_cf = [k for k in allowed if not k.lower().startswith(("cf_", "__cf")) and k not in injected_names]
            if non_cf:
                del allowed[non_cf[0]]
            else:
                del allowed[next(iter(allowed))]

        is_reduced = was_truncated or (len(allowed) < total_input_count)
        return allowed, is_reduced

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

    def set(
        self,
        domain: str,
        cookies: dict[str, str],
        ua: str | None = None,
        ttl: int = DEFAULT_COOKIE_TTL,
        allowlist: list[str] | None = None,
        inject_cookies: dict[str, str] | None = None,
        max_cookies: int = 35,
    ) -> None:
        """Set or update cookies and user-agent for a domain, applying allowlist and limits."""
        clean_cookies, _ = self.filter_and_limit(
            cookies=cookies,
            allowlist=allowlist,
            inject_cookies=inject_cookies,
            max_cookies=max_cookies,
        )
        existing_ua = None
        if domain in self._store:
            existing_ua = self._store[domain].get("user_agent")
            self._store.move_to_end(domain)
        elif len(self._store) >= self._max:
            evicted, _ = self._store.popitem(last=False)
            logger.debug("cookie_store: evicted LRU domain %s", evicted)
        self._store[domain] = {
            "cookies": clean_cookies,
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


store = CookieStore(max_domains=MAX_CACHED_DOMAINS)
