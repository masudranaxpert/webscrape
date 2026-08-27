# Anti-Bot Scraping API 🚀

A high-performance, asynchronous web scraping API designed to seamlessly bypass Cloudflare Turnstile and other advanced bot protections. It features a dual-engine architecture that intelligently routes traffic to maximize speed while minimizing server resource usage (RAM/CPU).

## ✨ Key Features

* **Dual-Engine Architecture**: 
  * **Fast-Path**: Uses `httpcloak` for high-speed, low-footprint HTTP requests with perfect TLS fingerprinting.
  * **Slow-Path**: Automatically falls back to a persistent, stealthy Chromium browser (`cloakbrowser`) to solve complex JS challenges and Turnstile captchas.
* **Request Coalescing (Thundering Herd Protection)**: Can handle 1000s of concurrent requests to the same domain. If a challenge is detected, exactly *one* browser instance is launched to solve it, while all other requests wait in memory and instantly reuse the harvested cookies upon success. Zero memory leaks!
* **Automated Session Management**: Built-in Bounded LRU `CookieStore` automatically caches, injects, and rotates `cf_clearance` and session cookies.
* **Data Extraction**: Built-in CSS selector support via `selectolax` for blazingly fast HTML parsing.
* **Fully Asynchronous**: Built on `FastAPI` and `asyncio`, designed for massive concurrency.

## 🚀 Getting Started

### Prerequisites
* Python 3.10+
* [uv](https://github.com/astral-sh/uv) (Recommended for dependency management)

### Installation

1. Clone the repository and install dependencies:
```bash
uv pip install -r requirements.txt
```

2. Playwright browsers will be managed automatically via `cloakbrowser`.

### Running the API

Start the FastAPI server:
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 📖 API Usage

### `POST /fetch`

Execute a stealth scraping request.

**Example Request:**
```json
{
  "url": "https://example.com/protected-page",
  "method": "GET",
  "selector": "h1.title",
  "selector_attr": "text"
}
```

**Advanced Payload:**
```json
{
  "url": "https://example.com/protected-page",
  "method": "GET",
  "force_browser": false,
  "preset": "auto", 
  "timeout": 30,
  "cookie_ttl": 1800
}
```

## 🛠️ Testing

A load-testing script (`m.py`) is included to verify the Request Coalescing behavior. It fires 100 concurrent requests to a Cloudflare-protected site and ensures only 1 browser is used.

```bash
python m.py
```

To run the internal system self-checks:
```bash
python test.py
```

## ⚙️ Configuration

Environment variables can be set via `.env`:
* `MAX_TABS`: Maximum number of concurrent browser tabs allowed (Default: 3).
* `MAX_CACHED_DOMAINS`: Maximum number of domains to cache cookies for (Default: 10).
* `BROWSER_HEADLESS`: Run Chromium in headless mode (Default: true).

---
*Built for efficiency. Scrape responsibly.*
