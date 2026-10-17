# Making requests

Every API call needs your key, either header works:

```text
X-API-Key: <API_KEY>
Authorization: Bearer <API_KEY>
```

Base URL below is `https://your-domain.example`; replace it with your server.

## The `proxy` field: three choices

This one field decides which IP your request leaves from. It works the same on `/fetch` and `/solve-turnstile`.

| What you send | Meaning | Use it when |
| --- | --- | --- |
| *nothing* (leave `proxy` out) | **Automatic.** The server picks the best route (see below). | Almost always. Start here. |
| `"proxy": "direct"` | Force the **server's own IP** for everything. WARP and proxies are skipped. | You need the server IP specifically, or WARP is down. |
| `"proxy": "socks5://user:pass@host:port"` | Use **your proxy** for everything (HTTP and browser). | You need a specific country/IP, or residential proxies. |

### What "automatic" (no `proxy`) does

| Server setting | `/fetch` HTTP request | `/fetch` browser (when a challenge appears) | `/solve-turnstile` |
| --- | --- | --- | --- |
| `WARP_PROXY` not set | server IP | server IP | server IP |
| `WARP_PROXY` set (production) | server IP, **or WARP** for sites WARP already solved | **WARP** (licensed browser) | **WARP** (licensed browser) |

In plain words: with no `proxy`, simple pages load straight from the server, and anything protected by Cloudflare is
solved by the best browser the server has (WARP with the licensed build). After that, the same site keeps using WARP,
because the Cloudflare cookie only works from the IP that earned it.

### What `"direct"` means

`"direct"` = "do not use any proxy or WARP; use this server's own internet connection". Both the HTTP request and the
browser (if needed) go out from the server's IP. `"off"` means the same.

It is usually **slower against Cloudflare**: the server's datacenter IP with the keyless browser gets harder
challenges (Turnstile measured 11–27 s direct vs ~3.5 s automatic).

### Your own proxy

Supported schemes: `http://`, `https://`, `socks5://`, `socks5h://`. Credentials go in the URL:

```text
socks5://user:pass@203.0.113.7:1080
http://user:pass@proxy.example.com:8080
```

- The proxy is used for **both** engines, and Cloudflare cookies earned through it are kept separately and only
  reused through the same proxy.
- Proxy browsers always run the keyless build (Chrome 146); only the default browser holds the license seat.
- A broken or unreachable proxy returns `status_code: 502` with the reason. It never silently falls back to the
  server IP.
- A malformed proxy URL is rejected with HTTP 422.

## `/fetch` examples

**Simplest call: automatic route.**

```bash
curl -X POST https://your-domain.example/fetch \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/"}'
```

**Extract data with a CSS selector.**

```json
{"url": "https://example.com/list", "selector": "a.item", "selector_attr": "href", "selector_all": true}
```

**Force the server's own IP.**

```json
{"url": "https://example.com/", "proxy": "direct"}
```

**Through your proxy.**

```json
{"url": "https://example.com/", "proxy": "socks5://user:pass@203.0.113.7:1080"}
```

**Separate login sessions** (cookies never mix between `session_id`s).

```json
{"url": "https://example.com/account", "session_id": "user-42"}
```

**Choose the engine yourself.**

```json
{"url": "https://example.com/", "force_http": true}
{"url": "https://example.com/", "force_browser": true}
```

**Click a button, follow where it goes** (download links, redirects).

```json
{"url": "https://example.com/download/123", "click_selector": "button#download", "cf_wait": 40}
```

**Get the redirect target without following it** (download links).

```json
{"url": "https://example.com/get/42", "follow_redirects": false}
```

The answer has `status_code: 302` and `redirect_url` with the full link; nothing is downloaded. Details:
[Get a redirect without following it](api/fetch.md#get-a-redirect-without-following-it).

**Wait for content rendered by JavaScript.**

```json
{"url": "https://example.com/app", "wait_for_selector": ".results"}
```

**POST a JSON body.**

```json
{"url": "https://api.example.com/search", "method": "POST",
 "headers": {"Content-Type": "application/json"}, "body": "{\"q\": \"term\"}"}
```

### Reading the answer

```json
{
  "status_code": 200,
  "url": "https://example.com/",
  "body": "<!DOCTYPE html>...",
  "cookies": {"cf_clearance": "..."},
  "extracted": ["..."],
  "logs": ["[PROXY] HTTP egress via direct; browser escalation via http://warp:8080", "..."],
  "meta": {"via": "http", "cache_hit": true, "preset": "chrome-154-windows", "user_agent": "..."}
}
```

- `status_code` is the **target site's** status. The API itself answers HTTP 200 whenever it ran.
- `meta.via` tells you which engine answered (`http` = fast path, `browser` = a challenge was solved).
- The `[PROXY]` line in `logs` tells you which IP was used.

Full field list: [`POST /fetch`](api/fetch.md).

## `/solve-turnstile` examples

**Automatic route** (recommended: ~3.5 s when warm, ~9 s for the very first solve).

```bash
curl -X POST https://your-domain.example/solve-turnstile \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"sitekey": "0x4AAAAAAA...", "siteurl": "https://example.com/"}'
```

**Through the server IP or your proxy.**

```json
{"sitekey": "0x4AAAAAAA...", "siteurl": "https://example.com/", "proxy": "direct"}
{"sitekey": "0x4AAAAAAA...", "siteurl": "https://example.com/", "proxy": "socks5://user:pass@203.0.113.7:1080"}
```

Answer:

```json
{"solved": true, "token": "0.AbC...", "elapsed": 4.6, "pooled": false, "proxy": "http://warp:8080"}
```

- `proxy` shows the route that was actually used (credentials hidden; `"direct"` for the server IP).
- The token is single-use and valid for 300 s: submit it to the site right away, once.

Details: [`POST /solve-turnstile`](api/turnstile.md).

## Python

```python
import requests

API = "https://your-domain.example"
HEADERS = {"X-API-Key": "your-api-key"}

r = requests.post(f"{API}/fetch", json={"url": "https://example.com/"}, headers=HEADERS, timeout=180)
data = r.json()
print(data["status_code"], data["meta"]["via"], len(data["body"]))

t = requests.post(f"{API}/solve-turnstile",
                  json={"sitekey": "0x4AAAAAAA...", "siteurl": "https://example.com/"},
                  headers=HEADERS, timeout=150).json()
print(t["token"] if t.get("solved") else t)
```

Use a client timeout above `cf_wait + 90` s for `/fetch` (a browser solve can take that long in the worst case).

## Errors at a glance

| You get | Meaning | What to do |
| --- | --- | --- |
| HTTP 401 | Missing or wrong API key | Check the header |
| HTTP 422 | Invalid request (bad URL, internal host, bad selector, malformed proxy) | Read `detail` |
| `status_code: 502` | Target or proxy unreachable, page failed to load | Check the proxy / URL |
| `status_code: 503` / HTTP 503 | Browser tabs or a Turnstile queue are full | Retry after a few seconds |
| `status_code: 504` / HTTP 504 | Took longer than the deadline | Retry; raise `cf_wait` / `timeout` |
| `status_code: 409` | Proxy browsers are disabled on this server | Send no `proxy`, or `"direct"` |
