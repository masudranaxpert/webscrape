"""Stealth scraping endpoint with automatic fast-path and browser fallback."""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlparse
from fastapi import APIRouter

from app.core.browser import BrowserJobTimeout, BrowserPoolBusy, pool
from app.core.cookie_store import store
from app.core.fetcher import has_turnstile_widget, httpcloak_fetch
from app.core.proxy import is_proxy_error, mask_proxy, proxy_key, redact, resolve_proxy
from app.schemas.scraping import FetchRequest, FetchResponse, RequestMeta


logger = logging.getLogger("anti.fetch")
router = APIRouter()

from collections import OrderedDict

_MAX_REDIRECTS = 1000
_redirect_cache: OrderedDict[str, str] = OrderedDict()


# Global primitives for Request Coalescing (Thundering Herd protection)
_coalesce_events: dict[str, asyncio.Event] = {}
_coalesce_lock: asyncio.Lock = asyncio.Lock()


def _cache_redirect(host: str, url: str) -> None:
    """Store redirect hostname with LRU eviction to prevent 403 loops on naked domains."""
    parsed_final = urlparse(url)
    if (
        parsed_final.hostname
        and parsed_final.hostname != host
        and store.root_domain_of(parsed_final.hostname) == store.root_domain_of(host)
    ):
        if host in _redirect_cache:
            _redirect_cache.move_to_end(host)
        elif len(_redirect_cache) >= _MAX_REDIRECTS:
            _redirect_cache.popitem(last=False)
        _redirect_cache[host] = parsed_final.netloc


def clear_redirect_cache(host: str | None = None) -> None:
    """Evict redirect target for a specific host (or all hosts sharing its root domain), or clear entirely if None."""
    if host:
        root = store.root_domain_of(host)
        keys_to_del = [
            k for k in _redirect_cache
            if k == host or store.root_domain_of(k) == root
        ]
        for k in keys_to_del:
            _redirect_cache.pop(k, None)
    else:
        _redirect_cache.clear()


def _preset_for_ua(ua: str | None) -> str | None:
    """Map the browser UA that earned the cookies onto the closest httpcloak TLS preset."""
    if not ua:
        return None
    low = ua.lower()
    if "firefox" in low:
        return "firefox-latest"
    if "linux" in low and "android" not in low:
        return "chrome-latest-linux"
    return "chrome-latest-windows"


def _extract(html: str, selector: str, attr: str, all_matches: bool) -> list[str]:
    """Parse HTML and extract node content via selectolax."""
    from selectolax.lexbor import LexborHTMLParser

    tree = LexborHTMLParser(html)
    nodes = tree.css(selector) if all_matches else [tree.css_first(selector)]
    results: list[str] = []

    for node in nodes:
        if node is None:
            continue
        if attr == "text":
            results.append(node.text(strip=True))
        elif attr == "html":
            results.append(node.html or "")
        else:
            val = node.attributes.get(attr)
            if val is not None:
                results.append(val)
    return results


def _selector_present(html: str, selector: str) -> bool:
    """True when the CSS selector matches at least one node in the HTML."""
    from selectolax.lexbor import LexborHTMLParser

    try:
        return LexborHTMLParser(html).css_first(selector) is not None
    except Exception:
        return False


@router.post(
    "/fetch",
    response_model=FetchResponse,
    summary="Execute stealth scraping request",
    response_description="Response payload containing status code, headers, body, cookies, execution logs, and metadata.",
)
async def fetch(req: FetchRequest) -> FetchResponse:
    """Fetch web content with automated Turnstile bypass and TLS fingerprinting."""
    logs: list[str] = []
    t_start = time.monotonic()

    host = store.host_of(req.url)

    # Egress identity: explicit proxy > "direct" opt-out > DEFAULT_PROXY routing rule
    proxy = resolve_proxy(req.proxy, host)
    req = req.model_copy(update={"proxy": proxy})
    identity = proxy_key(proxy)

    # cf_clearance is bound to the egress IP, UA and TLS fingerprint that earned it: cookies live in a per-proxy namespace
    cache_key = store.cache_key(host, req.session_id, identity)
    cached_cookies = store.get(cache_key) or {}
    ident = store.get_identity(cache_key)
    cached_ua = ident["user_agent"]
    if cached_cookies and "preset" not in req.model_fields_set and ident["preset"]:
        req = req.model_copy(update={"preset": ident["preset"]})
    raw_effective = {**cached_cookies, **req.inject_cookies}
    effective_cookies, was_truncated = store.filter_and_limit(
        cookies=raw_effective,
        allowlist=req.cookie_allowlist,
        inject_cookies=req.inject_cookies,
        max_cookies=req.cookie_limit,
    )
    cache_hit = bool(cached_cookies)

    logs.append(f"[INIT] Request initialized for target: {req.url} (Method: {req.method})")
    logs.append(f"[PROXY] Egress via {mask_proxy(proxy)}")

    session_label = f" (Session: {req.session_id})" if req.session_id else ""
    if cache_hit:
        logs.append(f"[CACHE] Found {len(cached_cookies)} pre-cached session cookies for host '{host}'{session_label}")
    else:
        logs.append(f"[CACHE] Cache miss for host '{host}'{session_label} (No prior session tokens)")

    if req.cookie_allowlist:
        logs.append(f"[ALLOWLIST] Applied custom cookie allowlist filter ({len(req.cookie_allowlist)} patterns): {req.cookie_allowlist}")

    if was_truncated or len(raw_effective) > len(effective_cookies):
        logs.append(
            f"[COOKIE-LIMIT] Cookie count ({len(raw_effective)}) exceeded limit ({req.cookie_limit}) or contained non-allowlisted cookies. "
            f"Safely truncated to {len(effective_cookies)} cookies for safe execution without HTTP 400."
        )

    if req.inject_cookies:
        logs.append(f"[INJECT] Injected {len(req.inject_cookies)} custom cookies into request")

    # Merge custom or domain-cached browser session User-Agent
    effective_headers = {**req.headers}
    effective_ua = req.user_agent or cached_ua
    if effective_ua and not any(k.lower() == "user-agent" for k in effective_headers):
        effective_headers["user-agent"] = effective_ua
        if req.user_agent:
            logs.append(f"[HEADERS] Applied custom User-Agent override: {effective_ua[:45]}...")
        elif cached_ua:
            logs.append(f"[CACHE] Applied cached session User-Agent: {effective_ua[:45]}...")

    if cached_cookies:
        logs.append(f"[IDENTITY] Sticky identity from cache: egress={mask_proxy(proxy)} preset={req.preset}")

    # 1. Fast path: Direct fingerprint HTTP request
    auto_capture = False
    force_browser = req.force_browser or req.capture_token or bool(req.token_field_names) or bool(req.click_selector)
    if not force_browser:
        target_url = req.url
        if effective_cookies and host in _redirect_cache:
            cached_netloc = _redirect_cache[host]
            parsed_req = urlparse(req.url)
            if parsed_req.netloc != cached_netloc:
                target_url = parsed_req._replace(netloc=cached_netloc).geturl()
                logs.append(f"[REDIRECT-CACHE] Rewriting target hostname to match post-challenge clearance: {target_url}")

        logs.append(f"[HTTP] Dispatching fast-path request via preset '{req.preset}'")
        result = await httpcloak_fetch(
            url=target_url,
            method=req.method,
            headers=effective_headers,
            body=req.body,
            cookies=effective_cookies,
            preset=req.preset,
            proxy=req.proxy,
            http_version=req.http_version,
            timeout=req.timeout,
        )

        if result.note:
            logs.append(f"[HTTP-LARGE] {result.note}: returning status, headers and final URL only")

        if req.force_http:
            # HTTP only: hand back whatever the origin answered, never escalate to the browser
            if result.error:
                logs.append(f"[HTTP-FAIL] force_http: {result.error} ({result.elapsed_ms}ms)")
                status_code, body = 502, f"HTTP request failed: {result.error}"
            else:
                logs.append(f"[HTTP-FORCED] force_http: returning HTTP {result.status_code} ({result.protocol or 'h2'}) in {result.elapsed_ms}ms without browser escalation")
                status_code, body = result.status_code, result.body
            total_ms = int((time.monotonic() - t_start) * 1000)
            logs.append(f"[DONE] Request resolved via HTTP Engine (forced) in {total_ms}ms")
            return FetchResponse(
                status_code=status_code,
                headers=result.headers,
                body=body,
                cookies=result.cookies or effective_cookies,
                url=result.final_url,
                redirect_chain=result.redirect_chain or None,
                protocol=result.protocol,
                extracted=_extract(result.body, req.selector, req.selector_attr, req.selector_all) if req.selector and not result.error else None,
                logs=logs,
                meta=RequestMeta(
                    via="http",
                    request_type="http_request",
                    preset=req.preset,
                    cf_bypass_attempted=False,
                    cache_hit=cache_hit,
                    cookies_used=len(effective_cookies),
                    user_agent=effective_ua,
                    body_truncated=result.truncated,
                    body_note=result.note,
                ),
            )

        if proxy and result.error and is_proxy_error(result.error):
            # The browser would go through the same dead proxy: fail fast instead of burning a browser solve
            proxy_err = redact(result.error)
            logs.append(f"[PROXY-FAIL] {mask_proxy(proxy)}: {proxy_err}")
            return FetchResponse(
                status_code=502,
                headers={},
                body=f"Proxy connection failed: {proxy_err}",
                cookies={},
                url=req.url,
                protocol=None,
                extracted=None,
                logs=logs,
                meta=RequestMeta(
                    via="http",
                    request_type="http_request",
                    preset=req.preset,
                    cf_bypass_attempted=False,
                    cache_hit=cache_hit,
                    cookies_used=len(effective_cookies),
                    user_agent=effective_ua,
                ),
            )

        selector_missing = bool(
            req.wait_for_selector and not result.cf_wall and not _selector_present(result.body, req.wait_for_selector)
        )
        needs_token = bool(req.auto_token and not result.cf_wall and has_turnstile_widget(result.body))
        if not result.cf_wall and not selector_missing and not needs_token:
            logs.append(f"[HTTP-SUCCESS] Received HTTP {result.status_code} ({result.protocol or 'h2'}) in {result.elapsed_ms}ms (No challenge wall)")
            if result.cookies:
                merged = {**effective_cookies, **result.cookies}
                store.set(
                    cache_key,
                    merged,
                    ua=effective_ua,
                    proxy=req.proxy,
                    preset=req.preset,
                    ttl=req.cookie_ttl,
                    allowlist=req.cookie_allowlist,
                    inject_cookies=req.inject_cookies,
                    max_cookies=req.cookie_limit,
                )
                cached_count = len(store.get(cache_key) or {})
                logs.append(f"[CACHE] Filtered & stored {cached_count} session cookies for '{cache_key}' (TTL: {req.cookie_ttl}s)")

            extracted = _extract(result.body, req.selector, req.selector_attr, req.selector_all) if req.selector else None
            if req.selector:
                logs.append(f"[EXTRACT] CSS selector '{req.selector}' extracted {len(extracted or [])} elements")

            total_ms = int((time.monotonic() - t_start) * 1000)
            logs.append(f"[DONE] Request resolved successfully via HTTP Engine in {total_ms}ms")
            logger.info("✓ http | %s -> %d | cache_hit=%s", req.url, result.status_code, cache_hit)

            return FetchResponse(
                status_code=result.status_code,
                headers=result.headers,
                body=result.body,
                cookies=result.cookies or effective_cookies,
                url=result.final_url,
                redirect_chain=result.redirect_chain or None,
                protocol=result.protocol,
                extracted=extracted,
                logs=logs,
                meta=RequestMeta(
                    via="http",
                    request_type="http_request",
                    preset=req.preset,
                    cf_bypass_attempted=False,
                    cache_hit=cache_hit,
                    cookies_used=len(effective_cookies),
                    user_agent=effective_ua,
                    body_truncated=result.truncated,
                    body_note=result.note,
                ),
            )

        # Log detailed failure reason on HTTP fast-path
        if needs_token:
            logs.append("[HTTP-WIDGET] Page embeds a Turnstile widget -> Escalating to Browser Solver to capture token (auto_token)")
            auto_capture = True
        elif selector_missing:
            logs.append(f"[HTTP-INCOMPLETE] HTTP {result.status_code} lacks selector '{req.wait_for_selector}' (rendered client-side)")
            logs.append("[ESCALATE] Dynamic content required -> Escalating to Browser Solver")
        elif result.error:
            logs.append(f"[HTTP-FAIL] Fast-path HTTP connection error: {result.error} ({result.elapsed_ms}ms)")
            logs.append("[ESCALATE] Network/TLS failure on fast-path -> Escalating to Browser Solver")
        elif result.status_code in (403, 503, 429):
            logs.append(f"[HTTP-BLOCKED] Fast-path blocked by Cloudflare (HTTP {result.status_code}) in {result.elapsed_ms}ms")
            logs.append("[ESCALATE] Cloudflare challenge wall detected -> Escalating to Browser Solver")
        else:
            logs.append(f"[HTTP-INTERSTITIAL] Interstitial verification screen detected (HTTP {result.status_code}) in {result.elapsed_ms}ms")
            logs.append("[ESCALATE] Challenge detected -> Escalating to Browser Solver")

        if result.cf_wall and cache_hit:
            # Preserve application session cookies so multi-step stateful flows (e.g. a login session) do not expire
            app_cookies = {
                k: v for k, v in cached_cookies.items()
                if not (k.startswith("cf_") or k.startswith("__cf"))
            }
            rejected_clearance = cached_cookies.get("cf_clearance")
            store.invalidate(cache_key, rejected_clearance=rejected_clearance)
            effective_cookies = {**app_cookies, **req.inject_cookies}
            logs.append("[CACHE] Cached clearance rejected by Cloudflare -> invalidated clearance, preserved application session cookies")

        logger.info("⚠ Challenge wall detected for %s (status=%d) — routing to browser engine", host, result.status_code)
    else:
        logs.append("[FORCE] browser forced (force_browser/capture_token) set in request -> Bypassing fast-path, routing directly to Browser Solver")

    # 2. Slow path: Cloudflare bypass via browser pool
    is_solver = True
    wait_event = None
    
    # Do not coalesce if force_browser=True, page-specific selector is required, or isolated session_id is provided
    if not force_browser and not auto_capture and not req.wait_for_selector and not req.session_id:
        async with _coalesce_lock:
            if cache_key in _coalesce_events:
                is_solver = False
                wait_event = _coalesce_events[cache_key]
            else:
                wait_event = asyncio.Event()
                _coalesce_events[cache_key] = wait_event

    if not is_solver and wait_event:
        logs.append(f"[COALESCE] Waiting for concurrent browser solver to finish for '{host}'...")
        await wait_event.wait()
        logs.append("[COALESCE] Solver finished! Retrying fast-path HTTP engine...")
        
        new_cached = store.get(cache_key) or {}
        new_ua = req.user_agent or store.get_ua(cache_key)
        retry_cookies = {**new_cached, **req.inject_cookies}
        retry_headers = {**req.headers}
        if new_ua and not any(k.lower() == "user-agent" for k in retry_headers):
            retry_headers["user-agent"] = new_ua
            
        retry_target_url = req.url
        if retry_cookies and host in _redirect_cache:
            cached_netloc = _redirect_cache[host]
            parsed_req = urlparse(req.url)
            if parsed_req.netloc != cached_netloc:
                retry_target_url = parsed_req._replace(netloc=cached_netloc).geturl()
                logs.append(f"[REDIRECT-CACHE] Rewriting target hostname for retry: {retry_target_url}")

        retry_res = await httpcloak_fetch(
            url=retry_target_url,
            method=req.method,
            headers=retry_headers,
            body=req.body,
            cookies=retry_cookies,
            preset=req.preset,
            proxy=req.proxy,
            http_version=req.http_version,
            timeout=req.timeout,
        )
        if not retry_res.cf_wall:
            logs.append(f"[HTTP-RETRY] Success! Received HTTP {retry_res.status_code} in {retry_res.elapsed_ms}ms")
            extracted = _extract(retry_res.body, req.selector, req.selector_attr, req.selector_all) if req.selector else None
            return FetchResponse(
                status_code=retry_res.status_code,
                headers=retry_res.headers,
                body=retry_res.body,
                cookies=retry_res.cookies or retry_cookies,
                url=retry_res.final_url,
                redirect_chain=retry_res.redirect_chain or None,
                protocol=retry_res.protocol,
                extracted=extracted,
                logs=logs,
                meta=RequestMeta(
                    via="http",
                    request_type="http_request_coalesced",
                    preset=req.preset,
                    cf_bypass_attempted=False,
                    cache_hit=True,
                    cookies_used=len(retry_cookies),
                    user_agent=retry_headers.get("user-agent", ""),
                    body_truncated=retry_res.truncated,
                    body_note=retry_res.note,
                ),
            )
        else:
            logs.append(f"[HTTP-RETRY] Still blocked (HTTP {retry_res.status_code}). Failing coalesced request.")
            return FetchResponse(
                 status_code=retry_res.status_code,
                 headers={},
                 body="Cloudflare bypass failed on coalesced retry",
                 cookies={},
                 url=req.url,
                 protocol="h2",
                 extracted=None,
                 logs=logs,
                 meta=RequestMeta(via="http", request_type="http_request_coalesced", preset=req.preset, cf_bypass_attempted=False, cache_hit=False, cookies_used=0, user_agent="")
            )

    logs.append(f"[BROWSER] Requesting worker tab from persistent Chromium pool (Timeout: {req.cf_wait}s)")
    try:
        br = await pool.solve_and_fetch(
            url=req.url,
            cf_wait=req.cf_wait,
            proxy=req.proxy,
            page_load_state=req.page_load_state,
            cookies=effective_cookies,
            wait_for_selector=req.wait_for_selector,
            capture_token=req.capture_token or bool(req.token_field_names) or auto_capture,
            token_field_names=req.token_field_names,
            click_selector=req.click_selector,
            method=req.method,
            headers=req.headers,
            body=req.body,
        )
        
        logs.extend(f"[SOLVER] {n}" for n in br.notes)
        logs.append(f"[SOLVER] Browser tab finished in {br.elapsed_ms}ms -> Status: {br.status_code} | Cookies harvested: {len(br.cookies)}")

        # Cache harvested cookies, session User-Agent, and resolved redirect target
        # Save whenever clearance is acquired or status is success
        if br.cookies and (br.status_code < 400 or "cf_clearance" in br.cookies):
            store.set(
                cache_key,
                br.cookies,
                ua=br.user_agent,
                proxy=req.proxy,
                preset=_preset_for_ua(br.user_agent),
                ttl=req.cookie_ttl,
                allowlist=req.cookie_allowlist,
                inject_cookies=req.inject_cookies,
                max_cookies=req.cookie_limit,
            )
            cached_count = len(store.get(cache_key) or {})
            logs.append(f"[CACHE] Saved {cached_count} harvested cookies and User-Agent for '{cache_key}'")
            if br.final_url and br.final_url != req.url:
                _cache_redirect(host, br.final_url)
                logs.append(f"[REDIRECT] Cached post-challenge destination: {host} -> {br.final_url}")
        extracted = _extract(br.body, req.selector, req.selector_attr, req.selector_all) if req.selector else None
        if req.selector:
            logs.append(f"[EXTRACT] CSS selector '{req.selector}' extracted {len(extracted or [])} elements")

        total_ms = int((time.monotonic() - t_start) * 1000)
        logs.append(f"[DONE] Request resolved via Browser Solver in {total_ms}ms")
        logger.info("✓ browser | %s -> %d", req.url, br.status_code)

        return FetchResponse(
            status_code=br.status_code,
            headers=br.headers,
            body=br.body,
            cookies=br.cookies,
            url=br.final_url,
            redirect_chain=br.redirect_chain or None,
            protocol=None,
            extracted=extracted,
            turnstile_token=br.turnstile_token,
            turnstile_sitekey=br.turnstile_sitekey,
            turnstile_solved_at=br.turnstile_solved_at,
            token_submissions=br.token_submissions or None,
            logs=logs,
            meta=RequestMeta(
                via="browser",
                request_type="browser",
                preset=None,
                cf_bypass_attempted=True,
                cache_hit=False,
                cookies_used=len(effective_cookies),
                user_agent=br.user_agent or effective_ua,
                body_truncated=br.note is not None,
                body_note=br.note,
            ),
        )

    except Exception as exc:
        total_ms = int((time.monotonic() - t_start) * 1000)
        # 503: pool saturated (retry later); 504: browser job hit its hard deadline; 502: anything else upstream
        status_code = 503 if isinstance(exc, BrowserPoolBusy) else 504 if isinstance(exc, BrowserJobTimeout) else 502
        logs.append(f"[ERROR] Browser solver execution error: {redact(str(exc))} ({total_ms}ms)")
        logs.append("[FAIL] Pipeline failed to resolve request")
        logger.error("Browser engine failure on %s (%d): %s", req.url, status_code, redact(str(exc)))
        return FetchResponse(
            status_code=status_code,
            headers={},
            body=f"Pipeline error: {redact(str(exc))}",
            cookies={},
            url=req.url,
            protocol=None,
            extracted=None,
            logs=logs,
            meta=RequestMeta(
                via="browser",
                request_type="browser",
                preset=None,
                cf_bypass_attempted=True,
                cache_hit=False,
                cookies_used=len(effective_cookies),
            ),
        )

    finally:
        # Wake up any requests waiting on this host's browser solve
        if is_solver and wait_event:
            async with _coalesce_lock:
                if cache_key in _coalesce_events:
                    _coalesce_events[cache_key].set()
                    del _coalesce_events[cache_key]
