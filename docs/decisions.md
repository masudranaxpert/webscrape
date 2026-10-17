# Design decisions

Why things are the way they are. Read this before "simplifying" something.

### HTTP first, browser only on a challenge
The HTTP engine costs milliseconds and a few MB; a browser tab costs seconds and 60+ MB. Most requests never meet a
challenge, and those that do only need the browser once per host per clearance lifetime.

### After a solve, replay with the browser's exact identity
`cf_clearance` is bound to the IP, User-Agent and TLS fingerprint that earned it. The HTTP engine therefore reuses
the browser's UA **and** the `httpcloak` preset of the same Chrome major/OS (`chrome-146-windows`). `chrome-latest-*`
sends `sec-ch-ua` for a newer version than the UA claims, which Cloudflare re-challenges.

### One Chromium process per egress identity
CloakBrowser sets proxy, timezone, locale and WebRTC IP per process. Mixing identities in one process would leak the
wrong IP or fingerprint.

### WARP is the browser's default egress; HTTP stays direct unless WARP owns the clearance
See [Egress & WARP](egress.md). Direct HTTP avoids an extra hop and the sidecar's limits for the majority of sites;
"sticky" HTTP through WARP is required only where WARP's IP holds the clearance.

### No direct fallback when a proxy fails
Silently switching to direct would expose the server IP where a proxy was requested and change the IP under a
clearance. Proxy failures are reported (502) instead.

### Launch and close outside the pool lock
A dead proxy's GeoIP lookup takes 20 s+. Under the lock it froze every other identity (measured: 6 parallel jobs took
120 s and 4 failed). Outside it, the same load finishes in ~6 s.

### Turnstile tokens are never cached
They are single-use and expire after 300 s. What scales is minting: a warm widget page with `turnstile.reset()`,
a queue per sitekey, and a few pre-minted spares during bursts.

### Turnstile pre-minting is off by default
A pre-minted token nobody takes is a solve Cloudflare saw for nothing, on the same sitekey and IP. The warm page
already makes on-demand tokens fast (~2 s), so `TURNSTILE_PREFETCH=0` unless traffic really comes in repeated bursts.

### No fixed settle on clean pages
A Cloudflare interstitial is server-rendered and visible at load. Waiting a fixed 2.5 s on every browser fetch was
most of its latency; clean pages now get a 0.6 s second look, walls still get a settle.

### Same-sitekey minting is serialised
Parallel solves of one sitekey from one IP make Cloudflare escalate difficulty.

### In-memory bounded queues, not a durable queue
The API is synchronous; a request queued past its client's timeout is wasted. See
[Caching & queues](caching-and-queues.md#why-not-a-disk-backed-request-queue).

### The cookie cache is persisted, capped at 20 MB
Deploys and restarts used to drop every clearance and force fresh solves. Expiry is wall-clock time so stale jars
are skipped after downtime.

### `uvicorn --loop asyncio`, not uvloop
uvloop spawns the Playwright driver with `fork()`; with httpcloak's Go runtime reserving GBs of address space the
kernel refuses the fork (`Errno 12`). The default loop uses `vfork`.

### Headed Chromium in Xvfb
Headless builds are easier to fingerprint. Xvfb + openbox give a real window manager and screen size.

### gost limits raised and a self-healing healthcheck
The WARP image's gost proxy defaults to 10 connections / 10 req/s, which one browser page exhausts. Docker never
restarts unhealthy containers, so the healthcheck kills the wedged process itself.

### The SSRF guard blocks dot-less hostnames
Inside Docker Compose, `warp`, `anti` and similar names resolve to internal services. Real public sites always have
a dot.
