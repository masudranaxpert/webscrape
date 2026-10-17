"""CloakBrowser binaries: present before they are needed, updated in the background, old versions pruned.

Two builds live in the cache dir (~/.cloakbrowser, a Docker volume so they survive deploys):
- keyless Chromium (the version pinned by the installed cloakbrowser package): every browser without the seat
- Pro Chromium (latest build the license server offers): the one browser holding the license seat

Launches never download (CLOAKBROWSER_AUTO_UPDATE=false keeps them on cached builds). Instead this module, at
start-up and once a day, under the pool's launch lock so no Chromium starts mid-download:
1. makes sure the keyless build of the installed package exists (a package upgrade brings a new one),
2. with a valid key, moves the Pro build to the server's latest (check_for_pro_update ignores the freeze),
3. deletes every other chromium-* build that no running process uses.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_INTERVAL_SEC = 24 * 3600


def _in_use() -> set[Path]:
    """Binary dirs of running Chromium processes (never delete those)."""
    dirs: set[Path] = set()
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            exe = (proc / "cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
        except OSError:
            continue
        if "/chromium-" in exe:
            dirs.add(Path(exe).parent)
    return dirs


def _prepare_sync() -> dict:
    """Blocking work, run in a thread. Returns what it did, for the log."""
    from cloakbrowser import download
    from cloakbrowser.config import get_binary_dir, get_cache_dir, get_effective_version
    from cloakbrowser.license import resolve_license_key, validate_license

    report: dict = {}
    key = resolve_license_key(None)

    # 1. Keyless build for the installed package version. ensure_binary() takes the Pro path whenever a key is in
    #    the environment, so hide it for this call (the caller holds the launch lock: no launch can see it missing)
    saved = os.environ.pop("CLOAKBROWSER_LICENSE_KEY", None)
    try:
        report["keyless"] = download.ensure_binary(license_key=None)
    except Exception as err:
        report["keyless_error"] = str(err)[:200]
    finally:
        if saved:
            os.environ["CLOAKBROWSER_LICENSE_KEY"] = saved

    # 2. Pro build: track the server's latest while a key is valid
    if key:
        try:
            info = validate_license(key)
            if info and info.valid:
                report["pro_updated_to"] = download.check_for_pro_update(key)
                report["pro"] = get_effective_version(pro=True)
            else:
                report["pro"] = "license not valid"
        except Exception as err:
            report["pro_error"] = str(err)[:200]

    # 3. Prune: keep the current keyless and Pro builds and anything a running browser uses
    keep = {get_binary_dir()} | _in_use()
    keyless_marker = get_effective_version()
    if keyless_marker:
        keep.add(get_binary_dir(keyless_marker))
    pro_version = get_effective_version(pro=True)
    if pro_version:
        keep.add(get_binary_dir(pro_version, pro=True))
    removed = []
    for build in get_cache_dir().glob("chromium-*"):
        if build.is_dir() and build not in keep:
            shutil.rmtree(build, ignore_errors=True)
            removed.append(build.name)
    report["pruned"] = removed
    return report


async def prepare(launch_lock: asyncio.Lock) -> None:
    async with launch_lock:
        try:
            report = await asyncio.to_thread(_prepare_sync)
        except Exception as err:
            logger.warning("CloakBrowser binary preparation failed: %s", err)
            return
    logger.info("CloakBrowser binaries ready: %s", report)


async def run(launch_lock: asyncio.Lock) -> None:
    """Background loop started from the app lifespan: prepare now, then once a day."""
    while True:
        await prepare(launch_lock)
        await asyncio.sleep(_INTERVAL_SEC)
