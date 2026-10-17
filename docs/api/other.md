# Other endpoints

## Cookies

All require the API key. Keys in the cache look like `host`, `px-<hash>@host` (earned through a proxy or WARP) or
`host#session_id`.

| Request | Meaning |
| --- | --- |
| `GET /cookies` | Every live host jar: cookies, cookie count, User-Agent, seconds left |
| `GET /cookies/{key}` | One jar. A subdomain without its own jar inherits only the root domain's `cf_clearance` |
| `DELETE /cookies/{key}` | Drop one jar and the redirect-cache entries of its root domain. Pass the full key (`px-…@host`) for proxied/WARP jars |

## Health

`GET /health` is unauthenticated and never shows proxies, cookies or sitekeys.

```json
{
  "status": "ok",
  "debug": false,
  "domains_cached": 12,
  "browser_pool": {"browsers": 2, "max_browsers": 1, "active_tabs": 1, "max_tabs": 5, "queued": 0,
                   "proxy_queue_bytes": 0, "licensed_browsers": 0},
  "turnstile": {"sitekeys": 1, "minting": 0, "pooled_tokens": 2, "queued": 0},
  "warp": "on"
}
```

`warp` appears only when `WARP_PROXY` is set: `on` means Cloudflare's trace endpoint, fetched through the proxy,
reports WARP egress; `off`/`unknown` means the proxy or tunnel is not usable right now.

`GET /solve-turnstile/health` returns the browser and Turnstile parts only.

## Web

| Path | Meaning |
| --- | --- |
| `/`, `/playground`, `/cache` | Web playground and cache explorer (the API key is entered in the page and kept in localStorage) |
| `POST /playground/fetch` | Same as `/fetch`, used by the playground |
| `/docs`, `/redoc` | OpenAPI UI for the running server |
