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
    "*sess*",
    "*sid*",
    "*ssid*",
    "phpsessid",
    "jsessionid",
    "ts_*",
    "*argon*",
    "*csrf*",
    "*xsrf*",
    "*token*",
    "*auth*",
    "*jwt*",
    "*key*",
    "*pass*",
    "*login*",
    "*user*",
    "*uid*",
    "*guid*",
    "*identity*",
    "*member*",
    "*account*",
    "*secure*",
    "*hash*",
    "*remember*",
    "wordpress_*",
    "wp_*",
    "laravel_*",
    "django_*",
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
    def host_of(url: str) -> str:
        """Extract exact lowercase hostname from URL preserving subdomains."""
        host = urlparse(url).hostname or url
        return host.strip().lower()

    @staticmethod
    def root_domain_of(host: str) -> str:
        """Extract root domain (eTLD+1) for shared wildcard cookies."""
        parts = host.strip().lower().split(".")
        return ".".join(parts[-2:]) if len(parts) > 1 else host

    @staticmethod
    def domain_of(url: str) -> str:
        """Return full hostname for precise cookie scoping."""
        return CookieStore.host_of(url)

    @staticmethod
    def cache_key(host: str, session_id: str | None = None) -> str:
        """Generate isolated storage key: scopes by session_id when provided."""
        clean_host = host.strip().lower()
        if session_id and str(session_id).strip():
            return f"{clean_host}#{str(session_id).strip()}"
        return clean_host

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

    def get(self, key: str) -> dict[str, str] | None:
        """Get valid cookies for a key (host or host#session_id), evicting if expired."""
        entry = self._store.get(key)
        if entry is not None:
            if time.time() > entry["expires_at"]:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return dict(entry["cookies"])

        # Fallback: if querying a subdomain or session without stored cookies,
        # inherit root domain's wildcard Cloudflare clearance cookies if active
        host_part = key.split("#")[0]
        root = self.root_domain_of(host_part)
        if root != host_part and root in self._store:
            root_entry = self._store[root]
            if time.time() <= root_entry["expires_at"]:
                cf_only = {
                    k: v for k, v in root_entry["cookies"].items()
                    if k.lower().startswith(("cf_", "__cf"))
                }
                if cf_only:
                    return cf_only
        return None

    def get_ua(self, key: str) -> str | None:
        """Get cached user_agent for a key if still valid."""
        entry = self._store.get(key)
        if entry is None or time.time() > entry["expires_at"]:
            host_part = key.split("#")[0]
            root = self.root_domain_of(host_part)
            if root != host_part and root in self._store and time.time() <= self._store[root]["expires_at"]:
                return self._store[root].get("user_agent")
            return None
        return entry.get("user_agent")

    def get_identity(self, key: str) -> dict[str, str | None]:
        """Identity (UA, proxy, preset) the cached cookies were earned with."""
        entry = self._store.get(key)
        if entry is None or time.time() > entry["expires_at"]:
            host_part = key.split("#")[0]
            root = self.root_domain_of(host_part)
            if root != host_part and root in self._store and time.time() <= self._store[root]["expires_at"]:
                return {k: self._store[root].get(k) for k in ("user_agent", "proxy", "preset")}
            return {"user_agent": None, "proxy": None, "preset": None}
        return {k: entry.get(k) for k in ("user_agent", "proxy", "preset")}

    def set(
        self,
        key: str,
        cookies: dict[str, str],
        ua: str | None = None,
        proxy: str | None = None,
        preset: str | None = None,
        ttl: int = DEFAULT_COOKIE_TTL,
        allowlist: list[str] | None = None,
        inject_cookies: dict[str, str] | None = None,
        max_cookies: int = 35,
    ) -> None:
        """Set or update cookies and user-agent for a cache key, applying allowlist and limits."""
        clean_cookies, _ = self.filter_and_limit(
            cookies=cookies,
            allowlist=allowlist,
            inject_cookies=inject_cookies,
            max_cookies=max_cookies,
        )
        existing_ua = None
        if key in self._store:
            existing_ua = self._store[key].get("user_agent")
            self._store.move_to_end(key)
        elif len(self._store) >= self._max:
            evicted, _ = self._store.popitem(last=False)
            logger.debug("cookie_store: evicted LRU entry %s", evicted)
        self._store[key] = {
            "cookies": clean_cookies,
            "user_agent": ua or existing_ua,
            "proxy": proxy,
            "preset": preset,
            "expires_at": time.time() + ttl,
        }

        # Also store root domain wildcard cf_ clearance if present
        host_part = key.split("#")[0]
        root = self.root_domain_of(host_part)
        cf_cookies = {k: v for k, v in clean_cookies.items() if k.lower().startswith(("cf_", "__cf"))}
        if cf_cookies and root != key:
            if root not in self._store:
                self._store[root] = {
                    "cookies": cf_cookies,
                    "user_agent": ua or existing_ua,
                    "proxy": proxy,
                    "preset": preset,
                    "expires_at": time.time() + ttl,
                }
            else:
                self._store[root]["cookies"].update(cf_cookies)

    def invalidate(self, key: str) -> bool:
        """Remove a key from cache."""
        return self._store.pop(key, None) is not None

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
