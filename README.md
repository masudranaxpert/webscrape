# Anti-Bot Scraping API

A FastAPI service that fetches pages protected by Cloudflare. It tries a fast TLS-fingerprinted HTTP request first and falls back to a stealth Chromium only when a challenge appears. It can also return the Turnstile token a page issues.

Live: https://your-domain.example

## How it works

1. **HTTP first.** `httpcloak` sends the request with a browser TLS fingerprint. Most requests end here.
2. **Browser on challenge.** `cloakbrowser` opens the page, clicks the Turnstile checkbox and collects `cf_clearance`.
3. **Replay.** Cookies are cached per host and per egress identity (direct or a specific proxy) together with the User-Agent and TLS preset that earned them, so later requests go back to step 1.

Concurrent requests for one domain share a single browser solve. Browser tabs are capped by `MAX_TABS`.

## Run

```bash
pip install -r requirements.txt
cp .env.example .env   # set API_KEY
uvicorn main:app --host 0.0.0.0 --port 8000
```

Docker: `docker compose up -d --build`. Interactive docs are at `/docs`.

## API

`POST /fetch` with an `X-API-Key` header.

```json
{
  "url": "https://example.com/page",
  "selector": "a.download",
  "selector_attr": "href",
  "selector_all": true
}
```

| Field | Purpose |
| --- | --- |
| `force_browser` | Skip the HTTP path |
| `force_http` | HTTP only: never escalate to the browser, return the raw response (403/challenge included). Cannot combine with `force_browser` |
| `wait_for_selector` | Wait until an element exists in the live DOM (useful for client-rendered dynamic content) |
| `click_selector` | Click a selector after challenge resolution to trigger in-page redirects or submissions |
| `cf_wait` | Seconds the solver may spend (default 25.0, exits immediately once resolved) |
| `session_id` | Isolated session namespace to prevent cookie collisions between concurrent scraping flows |
| `proxy` | Egress proxy (`http://`, `https://`, `socks5://`, `socks5h://`, credentials allowed). Omit for the `DEFAULT_PROXY` rule, `"direct"` to bypass it. See [Proxies](#proxies) |
| `preset`, `user_agent` | Network identity |
| `cookie_ttl`, `cookie_allowlist`, `inject_cookies` | Cookie cache control |
| `capture_token` | Return the Turnstile token |
| `token_field_names` | Extra field names to read the token from |
| `auto_token` | Capture automatically when the page embeds a widget (default `false`) |

### In-Page Actions & Multi-Step Flows

For pages where download buttons render dynamically or requiring in-browser form submission:

```bash
curl -X POST https://your-domain.example/fetch \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com/download-portal",
    "force_browser": true,
    "wait_for_selector": ".btn-download",
    "click_selector": "#verify-btn",
    "cf_wait": 35.0
  }'
```

### Turnstile tokens

The response carries `turnstile_token`, `turnstile_sitekey` and `turnstile_solved_at`. Tokens are single use and never cached.

Many sites submit the token themselves the moment it is issued and then remove the widget. For those, `token_submissions` holds the request the page sent and the server's answer, which is usually the useful part.

### Proxies

Use residential or ISP proxies for Cloudflare-protected sites: datacenter and VPN ranges (e.g. M247) are challenged on reputation alone.

- **One browser per egress identity.** CloakBrowser applies proxy, timezone, locale and WebRTC IP at launch, for the whole process. Each proxy therefore gets its own Chromium process, launched with timezone/locale derived from the proxy exit IP (GeoIP) and WebRTC pinned to that IP, so the fingerprint matches the IP. Processes are capped by `MAX_BROWSERS` (your license seats): when full, the least recently used idle process is closed first.
- **SOCKS5 with credentials works** (passed to Chromium's own proxy stack, which Playwright's per-context proxy cannot do).
- **Cookies never cross identities.** `cf_clearance` is bound to the IP that earned it, so the cookie cache is namespaced per proxy.
- **Fail closed.** An invalid proxy is rejected with 422, and a dead proxy returns 502; requests never silently fall back to a direct connection.
- **Credentials stay secret.** Proxy passwords are masked in logs and error messages.
- **Server-side routing.** Set `DEFAULT_PROXY` (and optionally `DEFAULT_PROXY_DOMAINS`) so clients don't need the credentials. A request can still pass its own `proxy`, or `"direct"` to skip the default.

```bash
DEFAULT_PROXY=http://user:pass@residential.example:8000
DEFAULT_PROXY_DOMAINS=target-site.site,example.com
```

Cookie endpoints: `GET /cookies`, `GET /cookies/{domain}`, `DELETE /cookies/{domain}`. Health check: `GET /health`.

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `API_KEY` | | Required for `/fetch` and `/cookies` |
| `PORT` | `8000` | |
| `MAX_TABS` | `5` | Concurrent browser tabs across all browser processes |
| `MAX_BROWSERS` | `1` | Browser processes alive at once, one per egress identity. Keep at or below your CloakBrowser license seats |
| `BROWSER_JOB_OVERHEAD_SEC` | `90` | Hard ceiling per browser job = `cf_wait` + this (queue wait included). A hung tab is cancelled, gets `504`, and its process is replaced. Keep the total below your client's HTTP timeout |
| `BROWSER_QUEUE_TIMEOUT_SEC` | `60` | Longest a request waits for a browser slot before failing fast with `503` |
| `BROWSER_CALL_TIMEOUT_SEC` | `10` | Ceiling for single page calls (`evaluate`, `content`, cookies, Turnstile clicks) that have no native timeout |
| `BROWSER_GEOIP` | `true` | Match timezone, locale and WebRTC IP to the egress IP (GeoLite2 DB is baked into the image) |
| `DEFAULT_PROXY` | | Proxy for requests that send no `proxy` |
| `DEFAULT_PROXY_DOMAINS` | | Comma-separated domains (subdomains included) routed through `DEFAULT_PROXY`; empty = all |
| `BROWSER_HEADLESS` | `false` | Default false: runs headfully inside Xvfb (:99) with Openbox for human-like screen geometry |
| `CLOAKBROWSER_LICENSE_KEY` | | Optional: unlocks latest Chromium build (v154+). Get a free key at [cloakbrowser.dev/free](https://cloakbrowser.dev/free). Without a key, falls back to v146. |
| `DEFAULT_COOKIE_TTL` | `86400` | Seconds |
| `MAX_CACHED_DOMAINS` | `64` | |

### CloakBrowser Engine & License

Anti uses [CloakBrowser](https://github.com/CloakHQ/cloakbrowser) for stealth Chromium automation with 87 C++ engine-level patches (WebGL, Canvas, Audio, Navigator, CDP flags) and `humanize=True` natural input simulation:

- **Without a license key:** CloakBrowser automatically downloads and runs the unauthenticated free binary (Chromium v146).
- **With a license key (`CLOAKBROWSER_LICENSE_KEY`):** CloakBrowser authenticates with the CDN and downloads the latest build (Chromium v154+).
  - A free license key is available at [cloakbrowser.dev/free](https://cloakbrowser.dev/free) (sign in with GitHub).
  - Set `CLOAKBROWSER_LICENSE_KEY=cb_...` in your `.env` file or environment.

## Notes

Use it on sites you own or are allowed to test. Cloudflare binds `cf_clearance` to the IP, User-Agent and TLS fingerprint that earned it, so keep those the same between the solve and the replay.
