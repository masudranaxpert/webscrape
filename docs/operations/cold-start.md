# Cold start & self-healing

What happens when the server is switched off for days and turned back on, or when something wedges at runtime.

## After days offline

| Step | What happens | Safe? |
| --- | --- | --- |
| Host boots | Docker starts the containers because both use `restart: unless-stopped` — **provided the Docker daemon is enabled at boot on the VPS** (`systemctl is-enabled docker`). Check this once on the host | ✓ if enabled |
| `warp` starts | Reuses the registration kept in the `warp-data` volume, reconnects the tunnel, starts gost on `:8080` | ✓ |
| `anti` starts | Does not wait for WARP (`service_started`): HTTP traffic works immediately; browser escalations through WARP fail with 502 for the few seconds WARP needs | ✓ |
| Xvfb | Stale `/tmp/.X99-lock` from an unclean shutdown is removed by the entrypoint before Xvfb starts | ✓ |
| Cookie cache | Restored from `/app/data/cookie_cache.json`; jars whose wall-clock expiry passed while offline are skipped. After days, expect an empty cache and fresh solves | ✓ |
| Browser binary / GeoIP DB | Baked into the image; no download at start. The GeoIP DB refreshes itself in the background after 30 days, never blocking a launch | ✓ |
| License | Validated online at the first launch; an invalid key falls back to the keyless build and is not retried for 10 min | ✓ |
| Exit IPs | WARP may come back with a new exit IP. Old clearances then fail once and are re-solved; the exit-IP probe also drops them | ✓ |

## Self-healing at runtime

Docker does **not** restart a container just because it is unhealthy. Both healthchecks therefore act themselves:

| Container | Check | After | Action |
| --- | --- | --- | --- |
| `warp` | `curl` through `:8080` to Cloudflare's trace must report `warp=on` | 6 failures in a row (90 s) | kills the `:8080` gost (the entrypoint's foreground process) → container exits → restart policy starts a fresh one |
| `anti` | `curl http://127.0.0.1:8000/health` within 10 s | 4 failures in a row (2 min) | sends SIGTERM to uvicorn (graceful: cookie cache saved) → tini exits → restart |

Inside the app:

| Failure | Handling |
| --- | --- |
| A page call stalls > `BROWSER_CALL_TIMEOUT_SEC` | Process poisoned, replaced when its tabs finish |
| A browser job exceeds `cf_wait + BROWSER_JOB_OVERHEAD_SEC` | Cancelled (504), process poisoned |
| A launch hangs | Abandoned after 90 s, its reservation freed |
| `browser.close()` hangs | Driver killed after 5 s; Chromium exits with its pipe |
| Proxy exit IP changes | Process retired, that identity's clearances dropped |
| Disconnected browser | Detected on the next acquire and replaced |

## Checking a cold start by hand

```bash
nd status <app-id> -A <account>                       # both running, warp (healthy)
nd logs <app-id> -A <account> -s anti -n 30            # "Cookie cache restored: N live entries"
nd exec -A <account> -s anti <app-id> sh -c 'curl -s http://127.0.0.1:8000/health' < /dev/null
```
