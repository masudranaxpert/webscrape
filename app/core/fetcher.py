"""Fast HTTP fetching with browser TLS/TCP fingerprinting via httpcloak."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import time
from urllib.parse import urljoin, urlparse

import httpcloak

from app.core.config import DEBUG
from app.core.proxy import mask_proxy, normalize_proxy, redact

logger = logging.getLogger(__name__)

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
    headers_dict = {str(k).lower(): str(v).lower() for k, v in (headers or {}).items()}

    # Cloudflare marks every challenge it serves with `cf-mitigated: challenge`, even on HTTP 200
    if "cf-mitigated" in headers_dict and "challenge" in headers_dict["cf-mitigated"]:
        return True

    if not body:
        # Empty body on 403 with Cloudflare server header indicates CF edge rejection
        return status == 403 and "cloudflare" in headers_dict.get("server", "")

    body_lower = body.lower()

    # Block page titles
    if any(t in body_lower for t in ("<title>just a moment", "<title>attention required!", "<title>security check")):
        return True

    # Challenge DOM indicators
    if any(ind in body for ind in _CHALLENGE_SUBSTRINGS):
        return True

    # Explicit Cloudflare WAF block markers on error responses
    if status in (403, 503) and ("cloudflare" in headers_dict.get("server", "") or any(m in body_lower for m in ("error 1020", "you have been blocked", "sorry, you have been blocked"))):
        return True

    return False


_WIDGET_MARKERS = ("cf-turnstile", "challenges.cloudflare.com/turnstile", "turnstile/v0/api.js")


def has_turnstile_widget(body: str) -> bool:
    """True when the raw HTML embeds a Turnstile widget (token can only be issued by a real browser)."""
    return bool(body) and any(m in body for m in _WIDGET_MARKERS)


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
    _redirect_depth: int = 0,
) -> FetcherResult:
    """Execute stealth HTTP request with full browser wire fingerprint."""
    # Fail closed: an unusable proxy raises instead of silently connecting direct and exposing the server IP
    clean_proxy = normalize_proxy(proxy)
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
        "max_redirects": 10,
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
                len(cookies or {}), mask_proxy(clean_proxy),
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
                    allow_redirects=True,
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
            logger.warning("httpcloak request failed (%s) [%dms]: %s", type(err).__name__, elapsed_ms, redact(str(err)))
            return FetcherResult(
                status_code=0,
                headers={},
                body="",
                cookies={},
                final_url=url,
                protocol=None,
                cf_wall=True,
                elapsed_ms=elapsed_ms,
                error=redact(str(err)),
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

        # WHATWG / Scrapy compliant Meta & Header Refresh handling
        if _redirect_depth < 5 and not wall:
            refresh_target = None
            refresh_hdr = next((v for k, v in flat_headers.items() if k.lower() == "refresh"), None)
            if refresh_hdr and "url=" in refresh_hdr.lower():
                delay_match = re.search(r"^\s*(\d+)", refresh_hdr)
                delay = int(delay_match.group(1)) if delay_match else 0
                if delay <= 2:
                    match = re.search(r"url\s*=\s*['\"]?([^'\"\s;]+)", refresh_hdr, re.IGNORECASE)
                    if match:
                        refresh_target = match.group(1).strip()

            # Fallback check for HTML <meta http-equiv="refresh" ...> tags on small redirect stubs
            if not refresh_target and len(body_text) < 4096 and "http-equiv" in body_text.lower():
                meta_match = re.search(
                    r'<meta[^>]*http-equiv=["\']?refresh["\']?[^>]*content=["\']?\s*(\d+)\s*;\s*url=([^"\'>\s]+)',
                    body_text,
                    re.IGNORECASE,
                )
                if meta_match and int(meta_match.group(1)) <= 2:
                    refresh_target = meta_match.group(2).strip()

            if refresh_target:
                current_url = getattr(resp, "final_url", url) or url
                redirect_target = urljoin(current_url, refresh_target)
                if redirect_target != current_url:
                    logger.info(
                        "Following Refresh redirect (depth=%d): %s -> %s",
                        _redirect_depth + 1, current_url, redirect_target,
                    )
                    merged_cookies = {**(cookies or {}), **harvested}
                    return await httpcloak_fetch(
                        url=redirect_target,
                        method="GET",
                        headers=headers,
                        body=None,
                        cookies=merged_cookies,
                        preset=preset,
                        proxy=proxy,
                        http_version=http_version,
                        timeout=timeout,
                        _redirect_depth=_redirect_depth + 1,
                    )

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
