# Troubleshooting

Start with the `logs` array of the `/fetch` response, then `GET /health`, then the container logs
(`nd logs <app-id> -A <account> -s anti -n 200`).

## WARP proxy stops answering

**Symptom:** browser escalations fail with `Proxy http://warp:8080 failed`, HTTP through WARP reports
`failed to read CONNECT response: i/o timeout`, `/health` shows `"warp": "unknown"`, yet `nd status` says the warp
container is healthy.

**Cause:** the image's gost proxy limits each listener to `PROXY_MAX_CONN` concurrent connections and `PROXY_MAX_RPS`
requests per second, both **10** by default. One Chromium page load exceeds that, the limiter slots stay taken, and
`:8080` stops answering while the WARP tunnel itself (`127.0.0.1:40000`) is fine.

**Fix (in place):** `docker-compose.yml` sets both to 1024/1000, and the healthcheck restarts the container when
`:8080` stops answering for 90 s. To confirm by hand:

```bash
nd exec -A <account> -s warp <app-id> sh -c \
  'curl -s -o /dev/null -w "%{http_code}\n" --max-time 10 --socks5-hostname 127.0.0.1:40000 https://www.cloudflare.com/cdn-cgi/trace;
   curl -s -o /dev/null -w "%{http_code}\n" --max-time 10 -x http://127.0.0.1:8080 https://www.cloudflare.com/cdn-cgi/trace' < /dev/null
```

`200` then `000` = gost is wedged (restart the warp service); `000` on both = the tunnel is down.

## Every request goes to the browser

- Check `meta.preset` after a solve: it must be a versioned preset like `chrome-146-windows`. A `chrome-latest-*`
  preset with a cached browser UA is the classic re-challenge loop (UA says 146, `sec-ch-ua` says 155).
- Check the `[PROXY]` log line: a clearance earned through WARP only works through WARP.

## `CloakBrowser Pro: license key is invalid or expired`

The key in `CLOAKBROWSER_LICENSE_KEY` was rejected and the browser fell back to the keyless build. On the free plan
this also appears when the single seat is held elsewhere (for example a local run with the same key), so first stop
other users of the key. If it persists, fix or remove the key and redeploy. The app stops re-validating a rejected key for 10 minutes, so the warning appears at most every 10 minutes.

## `status_code: 503`

The browser pool or a Turnstile queue is saturated. Check `browser_pool.active_tabs` / `queued` in `/health`. Retry
with backoff, or raise `MAX_TABS` after reading [Benchmark](benchmark.md).

## `status_code: 504`

A browser job exceeded `cf_wait + BROWSER_JOB_OVERHEAD_SEC`, or a page call stalled. The process was marked for
replacement automatically.

## `422` on a URL that looks fine

The SSRF guard rejects loopback, private, link-local and dot-less internal hosts (`localhost`, `warp`, `10.x`, …).
Set `ALLOW_PRIVATE_TARGETS=true` only for local development.

## Memory keeps growing

Browser processes are recycled after `BROWSER_MAX_LIFETIME_SEC` (idle) and the licensed one after
`BROWSER_LICENSED_LIFETIME_SEC`. Check `browser_pool.browsers` in `/health`; it should never exceed
`1 + MAX_BROWSERS`. uvicorn must run with `--loop asyncio` (uvloop forks and fails with Errno 12 on browser launch).
