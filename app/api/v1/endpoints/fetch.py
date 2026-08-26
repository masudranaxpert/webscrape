"""Stealth scraping endpoint with automatic fast-path and browser fallback."""

from __future__ import annotations

import logging
import time
from fastapi import APIRouter

from app.core.browser import pool
from app.core.cookie_store import store
from app.core.fetcher import httpcloak_fetch
from app.schemas.scraping import FetchRequest, FetchResponse, RequestMeta

from collections import OrderedDict

logger = logging.getLogger("anti.fetch")
router = APIRouter()

# Bounded LRU cache for origin domain -> post-challenge destination URL
_MAX_REDIRECTS = 500
_redirect_cache: OrderedDict[str, str] = OrderedDict()

# Global primitives for Request Coalescing (Thundering Herd protection)
import asyncio
_coalesce_events: dict[str, asyncio.Event] = {}
_coalesce_lock: asyncio.Lock = asyncio.Lock()


def _cache_redirect(domain: str, url: str) -> None:
    """Store redirect destination with LRU eviction."""
    if domain in _redirect_cache:
        _redirect_cache.move_to_end(domain)
    elif len(_redirect_cache) >= _MAX_REDIRECTS:
        _redirect_cache.popitem(last=False)
    _redirect_cache[domain] = url



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

    domain = store.domain_of(req.url)
    cached_cookies = store.get(domain) or {}
    cached_ua = store.get_ua(domain)
    effective_cookies = {**cached_cookies, **req.inject_cookies}
    cache_hit = bool(cached_cookies)

    logs.append(f"[INIT] Request initialized for target: {req.url} (Method: {req.method})")

    if cache_hit:
        logs.append(f"[CACHE] Found {len(cached_cookies)} pre-cached session cookies for domain '{domain}'")
    else:
        logs.append(f"[CACHE] Cache miss for domain '{domain}' (No prior session tokens)")

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

    # 1. Fast path: Direct fingerprint HTTP request
    if not req.force_browser:
        target_url = _redirect_cache.get(domain) if (_redirect_cache.get(domain) and effective_cookies) else req.url
        if target_url != req.url:
            logs.append(f"[REDIRECT-CACHE] Applying known post-challenge route: {target_url}")

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

        if not result.cf_wall:
            logs.append(f"[HTTP-SUCCESS] Received HTTP {result.status_code} ({result.protocol or 'h2'}) in {result.elapsed_ms}ms (No challenge wall)")
            if result.cookies:
                store.set(domain, {**effective_cookies, **result.cookies}, ua=effective_ua, ttl=req.cookie_ttl)
                logs.append(f"[CACHE] Stored {len(result.cookies)} session cookies for '{domain}' (TTL: {req.cookie_ttl}s)")

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
                ),
            )

        # Log detailed failure reason on HTTP fast-path
        if result.error:
            logs.append(f"[HTTP-FAIL] Fast-path HTTP connection error: {result.error} ({result.elapsed_ms}ms)")
            logs.append("[ESCALATE] Network/TLS failure on fast-path -> Escalating to Browser Solver")
        elif result.status_code in (403, 503, 429):
            logs.append(f"[HTTP-BLOCKED] Fast-path blocked by Cloudflare (HTTP {result.status_code}) in {result.elapsed_ms}ms")
            logs.append("[ESCALATE] Cloudflare challenge wall detected -> Escalating to Browser Solver")
        else:
            logs.append(f"[HTTP-INTERSTITIAL] Interstitial verification screen detected (HTTP {result.status_code}) in {result.elapsed_ms}ms")
            logs.append("[ESCALATE] Challenge detected -> Escalating to Browser Solver")

        logger.info("⚠ Challenge wall detected for %s (status=%d) — routing to browser engine", domain, result.status_code)
    else:
        logs.append("[FORCE] force_browser=true set in request -> Bypassing fast-path, routing directly to Browser Solver")

    # 2. Slow path: Cloudflare bypass via browser pool
    is_solver = True
    wait_event = None
    
    # Do not coalesce if force_browser=True
    if not req.force_browser:
        async with _coalesce_lock:
            if domain in _coalesce_events:
                is_solver = False
                wait_event = _coalesce_events[domain]
            else:
                wait_event = asyncio.Event()
                _coalesce_events[domain] = wait_event

    if not is_solver and wait_event:
        logs.append(f"[COALESCE] Waiting for concurrent browser solver to finish for '{domain}'...")
        await wait_event.wait()
        logs.append("[COALESCE] Solver finished! Retrying fast-path HTTP engine...")
        
        new_cached = store.get(domain) or {}
        new_ua = req.user_agent or store.get_ua(domain)
        retry_cookies = {**new_cached, **req.inject_cookies}
        retry_headers = {**req.headers}
        if new_ua and not any(k.lower() == "user-agent" for k in retry_headers):
            retry_headers["user-agent"] = new_ua
            
        retry_res = await httpcloak_fetch(
            url=req.url,
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
        )
        
        logs.append(f"[SOLVER] Browser tab finished in {br.elapsed_ms}ms -> Status: {br.status_code} | Cookies harvested: {len(br.cookies)}")

        # Cache harvested cookies, session User-Agent, and resolved redirect target
        if br.cookies:
            store.set(domain, br.cookies, ua=br.user_agent, ttl=req.cookie_ttl)
            logs.append(f"[CACHE] Saved {len(br.cookies)} harvested cookies (cf_clearance/session) and User-Agent for '{domain}'")
            if br.final_url and br.final_url != req.url:
                _cache_redirect(domain, br.final_url)
                logs.append(f"[REDIRECT] Cached post-challenge destination: {domain} -> {br.final_url}")
                
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
            protocol=None,
            extracted=extracted,
            logs=logs,
            meta=RequestMeta(
                via="browser",
                request_type="browser",
                preset=None,
                cf_bypass_attempted=True,
                cache_hit=False,
                cookies_used=len(effective_cookies),
                user_agent=br.user_agent or effective_ua,
            ),
        )

    except Exception as exc:
        total_ms = int((time.monotonic() - t_start) * 1000)
        logs.append(f"[ERROR] Browser solver execution error: {exc} ({total_ms}ms)")
        logs.append("[FAIL] Pipeline failed to resolve request")
        logger.exception("Browser engine failure on %s: %s", req.url, exc)
        return FetchResponse(
            status_code=502,
            headers={},
            body=f"Pipeline error: {exc}",
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
        # Wake up any requests waiting on this domain's browser solve
        if is_solver and wait_event:
            async with _coalesce_lock:
                if domain in _coalesce_events:
                    _coalesce_events[domain].set()
                    del _coalesce_events[domain]
