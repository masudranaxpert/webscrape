# Anti-Bot Scraping API 🚀

High-performance async scraping API built with FastAPI. Combines TLS fingerprinting (`httpcloak`) with stealth browser solving (`cloakbrowser`) to bypass Cloudflare Turnstile with minimal CPU/RAM footprint.

## ✨ Features

* **Dual-Engine Architecture**: Fast HTTP TLS-fingerprinted requests with automated fallback to headless Chromium for Turnstile challenges.
* **Request Coalescing**: Groups concurrent requests per domain so only 1 browser tab solves the challenge while others wait and reuse cookies.
* **Smart Session Store**: In-memory LRU `CookieStore` for `cf_clearance` caching, allowlisting, and header size safety.
* **HTML Extraction**: Fast CSS selector parsing powered by `selectolax`.
* **Resource Safe**: Semaphore-bounded browser pool with zero socket/context leaks.

## 🚀 Getting Started

### Installation
```bash
uv pip install -r requirements.txt
```

### Running the API
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## 📖 API Usage

### `POST /fetch`

**Headers:**
```http
X-API-Key: anti_secret_key_123
Content-Type: application/json
```

**Payload:**
```json
{
  "url": "https://example.com/protected-page",
  "method": "GET",
  "selector": "h1.title",
  "selector_attr": "text"
}
```

**Optional Parameters:**
```json
{
  "url": "https://example.com/protected-page",
  "method": "GET",
  "proxy": "http://user:pass@proxy.ip:port",
  "force_browser": false,
  "preset": "chrome-latest-windows",
  "cookie_ttl": 3600,
  "timeout": 30
}
```

## ⚙️ Configuration (`.env`)

* `API_KEY`: Secret authentication key for API access (required for `/fetch` and `/cookies`)
* `PORT`: Server port (default: `8000`)
* `MAX_TABS`: Concurrency limit for worker browser tabs (default: `3`)
* `BROWSER_HEADLESS`: Run Chromium headless (default: `true`)
* `DEFAULT_COOKIE_TTL`: Cookie cache TTL in seconds (default: `3600`)
* `MAX_CACHED_DOMAINS`: Max cached domain count (default: `10`)

## 🔮 Next Phase / Roadmap

* **Proxy-Aware Clearance Caching**: Key cached `cf_clearance` tokens by `(domain, proxy)` pair instead of domain alone. Since Cloudflare binds clearance tokens to the solver's egress IP, this eliminates redundant 403 fallbacks when rotating distinct proxies.
* **Distributed Session Store**: Optional Redis backend for `CookieStore` to support horizontal scaling across multi-worker clusters.
