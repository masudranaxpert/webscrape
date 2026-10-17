# Caching & queues

## What is cached, and what is not

| Thing | Cached? | Where | Why |
| --- | --- | --- | --- |
| Cookies that passed Cloudflare (`cf_clearance`, session cookies) + UA + TLS preset | Yes | Memory, LRU per host × egress identity; written to disk | Replaying them over HTTP turns a 10 s browser solve into a 0.1 s request |
| Post-challenge redirect host (e.g. `site.com` → `www.site.com`) | Yes, 1 h TTL | Memory | So the HTTP replay hits the host the clearance belongs to |
| Turnstile tokens | **No** | — | Single-use and expire in 300 s; a reused token is rejected by the site's backend |
| Page bodies | No | — | Callers want fresh content |
| In-flight requests | Only while waiting | Memory | See queues below |

### Cookie cache

- Key: `[identity@]host[#session_id]`. Host jars default to `cookie_ttl` (24 h; production uses 6 h), session jars are
  capped at `DEFAULT_SESSION_TTL` (10 min) and never write to the shared host jar.
- Capacity: `MAX_CACHED_DOMAINS` host jars and `MAX_CACHED_SESSIONS` session jars, least recently used evicted first.
- Filters: only cookies matching the allowlist (Cloudflare, session/auth/CSRF names, …) are stored; at most
  `cookie_limit` cookies and ~3.5 KB of `Cookie` header are sent, Cloudflare cookies first.
- A subdomain without its own jar inherits only the root domain's `cf_clearance` (never its other cookies).
- When the exit IP of a proxied identity changes, every jar of that identity is dropped (its clearances are dead).

### Persistence (survives restarts and deploys)

`app/core/cache_persist.py` writes the cookie cache to `COOKIE_CACHE_FILE` (`/app/data/cookie_cache.json`, a Docker
volume) every 30 s when it changed and once on shutdown, and restores it on start.

- Atomic write (temp file + rename), mode `0600` (it holds session cookies).
- Capped at `COOKIE_CACHE_MAX_MB` (20 MB): the least recently used jars are dropped first. At ~1–2 KB per jar this is
  far more than the in-memory capacity, so the cap only protects the disk.
- Expiry is wall-clock time, so after days offline every stale jar is simply skipped on load.
- A corrupt or oversized file is ignored (logged) and the service starts with an empty cache.

## Queues and backpressure

The API is synchronous: the client keeps its HTTP connection open until the answer is ready. So queued work lives
in memory, bounded, and **overload is answered quickly instead of piling up**.

| Queue | Bound | When full |
| --- | --- | --- |
| Browser tabs | `MAX_TABS` open at once; a request waits at most `BROWSER_QUEUE_TIMEOUT_SEC` (and never past its job deadline) | `status_code: 503` |
| Proxied browser processes | `MAX_BROWSERS`; idle ones are evicted LRU for a new identity | waits, then 503 |
| Request bodies waiting for a proxied slot | `PROXY_QUEUE_BYTE_LIMIT` (16 MB) in total | 503 |
| Same-host browser solves | One solve per host × identity; other requests wait for it and replay its cookies (coalescing) | — |
| Turnstile, per sitekey | `TURNSTILE_MAX_QUEUE` waiters | HTTP 503 |
| HTTP engine | None needed: async, ~6,500 req/min measured, a few MB | — |

### Why not a disk-backed request queue?

A durable queue (disk, Redis) is the right tool for **asynchronous jobs**: submit, get a job id, poll or receive a
webhook later. For a synchronous API it does not help:

- If the server stops, the client's open connection is gone; a request replayed from disk later has no one to answer.
- Waiting longer than a client's timeout is wasted work: the browser would solve a page nobody receives.
- Memory per waiting request is tiny (the request body, already capped), so RAM is not what limits the queue — tabs
  and CPU are.

The industry-standard design for this shape of API is what is implemented: bounded in-memory queues, fair waiting
(oldest first, identities cannot starve each other), request coalescing, deadlines on every wait, and a fast `503`
when saturated so the client can retry. If you ever need fire-and-forget scraping of thousands of URLs, add an
async job endpoint backed by a durable queue rather than making `/fetch` wait longer.
