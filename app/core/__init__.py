"""Core engine and configuration package."""

from app.core.browser import pool
from app.core.config import BROWSER_HEADLESS, DEBUG, DEFAULT_COOKIE_TTL, MAX_TABS, PORT, configure_logging
from app.core.cookie_store import store
from app.core.fetcher import httpcloak_fetch

__all__ = [
    "pool",
    "store",
    "httpcloak_fetch",
    "DEBUG",
    "PORT",
    "DEFAULT_COOKIE_TTL",
    "MAX_TABS",
    "BROWSER_HEADLESS",
    "configure_logging",
]
