# Anti-Bot Scraping API

A FastAPI service that fetches pages protected by Cloudflare. It tries a fast TLS-fingerprinted HTTP request first and falls back to a stealth Chromium only when a challenge appears. It can also return the Turnstile token a page issues.

Live: https://your-domain.example

## How it works

1. **HTTP first.** `httpcloak` sends the request with a browser TLS fingerprint. Most requests end here.
2. **Browser on challenge.** `cloakbrowser` opens the page, clicks the Turnstile checkbox and collects `cf_clearance`.
3. **Replay.** Cookies are cached per domain together with the User-Agent, proxy and TLS preset that earned them, so later requests go back to step 1.

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
| `wait_for_selector` | Wait until an element exists in the live DOM |
| `cf_wait` | Seconds the solver may spend (default 10, a page with two challenges needs about 25) |
| `proxy`, `preset`, `user_agent` | Network identity |
| `cookie_ttl`, `cookie_allowlist`, `inject_cookies` | Cookie cache control |
| `capture_token` | Return the Turnstile token |
| `token_field_names` | Extra field names to read the token from |
| `auto_token` | Capture automatically when the page embeds a widget (default `true`) |

### Turnstile tokens

The response carries `turnstile_token`, `turnstile_sitekey` and `turnstile_solved_at`. Tokens are single use and never cached.

Many sites submit the token themselves the moment it is issued and then remove the widget. For those, `token_submissions` holds the request the page sent and the server's answer, which is usually the useful part.

Cookie endpoints: `GET /cookies`, `GET /cookies/{domain}`, `DELETE /cookies/{domain}`. Health check: `GET /health`.

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `API_KEY` | | Required for `/fetch` and `/cookies` |
| `PORT` | `8000` | |
| `MAX_TABS` | `3` | Concurrent browser tabs |
| `BROWSER_HEADLESS` | `true` | |
| `DEFAULT_COOKIE_TTL` | `86400` | Seconds |
| `MAX_CACHED_DOMAINS` | `10` | |

## Notes

Use it on sites you own or are allowed to test. Cloudflare binds `cf_clearance` to the IP, User-Agent and TLS fingerprint that earned it, so keep those the same between the solve and the replay.
