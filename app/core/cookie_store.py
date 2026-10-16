"""Domain-aware LRU in-memory cookie store with pattern allowlist filtering and capacity limits."""

from __future__ import annotations

import contextlib
import fnmatch
import logging
import time
from collections import OrderedDict
from urllib.parse import urlparse

try:
    import tldextract
    # Pre-configure with bundled snapshot to avoid network requests at runtime
    _tld_extractor = tldextract.TLDExtract(suffix_list_urls=None)
except Exception:
    _tld_extractor = None

from app.core.config import (
    DEFAULT_COOKIE_TTL,
    DEFAULT_SESSION_TTL,
    MAX_CACHED_DOMAINS,
    MAX_CACHED_SESSIONS,
)

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
    """Check if cookie name matches any pattern via exact name or glob wildcard."""
    name_lower = name.lower()
    for pat in patterns:
        pat_lower = pat.lower()
        if name_lower == pat_lower or fnmatch.fnmatchcase(name_lower, pat_lower):
            return True
    return False


class CookieStore:
    """
    In-memory LRU cookie cache with pattern allowlist filtering and cookie capacity limits.
    ponytail: single-process in-memory store; migrate to Redis if multi-worker scaling is required.
    """

    def __init__(
        self,
        max_domains: int = MAX_CACHED_DOMAINS,
        max_sessions: int = MAX_CACHED_SESSIONS,
    ) -> None:
        self._max_domains = max_domains
        self._max_sessions = max_sessions
        self._domain_store: OrderedDict[str, dict] = OrderedDict()
        self._session_store: OrderedDict[str, dict] = OrderedDict()

    @staticmethod
    def _is_session_key(key: str) -> bool:
        """Check if key represents an isolated session namespace."""
        return "#" in key

    @staticmethod
    def host_of(url: str) -> str:
        """Extract exact lowercase hostname from URL preserving subdomains."""
        host = urlparse(url).hostname or url
        return host.strip().lower()

    @staticmethod
    def root_domain_of(host: str) -> str:
        """Extract root domain (eTLD+1) for shared wildcard cookies using Public Suffix List."""
        clean_host = host.strip().lower()
        if _tld_extractor is not None:
            with contextlib.suppress(Exception):
                ext = _tld_extractor(clean_host)
                if ext.domain and ext.suffix:
                    return f"{ext.domain}.{ext.suffix}"
                if ext.domain:
                    return ext.domain
        # Fallback for common multi-part TLDs (.co.uk, .com.au, etc.) if tldextract unavailable
        parts = clean_host.split(".")
        if len(parts) > 2 and parts[-2] in ("co", "com", "org", "net", "edu", "gov") and len(parts[-1]) == 2:
            return ".".join(parts[-3:])
        return ".".join(parts[-2:]) if len(parts) > 1 else clean_host

    @staticmethod
    def domain_of(url: str) -> str:
        """Return full hostname for precise cookie scoping."""
        return CookieStore.host_of(url)

    @staticmethod
    def cache_key(host: str, session_id: str | None = None, identity: str | None = None) -> str:
        """Storage key `[identity@]host[#session_id]`.

        `identity` is the egress proxy id: cf_clearance is bound to the IP that earned it, so cookies earned through
        one proxy must never be replayed through another (or direct).
        """
        key = host.strip().lower()
        if identity and identity != "direct":
            key = f"{identity}@{key}"
        if session_id and str(session_id).strip():
            key = f"{key}#{str(session_id).strip()}"
        return key

    @classmethod
    def _root_key(cls, key: str) -> str:
        """Root-domain (eTLD+1) key within the same egress identity namespace, ignoring any session suffix."""
        base = key.split("#", 1)[0]
        identity, sep, host = base.rpartition("@")
        return f"{identity}{sep}{cls.root_domain_of(host)}"

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

        # Filter against allowlist patterns
        allowed: dict[str, str] = {}
        for name, value in cookies.items():
            if name in injected_names or _matches_any_pattern(name, patterns):
                allowed[name] = value

        was_truncated = False

        # Retain priority clearance and session tokens first when capping count
        if len(allowed) > max_cookies:
            was_truncated = True
            priority = {
                k: v for k, v in allowed.items()
                if k in injected_names or k.lower().startswith(("cf_", "__cf"))
            }
            for k, v in reversed(list(allowed.items())):
                if len(priority) >= max_cookies:
                    break
                priority[k] = v
            allowed = priority

        # Evict non-essential cookies first to fit byte ceiling
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
        if self._is_session_key(key):
            entry = self._session_store.get(key)
            if entry is None:
                return None
            if time.time() > entry["expires_at"]:
                del self._session_store[key]
                return None
            self._session_store.move_to_end(key)
            return dict(entry["cookies"])

        entry = self._domain_store.get(key)
        if entry is not None:
            if time.time() > entry["expires_at"]:
                del self._domain_store[key]
            else:
                self._domain_store.move_to_end(key)
                return dict(entry["cookies"])

        # Fallback: only for non-session keys! Inherit root domain's cf_clearance (and ONLY cf_clearance)
        root = self._root_key(key)
        if root != key and root in self._domain_store:
            root_entry = self._domain_store[root]
            if time.time() <= root_entry["expires_at"]:
                clr = root_entry["cookies"].get("cf_clearance")
                if clr:
                    return {"cf_clearance": clr}
        return None

    def get_ua(self, key: str) -> str | None:
        """Get cached user_agent for a key if still valid."""
        if self._is_session_key(key):
            entry = self._session_store.get(key)
            if entry and time.time() <= entry["expires_at"]:
                return entry.get("user_agent")
            return None

        entry = self._domain_store.get(key)
        if entry and time.time() <= entry["expires_at"]:
            return entry.get("user_agent")
        root = self._root_key(key)
        if root != key and root in self._domain_store and time.time() <= self._domain_store[root]["expires_at"]:
            return self._domain_store[root].get("user_agent")
        return None

    def get_identity(self, key: str) -> dict[str, str | None]:
        """Identity (UA, proxy, preset) the cached cookies were earned with."""
        if self._is_session_key(key):
            entry = self._session_store.get(key)
            if entry and time.time() <= entry["expires_at"]:
                return {k: entry.get(k) for k in ("user_agent", "proxy", "preset")}
            return {"user_agent": None, "proxy": None, "preset": None}

        entry = self._domain_store.get(key)
        if entry and time.time() <= entry["expires_at"]:
            return {k: entry.get(k) for k in ("user_agent", "proxy", "preset")}
        root = self._root_key(key)
        if root != key and root in self._domain_store and time.time() <= self._domain_store[root]["expires_at"]:
            return {k: self._domain_store[root].get(k) for k in ("user_agent", "proxy", "preset")}
        return {"user_agent": None, "proxy": None, "preset": None}

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
        if self._is_session_key(key):
            # Session entries have capped ephemeral TTL (default 10 min) and dedicated LRU store
            session_ttl = min(ttl, DEFAULT_SESSION_TTL)
            existing_ua = self._session_store.get(key, {}).get("user_agent")
            if key in self._session_store:
                self._session_store.move_to_end(key)
            elif len(self._session_store) >= self._max_sessions:
                evicted, _ = self._session_store.popitem(last=False)
                logger.debug("cookie_store: evicted LRU session %s", evicted)
            self._session_store[key] = {
                "cookies": clean_cookies,
                "user_agent": ua or existing_ua,
                "proxy": proxy,
                "preset": preset,
                "expires_at": time.time() + session_ttl,
            }
            # Isolated session NEVER writes to root domain
            return

        # Shared domain/host storage
        existing_ua = self._domain_store.get(key, {}).get("user_agent")
        if key in self._domain_store:
            self._domain_store.move_to_end(key)
        elif len(self._domain_store) >= self._max_domains:
            evicted, _ = self._domain_store.popitem(last=False)
            logger.debug("cookie_store: evicted LRU domain %s", evicted)
        self._domain_store[key] = {
            "cookies": clean_cookies,
            "user_agent": ua or existing_ua,
            "proxy": proxy,
            "preset": preset,
            "expires_at": time.time() + ttl,
        }


    def invalidate(
        self,
        key: str,
        purge_root: bool = False,
        rejected_clearance: str | None = None,
    ) -> bool:
        """
        Evict a key from cache. If rejected_clearance is provided,
        purge that specific rejected clearance from root domain as well.
        """
        removed = False
        if self._is_session_key(key):
            removed = self._session_store.pop(key, None) is not None
        else:
            removed = self._domain_store.pop(key, None) is not None

        root = self._root_key(key)
        if root in self._domain_store and root != key:
            if rejected_clearance:
                root_cookies = self._domain_store[root].get("cookies", {})
                if root_cookies.get("cf_clearance") == rejected_clearance:
                    root_cookies.pop("cf_clearance", None)
                    removed = True
                    if not root_cookies:
                        self._domain_store.pop(root, None)
            elif purge_root:
                self._domain_store.pop(root, None)
                removed = True
        return removed

    def invalidate_identity(self, identity: str) -> int:
        """Drop every entry earned through one egress identity (its exit IP changed, so its clearances are dead)."""
        prefix = f"{identity}@"
        removed = 0
        for bucket in (self._domain_store, self._session_store):
            for key in [k for k in bucket if k.startswith(prefix)]:
                del bucket[key]
                removed += 1
        return removed

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
            for d, e in self._domain_store.items()
            if now <= e["expires_at"]
        }


store = CookieStore(max_domains=MAX_CACHED_DOMAINS, max_sessions=MAX_CACHED_SESSIONS)
