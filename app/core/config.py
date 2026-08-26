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

DEBUG: bool = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
PORT: int = int(os.getenv("PORT", "8000"))
DEFAULT_COOKIE_TTL: int = int(os.getenv("DEFAULT_COOKIE_TTL", "3600"))
MAX_TABS: int = int(os.getenv("MAX_TABS", "3"))
BROWSER_HEADLESS: bool = os.getenv("BROWSER_HEADLESS", "true").lower() not in ("false", "0", "no")
VERBOSE_BROWSER_LOGS: bool = os.getenv("VERBOSE_BROWSER_LOGS", "false").lower() in ("true", "1", "yes")


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
