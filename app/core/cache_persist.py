"""Cookie cache on disk: clearances earned before a deploy or restart are reused instead of re-solved.

The in-memory store stays the source of truth; it is written to COOKIE_CACHE_FILE (atomically, mode 0600: it holds
session cookies) every few seconds when it changed and once at shutdown, capped at COOKIE_CACHE_MAX_MB by dropping
the least recently used entries. Turnstile tokens are never persisted: they are single-use and die in 300s.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from app.core.config import COOKIE_CACHE_FILE, COOKIE_CACHE_MAX_MB
from app.core.cookie_store import store

logger = logging.getLogger(__name__)

_SAVE_INTERVAL_SEC = 30.0


def _max_bytes() -> int:
    return int(COOKIE_CACHE_MAX_MB * 1024 * 1024)


def load() -> None:
    if not COOKIE_CACHE_FILE:
        return
    path = Path(COOKIE_CACHE_FILE)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return
    except OSError as err:
        logger.warning("Cookie cache %s unreadable (%s); starting empty", path, err)
        return
    if len(raw) > _max_bytes() * 2:
        logger.warning("Cookie cache %s is %d bytes, over the limit; ignoring it", path, len(raw))
        return
    try:
        loaded = store.restore(raw)
    except Exception as err:
        logger.warning("Cookie cache %s is corrupt (%s); starting empty", path, err)
        return
    logger.info("Cookie cache restored: %d live entries from %s", loaded, path)


def _write(data: bytes) -> None:
    path = Path(COOKIE_CACHE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)  # readers only ever see a complete file


async def save(force: bool = False) -> None:
    if not COOKIE_CACHE_FILE or not (store.dirty or force):
        return
    data = store.snapshot(_max_bytes())  # in the event loop: the store is only touched from here
    store.dirty = False
    try:
        await asyncio.to_thread(_write, data)
    except OSError as err:
        store.dirty = True  # retry on the next tick
        logger.warning("Cookie cache write to %s failed: %s", COOKIE_CACHE_FILE, err)


async def run() -> None:
    """Background loop started from the app lifespan."""
    while True:
        await asyncio.sleep(_SAVE_INTERVAL_SEC)
        await save()
