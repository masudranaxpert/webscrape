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
BROWSER_HEADLESS: bool = os.getenv("BROWSER_HEADLESS", "false").lower() not in ("false", "0", "no")
BROWSER_MAX_LIFETIME_SEC: int = int(os.getenv("BROWSER_MAX_LIFETIME_SEC", "300"))
# One Chromium process per egress identity (direct / each proxy); bounded by CloakBrowser license seats
MAX_BROWSERS: int = max(1, int(os.getenv("MAX_BROWSERS", "1")))
# Largest response body (MB) the HTTP engine reads into memory. A body is held several times over while it is decoded
# and serialized (measured ~12x), so one multi-GB video behind a redirect used to exhaust RAM+swap. Larger or binary
# responses are not read: status, headers and the final (redirect target) URL are returned instead.
MAX_RESPONSE_MB: float = float(os.getenv("MAX_RESPONSE_MB", "16"))
# Hard ceiling for one browser job, queue wait included: cf_wait + this overhead. A hung Playwright call can never hold
# a tab (and with MAX_BROWSERS=1 the whole pool) longer than this. Keep the total below the client's HTTP timeout.
BROWSER_JOB_OVERHEAD_SEC: float = float(os.getenv("BROWSER_JOB_OVERHEAD_SEC", "90"))
# Longest a request may queue for a browser slot before failing fast with 503
BROWSER_QUEUE_TIMEOUT_SEC: float = float(os.getenv("BROWSER_QUEUE_TIMEOUT_SEC", "60"))
# Per-call ceiling for page.evaluate / content / cookies (they have no native timeout)
BROWSER_CALL_TIMEOUT_SEC: float = float(os.getenv("BROWSER_CALL_TIMEOUT_SEC", "10"))
# Ceiling for one Turnstile click: humanized mouse paths run several seconds and stretch past 10s on a loaded 2-vCPU
# host; cancelling mid-path means the widget never receives the click, so this is deliberately generous
BROWSER_CLICK_TIMEOUT_SEC: float = float(os.getenv("BROWSER_CLICK_TIMEOUT_SEC", "30"))
# Which egress identity may use the CloakBrowser license seat: "any" (first come), "proxy" or "direct". With a
# one-seat plan and MAX_BROWSERS=2, "proxy" keeps the licensed build on the proxied (Cloudflare-heavy) traffic and
# runs direct traffic on the keyless v146 build, so the two never evict each other
BROWSER_LICENSED_IDENTITY: str = os.getenv("BROWSER_LICENSED_IDENTITY", "any").strip().lower()
# Idle lifetime for the process holding the license seat: kept warm far longer than unlicensed workers
BROWSER_LICENSED_LIFETIME_SEC: int = int(os.getenv("BROWSER_LICENSED_LIFETIME_SEC", "3600"))
# How often a proxied worker re-checks its exit IP; a change retires the process (after its tabs finish) and drops
# the clearance cookies earned on the old IP
BROWSER_EXIT_IP_CHECK_SEC: float = float(os.getenv("BROWSER_EXIT_IP_CHECK_SEC", "60"))
# Derive timezone, locale and WebRTC IP from the egress IP (needs cloakbrowser[geoip])
BROWSER_GEOIP: bool = os.getenv("BROWSER_GEOIP", "true").lower() in ("true", "1", "yes")

# Optional server-side proxy: applied to requests without an explicit `proxy` (send proxy="direct" to opt out)
DEFAULT_PROXY: str = os.getenv("DEFAULT_PROXY", "")
# Comma-separated domains routed through DEFAULT_PROXY (subdomains included); empty = every domain
DEFAULT_PROXY_DOMAINS: str = os.getenv("DEFAULT_PROXY_DOMAINS", "")


# Suppress background auto-update checks from cloakbrowser
os.environ.setdefault("CLOAKBROWSER_AUTO_UPDATE", "false")

def configure_logging() -> None:
    """Initialize application-wide logging configuration."""
    level = logging.DEBUG if DEBUG else logging.INFO
    fmt = (
        "%(asctime)s [%(levelname)-8s] %(name)s | %(message)s"
        if DEBUG
        else "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )

    # Suppress verbose external networking loggers
    for noisy in ("websockets", "asyncio", "aiohttp", "urllib3", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
