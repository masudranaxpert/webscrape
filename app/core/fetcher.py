"""Fast HTTP fetching with browser TLS/TCP fingerprinting via httpcloak."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import time
from urllib.parse import urljoin, urlparse

import httpcloak

from app.core.config import DEBUG, MAX_RESPONSE_MB
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
    # True when the body was not (fully) read: binary/oversized response. status, headers and final_url stay valid
    truncated: bool = False
    note: str | None = None
    # Every hop of a redirect chain: {"url", "status", "location"}; empty when there was no redirect
    redirect_chain: list[dict] = dataclasses.field(default_factory=list)


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


# Extra seconds past the native timeout before giving up on the request from Python's side
_NATIVE_GRACE_SEC = 10.0

# Bodies in these families are never read: the caller wants the status, headers and final URL, not the bytes
_BINARY_TYPE_PREFIXES = ("video/", "audio/", "image/", "font/")
_BINARY_TYPES = frozenset({
    "application/octet-stream", "application/zip", "application/x-zip-compressed", "application/gzip",
    "application/x-gzip", "application/x-7z-compressed", "application/vnd.rar", "application/x-rar-compressed",
    "application/x-tar", "application/x-matroska", "application/vnd.apple.mpegurl", "application/x-msdownload",
    "application/pdf", "binary/octet-stream",
})
_READ_CHUNK = 256 * 1024


class _NativeTracker:
    """Closes an httpcloak session (and its streams) only after every native call using it has finished."""

    def __init__(self, session: "httpcloak.Session") -> None:
        self._session = session
        self._streams: list = []
        self._pending = 0
        self._released = False

    def watch(self, fut: "asyncio.Future") -> None:
        self._pending += 1
        fut.add_done_callback(self._done)

    def add_stream(self, stream) -> None:
        self._streams.append(stream)

    def _done(self, fut: "asyncio.Future") -> None:
        if not fut.cancelled():
            fut.exception()  # consume it so a late failure is not reported as "never retrieved"
        self._pending -= 1
        self._maybe_close()

    def release(self) -> None:
        self._released = True
        self._maybe_close()

    def _maybe_close(self) -> None:
        if not self._released or self._pending > 0:
            return
        for stream in self._streams:
            try:
                stream.close()
            except Exception:
                pass
        self._streams.clear()
        try:
            self._session.close()
        except Exception:
            pass


def _flatten_headers(raw) -> dict[str, str]:
    return {k: ", ".join(v) if isinstance(v, list) else str(v) for k, v in (raw or {}).items()}


def _header(headers: dict[str, str], name: str) -> str:
    return next((v for k, v in headers.items() if k.lower() == name), "")


def _skip_reason(method: str, headers: dict[str, str], content_length: int | None, max_bytes: int) -> str | None:
    """Why this body must not be read into memory (None = read it, up to max_bytes)."""
    ctype = _header(headers, "content-type").split(";")[0].strip().lower()
    if method == "HEAD":
        return None
    if ctype.startswith(_BINARY_TYPE_PREFIXES) or ctype in _BINARY_TYPES:
        return f"binary response ({ctype}) not read"
    if "attachment" in _header(headers, "content-disposition").lower():
        return "file download (Content-Disposition: attachment) not read"
    if content_length is not None and content_length > max_bytes:
        return f"body of {content_length / 1048576:.0f} MB exceeds the {max_bytes / 1048576:.0f} MB limit, not read"
    return None


def _read_capped(stream, max_bytes: int) -> tuple[bytes, bool]:
    """Read at most max_bytes from a stream (runs in a worker thread). Returns (data, hit_the_cap)."""
    chunks: list[bytes] = []
    total = 0
    for chunk in stream.iter_content(chunk_size=_READ_CHUNK):
        room = max_bytes - total
        if len(chunk) > room:
            chunks.append(chunk[:room])
            return b"".join(chunks), True
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), False


def _decode_body(data: bytes, headers: dict[str, str]) -> str:
    if not data:
        return ""
    charset = re.search(r"charset=([\w.-]+)", _header(headers, "content-type"), re.IGNORECASE)
    try:
        return data.decode(charset.group(1) if charset else "utf-8", errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_MAX_REDIRECTS = 10


async def _request(tracker: "_NativeTracker", session, *, method: str, url: str, headers: dict, payload,
                   cookies: dict, timeout: int, max_bytes: int):
    """Send the request as a stream, look at the headers, and read the body only if it is small, non-binary text.

    httpcloak's stream API does not follow redirects, so they are followed here, hop by hop, without reading any
    redirect body (browser rules: 301/302/303 turn a POST into a GET, 307/308 keep the method).
    Returns (response, body_bytes, note, redirect_chain). `note` is set when the body was skipped or cut at the cap;
    the chain lists every hop (empty when there was no redirect).
    Every native call is shielded and tracked, so a timeout never frees memory under a running goroutine.
    """
    start = time.monotonic()
    deadline = start + timeout + _NATIVE_GRACE_SEC
    origin_host = urlparse(url).hostname
    current_url, current_method, current_payload = url, method, payload

    stream = None
    chain: list[dict] = []
    for hop in range(_MAX_REDIRECTS + 1):
        remaining = max(1, int(timeout - (time.monotonic() - start)))
        # Per-request cookies belong to the origin host; a redirect to another site must not receive them
        # (cookies already stored in the session jar are scoped by domain and travel correctly)
        same_host = urlparse(current_url).hostname == origin_host
        native = asyncio.ensure_future(
            session.request_stream_async(
                method=current_method, url=current_url, headers=headers, data=current_payload,
                cookies=cookies if same_host else {}, allow_redirects=False, timeout=remaining,
            )
        )
        tracker.watch(native)
        stream = await asyncio.wait_for(asyncio.shield(native), timeout=max(1.0, deadline - time.monotonic()))
        tracker.add_stream(stream)

        flat = _flatten_headers(stream.headers)
        location = _header(flat, "location")
        chain.append({
            "url": current_url,
            "status": stream.status_code,
            "location": urljoin(current_url, location) if location and stream.status_code in _REDIRECT_STATUSES else None,
        })
        if stream.status_code in _REDIRECT_STATUSES and location and hop < _MAX_REDIRECTS:
            current_url = urljoin(current_url, location)
            if stream.status_code == 303 or (stream.status_code in (301, 302) and current_method not in ("GET", "HEAD")):
                current_method, current_payload = "GET", None
            continue  # the redirect body is never read; the stream is closed when the tracker is released
        break

    declared = getattr(stream, "content_length", None)
    declared = declared if isinstance(declared, int) and declared >= 0 else None
    reason = _skip_reason(current_method, flat, declared, max_bytes)
    if reason:
        logger.info("httpcloak: %s -> %s; body not read (status %s)", current_url, reason, stream.status_code)
        return stream, b"", reason, (chain if len(chain) > 1 else [])

    reader = asyncio.ensure_future(asyncio.to_thread(_read_capped, stream, max_bytes))
    tracker.watch(reader)
    data, capped = await asyncio.wait_for(asyncio.shield(reader), timeout=max(1.0, deadline - time.monotonic()))
    note = f"body cut at {max_bytes / 1048576:.0f} MB limit" if capped else None
    if capped:
        logger.warning("httpcloak: %s -> %s", current_url, note)
    return stream, data, note, (chain if len(chain) > 1 else [])


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
    # Closing the session while a native request (or body read) is still running frees memory its goroutine is still
    # using (use-after-free -> segfault that killed the whole API without a traceback): the tracker closes it only
    # once every native call has finished, even after a timeout or cancellation
    tracker = _NativeTracker(session)

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

        # The native side owns the deadline (timeout=); the outer guard only catches a native hang and is shielded,
        # because cancelling the native future makes its completion callback hit a dead future (InvalidStateError)
        try:
            resp, data, note, chain = await _request(
                tracker, session, method=method.upper(), url=url, headers=req_headers, payload=payload,
                cookies=cookies or {}, timeout=int(timeout), max_bytes=int(MAX_RESPONSE_MB * 1024 * 1024),
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
        except asyncio.CancelledError:
            # The caller was cancelled (e.g. a browser job deadline): the tracker closes the session once the
            # native request has finished on its own
            raise
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

        status: int = resp.status_code
        flat_headers = _flatten_headers(resp.headers)
        body_text = _decode_body(data, flat_headers)
        del data

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
            truncated=note is not None,
            note=note,
            redirect_chain=chain,
        )
    finally:
        tracker.release()
