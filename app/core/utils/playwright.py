"""Small Playwright call wrappers shared by the browser pool and the challenge probes."""
import asyncio
from typing import Any

from app.core.config import BROWSER_CALL_TIMEOUT_SEC


async def bounded(awaitable: Any, timeout: float = BROWSER_CALL_TIMEOUT_SEC) -> Any:
    """Await a Playwright call that has no native timeout (evaluate, content, cookies) with a hard ceiling."""
    return await asyncio.wait_for(awaitable, timeout=timeout)


async def cancel_download(download: Any) -> None:
    """Cancel a browser download so a multi-GB file never reaches the disk."""
    try:
        await download.cancel()
    except Exception:
        pass
