"""Fast HTTP fetching with browser TLS/TCP fingerprinting via httpcloak."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from urllib.parse import urlparse

import httpcloak

from app.core.config import DEBUG

logger = logging.getLogger(__name__)

_VALID_PROXIES = ("http://", "https://", "socks5://", "socks5h://", "masque://")
_CHALLENGE_SUBSTRINGS = (
    'id="challenge-running"',
    'id="challenge-stage"',
    'id="cf-challenge-running"',
    'class="cf-browser-verification"',
    'id="cf-please-wait"',
    'action="/?__cf_chl_f_tk=',
    'name="cf_challenge_form"',
)


@dataclasses.dataclass
class FetcherResult:
    status_code: int
    headers: dict[str, str]
    body: str
    cookies: dict[str, str]
    final_url: str
    protocol: str | None = None
    cf_wall: bool = False
    elapsed_ms: int = 0
    error: str | None = None


def _is_cf_wall(status: int, body: str, headers: dict[str, str] | None = None) -> bool:
    """Identify Cloudflare Turnstile and challenge interstitials."""
    if not body:
        return status in (403, 429, 503)

    body_lower = body.lower()

    # Block page titles
    if any(t in body_lower for t in ("<title>just a moment", "<title>attention required!", "<title>security check")):
        return True

    # Challenge DOM indicators
    has_challenge_element = any(ind in body for ind in _CHALLENGE_SUBSTRINGS)

    # 4xx/5xx status with Cloudflare challenge markers
    if status in (403, 429, 503):
        if has_challenge_element:
            return True
        if "cloudflare" in body_lower and any(w in body_lower for w in ("turnstile", "challenge", "ray id")):
            return True
        return True

    # 200 status during ongoing interstitial verification
    if has_challenge_element and any(w in body_lower for w in ("just a moment", "checking your browser", "verify you are human")):
        return True

    return False


def _sanitize_proxy(proxy: str | None) -> str | None:
    if not proxy or not isinstance(proxy, str):
        return None
    p = proxy.strip()
    return p if any(p.startswith(prefix) for prefix in _VALID_PROXIES) else None


def _sanitize_http_version(http_version: str | None) -> str | None:
    if not http_version or not isinstance(http_version, str):
        return None
    v = http_version.strip().lower()
    return v if v in ("h1", "h2", "h3") else None


async def httpcloak_fetch(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    cookies: dict[str, str] | None = None,
    preset: str = "chrome-latest-windows",
    proxy: str | None = None,
    http_version: str | None = None,
    timeout: int = 30,
) -> FetcherResult:
    """Execute stealth HTTP request with full browser wire fingerprint."""
    clean_proxy = _sanitize_proxy(proxy)
    clean_version = _sanitize_http_version(http_version)

    session_kwargs: dict = {
        "preset": preset,
        "timeout": timeout,
        "tcp_ttl": 128,
        "tcp_mss": 1460,
        "tcp_window_size": 64240,
        "tcp_window_scale": 8,
        "tcp_df": True,
        "ech_config_domain": "cloudflare-ech.com",
        "allow_redirects": True,
    }
    if clean_proxy:
        session_kwargs["proxy"] = clean_proxy
    if clean_version:
        session_kwargs["http_version"] = clean_version

    session = httpcloak.Session(**session_kwargs)

    try:
        # Pre-seed session cookie jar
        if cookies:
            domain = urlparse(url).hostname or url
            for name, value in cookies.items():
                session.set_cookie(name=name, value=value, domain=domain)

        req_headers = {**headers} if headers else {}
        ua_in_req = next((v for k, v in req_headers.items() if k.lower() == "user-agent"), None)

        if DEBUG:
            logger.debug(
                "httpcloak -> %s %s | preset=%s | ua=%s | version=%s | cookies=%d | proxy=%s",
                method.upper(), url, preset,
                (ua_in_req[:40] + "...") if ua_in_req else "preset-default",
                clean_version or "auto",
                len(cookies or {}), clean_proxy or "none",
            )

        t0 = time.monotonic()
        payload = body if method.upper() not in ("GET", "HEAD") else None

        try:
            resp = await asyncio.wait_for(
                session.request_async(
                    method=method.upper(),
                    url=url,
                    headers=req_headers,
                    data=payload,
                    cookies=cookies or {},
                ),
                timeout=float(timeout),
            )
        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.warning("httpcloak request timed out after %ds [%dms]: %s", timeout, elapsed_ms, url)
            return FetcherResult(
                status_code=0,
                headers={},
                body="",
                cookies={},
                final_url=url,
                protocol=None,
                cf_wall=True,
                elapsed_ms=elapsed_ms,
                error=f"HTTP request timed out after {timeout}s",
            )
        except Exception as err:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.warning("httpcloak request failed (%s) [%dms]: %s", type(err).__name__, elapsed_ms, err)
            return FetcherResult(
                status_code=0,
                headers={},
                body="",
                cookies={},
                final_url=url,
                protocol=None,
                cf_wall=True,
                elapsed_ms=elapsed_ms,
                error=str(err),
            )

        elapsed_ms = int((time.monotonic() - t0) * 1000)

        # Harvest cookies from session jar
        harvested: dict[str, str] = {}
        try:
            for c in session.get_cookies():
                harvested[c.name] = c.value
        except Exception:
            pass

        # Parse text/binary content
        body_text: str = getattr(resp, "text", None) or ""
        if not body_text:
            raw = getattr(resp, "content", None)
            if isinstance(raw, (bytes, bytearray)):
                body_text = raw.decode("utf-8", errors="replace")

        status: int = resp.status_code
        raw_hdrs = resp.headers or {}
        flat_headers = {
            k: ", ".join(v) if isinstance(v, list) else str(v)
            for k, v in raw_hdrs.items()
        }

        wall = _is_cf_wall(status, body_text, flat_headers)

        if DEBUG:
            logger.debug(
                "httpcloak <- status=%d | protocol=%s | cf_wall=%s | cookies=%d | elapsed=%dms",
                status, getattr(resp, "protocol", "?"), wall, len(harvested), elapsed_ms,
            )
        else:
            logger.info(
                "httpcloak: %s %s -> %d (%s) [%dms] cf_wall=%s",
                method.upper(), url, status, getattr(resp, "protocol", "?"), elapsed_ms, wall,
            )

        return FetcherResult(
            status_code=status,
            headers=flat_headers,
            body=body_text,
            cookies=harvested,
            final_url=getattr(resp, "final_url", url) or url,
            protocol=getattr(resp, "protocol", None),
            cf_wall=wall,
            elapsed_ms=elapsed_ms,
        )
    finally:
        session.close()
