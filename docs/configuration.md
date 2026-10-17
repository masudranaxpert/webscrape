# Configuration

All settings are environment variables, read once at start-up in `app/core/config.py` (restart to change).
`.env.example` is the annotated template; on NextDeploy they are set in the panel (`nd env list <app-id>`).

Empty values are treated as unset for strings; booleans accept `true/1/yes`. A non-numeric value for a numeric
setting stops the app at start-up.

## Required

| Variable | Default | Used in | What / why |
| --- | --- | --- | --- |
| `API_KEY` | — | `core/auth.py` | Key for `X-API-Key` / Bearer auth. The app refuses to start without it unless `DEBUG=true` |
| `CLOAKBROWSER_LICENSE_KEY` | empty | `core/browser.py`, `core/binaries.py` | Pro license: only the default browser can use it, see [Browsers & license](browsers.md). Empty or invalid → keyless build (challenged more often). An "invalid or expired" answer is remembered for 10 min so launches stop re-validating it (the free plan reports a seat busy on another machine the same way) |

## Server

| Variable | Default | What / why |
| --- | --- | --- |
| `PORT` | `8000` | Uvicorn port (entrypoint) |
| `DEBUG` | `false` | Verbose logs; allows starting without `API_KEY`. **Keep false in production** |

## Browser engine

| Variable | Default | Used in | What / why |
| --- | --- | --- | --- |
| `BROWSER_HEADLESS` | `false` | `browser.py` | Headed Chromium in Xvfb is harder to detect. Only `true/1/yes` switches to headless |
| `BROWSER_GEOIP` | `true` | `browser.py` | Match timezone, locale and WebRTC IP to the exit IP |
| `MAX_TABS` | `5` | `browser.py` | Tabs open at once across all browsers. See [Benchmark](operations/benchmark.md) before raising |
| `MAX_BROWSERS` | `1` | `browser.py` | Max proxied Chromium processes (WARP counts as proxied). `0` = proxied solving off (409) |
| `BROWSER_MAX_LIFETIME_SEC` | `300` | `browser.py` | Idle age after which a keyless process is recycled (frees memory, drops HTTP cache) |
| `BROWSER_LICENSED_LIFETIME_SEC` | `3600` | `browser.py` | Same for the licensed seat holder (relaunching it costs a seat round-trip and a cold clearance) |
| `BROWSER_EXIT_IP_CHECK_SEC` | `60` | `browser.py` | How often a proxied process re-checks its exit IP |

## Liveness timeouts

| Variable | Default | What / why |
| --- | --- | --- |
| `BROWSER_JOB_OVERHEAD_SEC` | `90` | Hard browser job ceiling = `cf_wait` + this (504 past it). Keep below your client's HTTP timeout |
| `BROWSER_QUEUE_TIMEOUT_SEC` | `60` | Longest wait for a free tab (503 past it) |
| `BROWSER_CALL_TIMEOUT_SEC` | `10` | Ceiling for one page call (`evaluate`, `content`, `cookies`). Exceeding it poisons the process |
| `BROWSER_CLICK_TIMEOUT_SEC` | `30` | Ceiling for one humanized Turnstile click |
| `PROXY_QUEUE_BYTE_LIMIT` | `16777216` | Request-body bytes allowed to wait for a proxied slot |

## Egress

| Variable | Default | What / why |
| --- | --- | --- |
| `WARP_PROXY` | empty | Default **browser** egress, e.g. `http://warp:8080`. HTTP stays direct except for hosts WARP solved. See [Egress & WARP](egress.md) |
| `ALLOW_PRIVATE_TARGETS` | `false` | Turns off the SSRF guard (localhost/private/internal targets). Local development only |

## Response size

| Variable | Default | What / why |
| --- | --- | --- |
| `MAX_RESPONSE_MB` | `16` | Largest body the HTTP engine reads; binary/attachment/oversized → status, headers and URL only |

## Cookie cache

| Variable | Default | What / why |
| --- | --- | --- |
| `DEFAULT_COOKIE_TTL` | `86400` | Default jar lifetime (requests can override with `cookie_ttl`) |
| `DEFAULT_SESSION_TTL` | `600` | Cap for `session_id` jars |
| `MAX_CACHED_DOMAINS` | `64` | Host jars kept in memory (LRU) |
| `MAX_CACHED_SESSIONS` | `256` | Session jars kept in memory (LRU) |
| `COOKIE_CACHE_FILE` | `<app>/data/cookie_cache.json` | Disk copy of the cache; empty = memory only |
| `COOKIE_CACHE_MAX_MB` | `20` | Size cap of that file (oldest jars dropped first) |

## Turnstile solver

| Variable | Default | What / why |
| --- | --- | --- |
| `TURNSTILE_MAX_QUEUE` | `200` | Requests waiting per sitekey before 503 |
| `TURNSTILE_MINTERS_PER_KEY` | `1` | Parallel minters per sitekey. >1 makes Cloudflare escalate difficulty |
| `TURNSTILE_PREFETCH` | `0` | Spare tokens kept during a burst. 0 = mint strictly on demand (recommended: no wasted solves on the sitekey) |

## Set by the platform, not by the app

| Variable | Note |
| --- | --- |
| `CLOAKBROWSER_AUTO_UPDATE` | Forced to `false` (`config.py`) so a launch never downloads; updates run in the background instead (see [Browsers & license](browsers.md#updates-and-disk)) |
| `MALLOC_ARENA_MAX`, `DISPLAY` | Set by `docker-entrypoint.sh` |
| `VERBOSE_BROWSER_LOGS`, `COMPOSE_PROFILES` | Present in the production panel but **not read by the code**; safe to delete |

## WARP sidecar (docker-compose.yml)

| Variable | Value | Why |
| --- | --- | --- |
| `PROXY_MAX_CONN` | `1024` | gost's default of 10 concurrent connections is exhausted by one Chromium page load and wedges the proxy |
| `PROXY_MAX_RPS` | `1000` | Same for its default of 10 requests/second |
| `WARP_INSTANCES` | `1` | Single tunnel; the self-healing healthcheck relies on single-instance mode |
