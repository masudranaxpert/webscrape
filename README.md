# Anti-Bot Scraping API

A FastAPI service that fetches pages protected by Cloudflare and mints Turnstile tokens. It tries a fast
TLS-fingerprinted HTTP request first and opens a stealth Chromium only when a challenge appears; after that, the same
site is served by the fast HTTP engine again with the cookies the browser earned.

**Full documentation:** <https://masudranaxpert.github.io/webscrape/> (architecture, every setting, operations).

## How it works

1. **HTTP first.** `httpcloak` sends the request with a real browser TLS/HTTP2 fingerprint. Most requests end here.
2. **Browser on challenge.** `cloakbrowser` opens the page, clicks the Turnstile checkbox and collects `cf_clearance`.
3. **Replay.** Cookies are cached per site and per IP together with the User-Agent and TLS preset that earned them,
   so later requests go back to step 1 (~0.1 s instead of ~10 s).

## Run

```bash
pip install -r requirements.txt
cp .env.example .env          # set API_KEY
uvicorn main:app --host 0.0.0.0 --port 8000 --loop asyncio
```

Docker (API + WARP sidecar): `docker compose up -d --build`. Interactive API docs on the running server: `/docs`.

## Make a request

Send your key as `X-API-Key` (or `Authorization: Bearer`).

```bash
curl -X POST https://your-domain.example/fetch \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/", "selector": "h1"}'
```

```bash
curl -X POST https://your-domain.example/solve-turnstile \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"sitekey": "0x4AAAAAAA...", "siteurl": "https://example.com/"}'
```

### The `proxy` field

| You send | Route |
| --- | --- |
| nothing | **Automatic** (recommended). Simple pages go out from the server; Cloudflare challenges and Turnstile tokens are solved by the default browser (WARP + licensed build when `WARP_PROXY` is set) |
| `"direct"` | The server's own IP for everything, no WARP, no proxy |
| `"socks5://user:pass@host:port"` (or `http://`, `https://`, `socks5h://`) | Your proxy for everything |

A bad proxy URL is a 422; an unreachable proxy returns `status_code: 502`. Requests never silently fall back to the
server IP. More examples: [Making requests](https://masudranaxpert.github.io/webscrape/requests/).

### Useful `/fetch` fields

| Field | Purpose |
| --- | --- |
| `selector`, `selector_attr`, `selector_all` | Extract text/attributes with a CSS selector into `extracted` |
| `force_http` / `force_browser` | Pick the engine yourself |
| `wait_for_selector` | Wait until JavaScript renders an element |
| `click_selector` | Click once ready and follow where it goes (`url`, `redirect_chain`; downloads return the file URL, never the file) |
| `cf_wait` | Seconds the browser may spend on a challenge (default 25) |
| `session_id` | Isolated cookie jar (separate logins never mix) |
| `method`, `headers`, `body` | Any method; the body is sent byte-for-byte |

All fields: [`POST /fetch`](https://masudranaxpert.github.io/webscrape/api/fetch/). Settings:
[Configuration](https://masudranaxpert.github.io/webscrape/configuration/).

## Notes

Use it on sites you own or are allowed to test. Cloudflare binds `cf_clearance` to the IP, User-Agent and TLS
fingerprint that earned it; the service keeps those identical between the solve and the replay.
