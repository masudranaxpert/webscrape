# Deploy

Production: **`https://your-domain.example`**, NextDeploy app **`<app-id>`** on account `<account>`. The app has no
Git connection: it deploys the workspace files pushed with `nd push`. This repository's `.nd/project.json` links it.

!!! danger "Not production"
    `<old-app-id>` / `old-test.example` is an old, idle test app. Do not deploy there by mistake.

## Steps

```bash
nd diff <app-id> -A <account>                 # what would change
nd push <app-id> -A <account> --deploy -y     # upload changed files, rebuild, restart
nd status <app-id> -A <account>               # both containers running, health 200
nd logs <app-id> -A <account> -s anti -n 50   # startup: "Cookie cache restored", "startup complete"
```

`nd push` never uploads `.env*` files; change settings with `nd env set <app-id> -A <account> KEY=VALUE` and
redeploy. Files listed in `.gitignore` (including `data/`) are not uploaded either.

## Verify after a deploy

Run the live check from inside the container (it uses the container's own `API_KEY`, nothing leaves the server):

```bash
cat docs/operations/bench.py | nd exec -A <account> -s anti -i -t 1800 <app-id> /app/venv/bin/python -u -
```

or a single request:

```bash
nd exec -A <account> -s anti <app-id> sh -c \
  'curl -s -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
   -d "{\"url\":\"https://example.com/\"}" http://127.0.0.1:8000/fetch | head -c 300' < /dev/null
```

## Containers

| Service | Image | Notes |
| --- | --- | --- |
| `anti` | built from `Dockerfile` (Ubuntu 24.04, Xvfb, openbox, tini, CloakBrowser binary and GeoIP DB baked in) | Runs as `ubuntu`; volume `anti-data` → `/app/data` |
| `warp` | `dublok/cloudflare-warp:latest` | HTTP proxy on `:8080` (WARP exit), `:8081` (direct exit); volume `warp-data` keeps the WARP registration |

Both use `restart: unless-stopped` and a self-healing healthcheck (see [Cold start](cold-start.md)).

## Local development

```bash
pip install -r requirements.txt pytest pytest-asyncio httpx
API_KEY=dev DEBUG=true COOKIE_CACHE_FILE= python -m pytest -q -o asyncio_mode=auto
API_KEY=dev ALLOW_PRIVATE_TARGETS=true uvicorn main:app --loop asyncio --reload
```

!!! warning "Shared license seat"
    The free CloakBrowser license allows **one** concurrent session across all machines. Running browser tests
    locally with the production key can take the seat from the server.
