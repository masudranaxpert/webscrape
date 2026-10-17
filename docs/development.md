# Development

## Tests

```bash
pip install -r requirements.txt pytest pytest-asyncio httpx
API_KEY=dev DEBUG=true COOKIE_CACHE_FILE= CLOAKBROWSER_LICENSE_KEY= python -m pytest -q -o asyncio_mode=auto
```

Most tests need neither network nor a browser: they replace the browser launch, the HTTP engine or the widget page
with fakes. A few tests launch a real keyless Chromium; keep `CLOAKBROWSER_LICENSE_KEY` empty so they never take the production license seat.

| File | Covers |
| --- | --- |
| `test_audit_fixes.py` | SSRF guard, selector/body validation, proxy-error markers, UA-matched presets, Turnstile minter (warm page, distinct tokens, pre-minting, queue limit, fatal errors), hybrid egress, cookie-cache persistence |
| `test_proxy.py` | Proxy parsing, routing, pool eviction, slow/failed launches never blocking other identities |
| `test_pool_identity.py`, `test_pool_deadlock.py` | Pool slots, poisoning, exit-IP retirement, deadlines |
| `test_fetcher_native.py` | HTTP engine streaming, binary/oversized skipping, redirects, native-call lifetime |
| `test_flow.py`, `test_browser_navigation.py` | Browser tab pipeline |
| `test_core.py` | Cookie store filters and LRU |

## Conventions

- Comments explain *why*, not what; keep them short and next to the code they justify.
- Any change to the pool must keep the rule **no network call and no browser launch/close while holding the pool
  lock**; there are regression tests for it.
- Never cache or log a Turnstile token, a proxy password or a cookie value.
- Add a row to [Configuration](configuration.md) for every new environment variable, and to
  [Design decisions](decisions.md) for every non-obvious trade-off.

## Docs site

This site is built with [Zensical](https://zensical.org) from `docs/` and `zensical.toml`, and published to GitHub
Pages by `.github/workflows/docs.yml` on every push to `main`.

```bash
pip install zensical
zensical serve          # http://localhost:8000 with live reload
zensical build --clean  # static site in site/
```

GitHub repository settings → Pages → Source must be **GitHub Actions**.
