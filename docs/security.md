# Security

## Authentication

Every API route except `/health`, `/`, `/docs` and static files requires `X-API-Key` or `Authorization: Bearer`.
Keys are compared as bytes in constant time; a non-ASCII header is a clean 401, never a 500. The app refuses to start
without `API_KEY` unless `DEBUG=true`.

## SSRF guard

The service fetches arbitrary URLs from inside the server's network, so targets are restricted
(`forbidden_target` in `app/core/proxy.py`):

- Only `http://` and `https://` (no `file://`, `chrome://`, …): checked in the request schema.
- Rejected hosts: `localhost`, `*.localhost`, `*.internal`, `*.local`, dot-less names (Docker service names such as
  `warp`), and any IP literal that is not globally routable (loopback, RFC 1918, link-local incl. `169.254.169.254`,
  CGNAT, IPv4-mapped IPv6).
- The HTTP engine re-checks every redirect hop and `Refresh` target and never follows one into an internal host.

Not covered: a public DNS name that resolves to a private address, and redirects inside the browser. Set
`ALLOW_PRIVATE_TARGETS=true` only on a development machine.

## Credentials and secrets

- Proxy credentials are re-encoded for Chromium/httpcloak and masked in every log line and error message
  (`mask_proxy`, `redact`). Proxy identities are stored as non-reversible hashes (`px-<sha256 prefix>`).
- `Authorization`, `Cookie` and `Proxy-Authorization` request headers are dropped when a redirect leaves the host.
- `/health` exposes counts only: no proxies, cookies, sitekeys or keys.
- The cookie cache file is written with mode `0600`; `data/` is in `.gitignore` and `.dockerignore`.
- `.env` is never committed and never uploaded by `nd push`.

## Playground

The web playground stores the API key the user types in the browser's `localStorage`, and renders all scraped
content escaped. Anyone who can run scripts on the page's origin could read that key, so do not host other content on
the API domain.

## Shared cookie cache

Cookies are cached per host and egress identity, **not per caller**. Two clients that log in to the same site
without `session_id` share that site's session cookies. Multi-tenant callers must use `session_id`.
