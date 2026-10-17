# POST /fetch

Fetch a URL. Authenticate with `X-API-Key: <key>` or `Authorization: Bearer <key>`.

The HTTP status of the API response is `200` whenever the pipeline ran; the **target's** status is in
`status_code`. Validation errors are `422`, a bad key `401`.

## Request fields

| Field | Default | HTTP engine | Browser | Meaning |
| --- | --- | :-: | :-: | --- |
| `url` | — | ✓ | ✓ | `http://` or `https://`, public host only (see [Security](../security.md)) |
| `method` | `GET` | ✓ | ✓ | Any method. The browser rewrites the navigation to it (POST/PUT/PATCH/DELETE) |
| `headers` | `{}` | ✓ | ✓ | Extra headers. Credentials (`Authorization`, `Cookie`) are dropped on cross-host redirects |
| `body` | `null` | ✓ | ✓ | Sent byte-for-byte (never trimmed or rewritten) |
| `force_browser` | `false` | — | ✓ | Skip the HTTP engine |
| `force_http` | `false` | ✓ | — | Never escalate; return whatever the origin answered (403/challenge included) |
| `follow_redirects` | `true` | ✓ | — | `false` = stop at the first response: `status_code` is the 3xx and `redirect_url` its `Location`; the next URL (often a download) is never requested. See [below](#get-a-redirect-without-following-it) |
| `cf_wait` | `25` (1–120) | — | ✓ | Seconds the solver may wait for the wall to clear / `wait_for_selector` |
| `wait_for_selector` | `null` | ✓ | ✓ | CSS selector that must exist. Missing from HTTP HTML → browser; validated up front |
| `click_selector` | `null` | — | ✓ | Click once ready (waits while `disabled`), then follow navigations/downloads. Implies browser |
| `page_load_state` | `complete` | — | ✓ | `complete` (load event) or `interactive` (DOMContentLoaded) |
| `proxy` | `null` | ✓ | ✓ | Proxy URL, or `"direct"`. Omitted → hybrid egress, see [Egress & WARP](../egress.md) |
| `preset` | `chrome-latest-windows` | ✓ | — | `httpcloak` fingerprint. Replaced automatically by the preset that earned cached cookies |
| `user_agent` | `null` | ✓ | — | UA override for the HTTP engine (the browser always uses its own real UA) |
| `http_version` | `null` | ✓ | — | `h1`, `h2` or `h3` |
| `timeout` | `30` (1–120) | ✓ | — | HTTP request timeout, seconds |
| `session_id` | `null` | ✓ | ✓ | Isolated cookie namespace (10-minute TTL, never shared with the host jar) |
| `cookie_ttl` | `86400` | ✓ | ✓ | How long harvested cookies are cached |
| `inject_cookies` | `{}` | ✓ | ✓ | Cookies added to this request (always kept by the filters) |
| `cookie_allowlist` | built-in list | ✓ | ✓ | Glob patterns of cookie names worth caching/sending |
| `cookie_limit` | `35` | ✓ | ✓ | Max cookies sent; Cloudflare cookies are kept first |
| `selector` | `null` | ✓ | ✓ | CSS selector to extract into `extracted`; validated up front (422 if invalid) |
| `selector_all` | `false` | ✓ | ✓ | All matches instead of the first |
| `selector_attr` | `text` | ✓ | ✓ | `text`, `html`, or any attribute name (`href`, `src`, …) |

`force_http` cannot be combined with `force_browser` or `click_selector`.

## Response fields

| Field | Meaning |
| --- | --- |
| `status_code` | Target's status. `502` = upstream/proxy/navigation failure, `503` = pool or queue busy, `504` = browser job deadline, `409` = proxied solving disabled |
| `headers`, `body`, `cookies`, `url` | Final response. `url` is the final URL after redirects (the file URL for downloads) |
| `redirect_chain` | Every hop `{url, status, location}`; `null` when there was no redirect |
| `redirect_url` | Where the response points but was not followed (`follow_redirects=false`, or a Refresh target) |
| `extracted` | Selector results |
| `logs` | Step-by-step trace: egress, cache, escalation reason, solver notes. Read this first when debugging |
| `meta.via` | `http` or `browser` |
| `meta.cache_hit` | Cached cookies were used |
| `meta.preset` | TLS preset used by the HTTP engine |
| `meta.body_truncated`, `meta.body_note` | Body skipped (binary, attachment, over `MAX_RESPONSE_MB`) or cut; status/headers/url stay valid |

## Examples

```json
{"url": "https://example.com/list", "selector": "a.item", "selector_attr": "href", "selector_all": true}
```

```json
{"url": "https://example.com/portal", "click_selector": "button#download", "cf_wait": 40}
```

```json
{"url": "https://api.example.com/x", "method": "POST", "headers": {"Content-Type": "application/json"},
 "body": "{\"q\": null}\n", "force_http": true}
```

## Get a redirect without following it

For a link that redirects to a file (a download gateway), you usually want the file's address, not the file:

```json
{"url": "https://example.com/get/42", "follow_redirects": false}
```

```json
{"status_code": 302, "redirect_url": "https://cdn.example.com/files/movie.mkv",
 "url": "https://example.com/get/42", "headers": {"location": "/files/movie.mkv", "...": "..."}}
```

How it stays download-free:

- `httpcloak` sends the request and stops at the first response; the `Location` is returned in `redirect_url`.
- If Cloudflare blocks that request, the browser clears the challenge on the site's **root page only**
  (`https://example.com/`) — `cf_clearance` covers the whole host — and `httpcloak` repeats the request with that
  clearance, again stopping at the first response. The browser never opens the target URL, so it can never follow the
  redirect or start the download.
- `meta.request_type` is `browser_clearance_then_http` when that happened.
- If the target path has its own Cloudflare rule (still challenged after the root is cleared), that challenge response
  is returned as-is.
- `click_selector` and `wait_for_selector` need the page open in the browser, which always follows redirects, so they
  cannot be combined with `follow_redirects: false` (422).

## Which engine answered, and why

The `logs` array always contains one of these escalation reasons:

| Log tag | Meaning |
| --- | --- |
| `[HTTP-SUCCESS]` | HTTP engine answered, no wall |
| `[HTTP-BLOCKED]` | 403/429/503 wall → browser |
| `[HTTP-INTERSTITIAL]` | Challenge page with another status → browser |
| `[HTTP-INCOMPLETE]` | `wait_for_selector` missing in server HTML → browser |
| `[HTTP-FAIL]` | Network/TLS error → browser (a proxy error fails fast instead, when the browser would use the same proxy) |
| `[COALESCE]` | Waited for another request's solve, then retried HTTP |
| `[FORCE]` | `force_browser` / `click_selector` |
