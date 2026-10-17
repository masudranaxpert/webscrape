"""Application configuration and logging setup."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
TEMPLATES_DIR: Path = BASE_DIR / "templates"
DOCS_HTML_PATH: Path = TEMPLATES_DIR / "index.html"

API_KEY: str = os.getenv("API_KEY", "")
DEBUG: bool = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
PORT: int = int(os.getenv("PORT", "8000"))
DEFAULT_COOKIE_TTL: int = int(os.getenv("DEFAULT_COOKIE_TTL", "86400"))
MAX_TABS: int = int(os.getenv("MAX_TABS", "5"))
MAX_CACHED_DOMAINS: int = int(os.getenv("MAX_CACHED_DOMAINS", "64"))
MAX_CACHED_SESSIONS: int = int(os.getenv("MAX_CACHED_SESSIONS", "256"))
DEFAULT_SESSION_TTL: int = int(os.getenv("DEFAULT_SESSION_TTL", "600"))
BROWSER_HEADLESS: bool = os.getenv("BROWSER_HEADLESS", "false").strip().lower() in ("true", "1", "yes")
BROWSER_MAX_LIFETIME_SEC: int = int(os.getenv("BROWSER_MAX_LIFETIME_SEC", "300"))
# Close a browser other than the default one after this many seconds without a tab (0 = keep it); the default browser
# (WARP when set, else direct) is never closed and is kept launched in the background
BROWSER_IDLE_SHUTDOWN_SEC: float = float(os.getenv("BROWSER_IDLE_SHUTDOWN_SEC", "600"))

# Max proxy browsers (requests with a proxy URL) at once; the default (WARP) and direct browsers are not counted.
# 0 = proxy browsers off (409)
MAX_BROWSERS: int = max(0, int(os.getenv("MAX_BROWSERS", "1")))
# Binary/oversized bodies are not read: status, headers and the final URL are returned instead
MAX_RESPONSE_MB: float = float(os.getenv("MAX_RESPONSE_MB", "16"))
# cf_wait + this = hard job ceiling; keep below the client's HTTP timeout
BROWSER_JOB_OVERHEAD_SEC: float = float(os.getenv("BROWSER_JOB_OVERHEAD_SEC", "90"))
BROWSER_QUEUE_TIMEOUT_SEC: float = float(os.getenv("BROWSER_QUEUE_TIMEOUT_SEC", "60"))
# Ceiling for page.evaluate / content / cookies (no native timeout)
BROWSER_CALL_TIMEOUT_SEC: float = float(os.getenv("BROWSER_CALL_TIMEOUT_SEC", "10"))
# Generous: cancelling a humanized click mid-path means the widget never receives it
BROWSER_CLICK_TIMEOUT_SEC: float = float(os.getenv("BROWSER_CLICK_TIMEOUT_SEC", "30"))
# Seat holder stays warm far longer than keyless workers
BROWSER_LICENSED_LIFETIME_SEC: int = int(os.getenv("BROWSER_LICENSED_LIFETIME_SEC", "3600"))
# An exit-IP change retires the worker and drops its clearance cookies
BROWSER_EXIT_IP_CHECK_SEC: float = float(os.getenv("BROWSER_EXIT_IP_CHECK_SEC", "60"))
BROWSER_GEOIP: bool = os.getenv("BROWSER_GEOIP", "true").lower() in ("true", "1", "yes")
# Request-body bytes queued at once for proxied browser slots
PROXY_QUEUE_BYTE_LIMIT: int = int(os.getenv("PROXY_QUEUE_BYTE_LIMIT", str(16 * 1024 * 1024)))
# http://warp:8080 = the default browser egresses via the warp sidecar; restart to change
WARP_PROXY: str = os.getenv("WARP_PROXY", "").strip()
# /solve-turnstile: requests queued per sitekey before 503; parallel minters per sitekey (more than 1 makes
# Cloudflare escalate difficulty); spare tokens pre-minted during a burst (0 = strictly on demand: every solve
# answers a real request, so Cloudflare never sees wasted solves on the sitekey)
TURNSTILE_MAX_QUEUE: int = max(1, int(os.getenv("TURNSTILE_MAX_QUEUE", "200")))
TURNSTILE_MINTERS_PER_KEY: int = max(1, int(os.getenv("TURNSTILE_MINTERS_PER_KEY", "1")))
TURNSTILE_PREFETCH: int = max(0, int(os.getenv("TURNSTILE_PREFETCH", "0")))
# Cookie cache persisted across restarts/deploys ('' = memory only); oldest entries dropped past the size cap
COOKIE_CACHE_FILE: str = os.getenv("COOKIE_CACHE_FILE", str(BASE_DIR / "data" / "cookie_cache.json")).strip()
COOKIE_CACHE_MAX_MB: float = float(os.getenv("COOKIE_CACHE_MAX_MB", "20"))
# true = allow fetching localhost/private/internal hosts (SSRF guard off; only for local development)
ALLOW_PRIVATE_TARGETS: bool = os.getenv("ALLOW_PRIVATE_TARGETS", "false").strip().lower() in ("true", "1", "yes")

# Suppress cloakbrowser background auto-update checks
os.environ.setdefault("CLOAKBROWSER_AUTO_UPDATE", "false")


def configure_logging() -> None:
    """Initialize application-wide logging configuration."""
    level = logging.DEBUG if DEBUG else logging.INFO
    fmt = (
        "%(asctime)s [%(levelname)-8s] %(name)s | %(message)s"
        if DEBUG
        else "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    logging.basicConfig(level=level, format=fmt, datefmt="%Y-%m-%d %H:%M:%S", force=True)

    for noisy in ("websockets", "asyncio", "aiohttp", "urllib3", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
