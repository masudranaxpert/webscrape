# Anti API

A FastAPI service that fetches pages protected by Cloudflare and mints Turnstile tokens.

It tries a fast, TLS-fingerprinted HTTP request first and opens a stealth Chromium only when a challenge appears.
After the browser earns `cf_clearance`, later requests for that host go back to the fast HTTP engine with the same
cookies, User-Agent, TLS fingerprint and egress IP.

## Make your first request

```bash
curl -X POST https://your-domain.example/fetch \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/"}'
```

The optional `proxy` field picks the route:

| You send | Route |
| --- | --- |
| nothing | **Automatic, recommended.** Simple pages go out from the server; Cloudflare challenges and Turnstile tokens are solved by the default browser (WARP + licensed build when `WARP_PROXY` is set) |
| `"direct"` | The server's own IP for everything (no WARP, no proxy) |
| `"socks5://user:pass@host:port"` | Your proxy for everything |

Examples for every case, Python code and error meanings: **[Making requests](requests.md)**.

!!! tip "Working on the service? Read in this order"
    1. [Architecture](architecture.md) — the moving parts and how a request travels through them
    2. [Egress & WARP](egress.md) — which IP each request leaves from, with `WARP_PROXY` set and unset
    3. [Configuration](configuration.md) — every environment variable: what it does, where it is used, why
    4. [Design decisions](decisions.md) — why things are the way they are, so you do not undo them by accident

## What it does

| Endpoint | Purpose |
| --- | --- |
| [`POST /fetch`](api/fetch.md) | Fetch a URL: HTTP engine first, browser on a Cloudflare challenge, clearance replayed afterwards |
| [`POST /solve-turnstile`](api/turnstile.md) | Mint a Turnstile token for a sitekey bound to a domain |
| [`/cookies`](api/other.md#cookies) | Inspect and invalidate the cookie cache |
| [`GET /health`](api/other.md#health) | Pool, cache, Turnstile and WARP status (unauthenticated, no secrets) |
| `GET /docs` | Interactive OpenAPI (Swagger) for the running server |

## Run it yourself

```bash
pip install -r requirements.txt
cp .env.example .env          # set API_KEY
uvicorn main:app --host 0.0.0.0 --port 8000 --loop asyncio
```

Production runs with Docker Compose (the API plus a WARP sidecar). See [Deploy](operations/deploy.md).

## Repository map

| Path | What lives there |
| --- | --- |
| `main.py` | App factory and lifespan: starts the browser pool, WARP monitor, cookie-cache persistence |
| `app/api/v1/endpoints/fetch.py` | The `/fetch` pipeline: egress choice, HTTP engine, coalescing, browser escalation, caching |
| `app/core/fetcher.py` | HTTP engine (`httpcloak`): streaming reads, redirects, binary/oversized body skipping |
| `app/core/browser.py` | Browser pool: one Chromium per egress identity, tab slots, launch/recycle/poison rules |
| `app/core/fetch_flow.py` | What one browser tab does for `/fetch`: navigate, clear the wall, click, harvest |
| `app/core/turnstile.py` | `/solve-turnstile` token minter (warm widget page, queue, pre-minting) |
| `app/core/cookie_store.py` | In-memory LRU cookie cache, per host and per egress identity |
| `app/core/cache_persist.py` | Writes/restores the cookie cache to disk (20 MB cap) |
| `app/core/proxy.py` | Proxy validation, identity keys, default egress, SSRF guard, proxy-error detection |
| `app/core/warp.py` | WARP health probe shown in `/health` |
| `app/core/utils/` | Challenge probes, Turnstile click logic, Playwright helpers, injected JS |
| `templates/` | Web playground served at `/` |
| `tests/` | Pytest suite (no network or browser needed for most tests) |
