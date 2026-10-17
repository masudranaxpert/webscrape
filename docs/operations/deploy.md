# Deploy

Production runs on NextDeploy (`nd`) with Docker Compose. The app has no Git connection: it deploys the
workspace files pushed with `nd push`. Link this checkout once with `nd link <app-id>` (writes the git-ignored
`.nd/project.json`), then `<app-id>` can be omitted from the commands below.

## Steps

```bash
nd diff <app-id>                 # what would change
nd push <app-id> --deploy -y     # upload changed files, rebuild, restart
nd status <app-id>               # both containers running, health 200
nd logs <app-id> -s anti -n 50   # startup: "Cookie cache restored", "startup complete"
```

`nd push` never uploads `.env*` files; change settings with `nd env set <app-id> KEY=VALUE` and
redeploy. Files listed in `.gitignore` (including `data/`) are not uploaded either.

## Verify after a deploy

Run the live check from inside the container (it uses the container's own `API_KEY`, nothing leaves the server):

```bash
cat docs/operations/bench.py | nd exec -s anti -i -t 1800 <app-id> /app/venv/bin/python -u -
```

or a single request:

```bash
nd exec -s anti <app-id> sh -c \
  'curl -s -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
   -d "{\"url\":\"https://example.com/\"}" http://127.0.0.1:8000/fetch | head -c 300' < /dev/null
```

## Containers

| Service | Image | Notes |
| --- | --- | --- |
| `anti` | built from `Dockerfile` (Ubuntu 24.04, Xvfb, openbox, tini, CloakBrowser binary and GeoIP DB baked in) | Runs as `ubuntu`; volumes `anti-data` → `/app/data` (cookie cache), `cloak-cache` → `~/.cloakbrowser` (browser builds, GeoIP DB) |
| `warp` | `dublok/cloudflare-warp:latest` | HTTP proxy on `:8080` (WARP exit), `:8081` (direct exit); volume `warp-data` keeps the WARP registration |

Both use `restart: unless-stopped` and a self-healing healthcheck (see [Cold start](cold-start.md)).

## Local development

```bash
pip install -r requirements.txt pytest pytest-asyncio httpx
API_KEY=dev DEBUG=true COOKIE_CACHE_FILE= CLOAKBROWSER_LICENSE_KEY= python -m pytest -q -o asyncio_mode=auto
API_KEY=dev ALLOW_PRIVATE_TARGETS=true uvicorn main:app --loop asyncio --reload
```

!!! warning "Shared license seat"
    The free CloakBrowser license allows **one** concurrent session across all machines. Running browser tests
    locally with the production key can take the seat from the server.
