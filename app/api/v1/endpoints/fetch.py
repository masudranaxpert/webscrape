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
    effective_cookies = {**cached_cookies, **req.inject_cookies}
    cache_hit = bool(cached_cookies)

    logs.append(f"[INIT] Request initialized for target: {req.url} (Method: {req.method})")

    if cache_hit:
        logs.append(f"[CACHE] Found {len(cached_cookies)} pre-cached session cookies for domain '{domain}'")
    else:
        logs.append(f"[CACHE] Cache miss for domain '{domain}' (No prior session tokens)")

    if req.inject_cookies:
        logs.append(f"[INJECT] Injected {len(req.inject_cookies)} custom cookies into request")

    # Merge custom User-Agent if explicitly provided
    effective_headers = {**req.headers}
    if req.user_agent and not any(k.lower() == "user-agent" for k in effective_headers):
        effective_headers["user-agent"] = req.user_agent
        logs.append(f"[HEADERS] Applied custom User-Agent override: {req.user_agent[:45]}...")

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
                store.set(domain, {**effective_cookies, **result.cookies}, ttl=req.cookie_ttl)
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
    logs.append(f"[BROWSER] Requesting worker tab from persistent Chromium pool (Timeout: {req.cf_wait}s)")
    try:
        br = await pool.solve_and_fetch(
            url=req.url,
            cf_wait=req.cf_wait,
            proxy=req.proxy,
            page_load_state=req.page_load_state,
            cookies=effective_cookies,
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

    logs.append(f"[SOLVER] Browser tab finished in {br.elapsed_ms}ms -> Status: {br.status_code} | Cookies harvested: {len(br.cookies)}")

    # Cache harvested cookies and resolved redirect target
    if br.cookies:
        store.set(domain, br.cookies, ttl=req.cookie_ttl)
        logs.append(f"[CACHE] Saved {len(br.cookies)} harvested cookies (cf_clearance/session) for '{domain}'")
        if br.final_url and br.final_url != req.url:
            _cache_redirect(domain, br.final_url)
            logs.append(f"[REDIRECT] Cached post-challenge destination: {domain} -> {br.final_url}")

    # 3. Retry fast path if browser hit a barrier but obtained cookies
    if not req.force_browser and br.cookies and br.status_code != 200:
        fresh_cookies = {**br.cookies, **req.inject_cookies}
        logs.append("[RETRY] Retrying fast HTTP engine with freshly solved Turnstile clearance cookies")
        retry = await httpcloak_fetch(
            url=br.final_url,
            method=req.method,
            headers=effective_headers,
            body=req.body,
            cookies=fresh_cookies,
            preset=req.preset,
            proxy=req.proxy,
            http_version=req.http_version,
            timeout=req.timeout,
        )

        if not retry.cf_wall:
            logs.append(f"[RETRY-SUCCESS] Fast-path retry succeeded (HTTP {retry.status_code}) in {retry.elapsed_ms}ms")
            extracted = _extract(retry.body, req.selector, req.selector_attr, req.selector_all) if req.selector else None
            if req.selector:
                logs.append(f"[EXTRACT] CSS selector '{req.selector}' extracted {len(extracted or [])} elements")

            total_ms = int((time.monotonic() - t_start) * 1000)
            logs.append(f"[DONE] Request resolved via fast HTTP retry in {total_ms}ms")
            logger.info("✓ http (retry) | %s -> %d", req.url, retry.status_code)

            return FetchResponse(
                status_code=retry.status_code,
                headers=retry.headers,
                body=retry.body,
                cookies=br.cookies,
                url=retry.final_url,
                protocol=retry.protocol,
                extracted=extracted,
                logs=logs,
                meta=RequestMeta(
                    via="http",
                    request_type="http_request",
                    preset=req.preset,
                    cf_bypass_attempted=True,
                    cache_hit=False,
                    cookies_used=len(fresh_cookies),
                ),
            )
        else:
            logs.append(f"[RETRY-FAIL] Retry returned status {retry.status_code} -> Falling back to browser rendered content")

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
        ),
    )
