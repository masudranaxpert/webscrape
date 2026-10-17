"""POST /solve-turnstile — standalone Turnstile token minting (sitekey + domain -> token)."""
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.core.browser import BrowserPoolBusy, ProxySolvingDisabled, pool
from app.core.proxy import mask_proxy, resolve_proxy
from app.core.turnstile import solve as _solve

router = APIRouter()


class TurnstileSolveRequest(BaseModel):
    """Sitekey + the domain the sitekey is bound to; that is all a Turnstile solve needs."""

    sitekey: str = Field(
        pattern=r"^[A-Za-z0-9_-]{1,128}$",
        description="Turnstile sitekey (0x...). This value is embedded in the solve page, so the charset is strict.",
    )
    siteurl: str = Field(description="Full URL of the domain the sitekey is bound to, e.g. https://example.com/")
    timeout: int = Field(default=30, ge=5, le=120, description="Max seconds to wait for the token.")
    proxy: str | None = Field(
        default=None,
        description=(
            "Egress for the solve. Omitted: the default browser (WARP + the licensed build when WARP_PROXY is set), "
            "which Cloudflare challenges far less than a keyless browser on a datacenter IP (~5 s vs 10-27 s "
            "measured). 'direct' forces the server IP; a proxy URL uses that proxy. 409 when MAX_BROWSERS=0 and a "
            "proxy is needed."
        ),
    )

    @field_validator("siteurl")
    @classmethod
    def _http_only(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("siteurl must start with http:// or https://")
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(v)
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        if scheme == "http" and netloc.endswith(":80"):
            netloc = netloc[:-3]
        elif scheme == "https" and netloc.endswith(":443"):
            netloc = netloc[:-4]
        path = parsed.path or "/"
        return urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))


@router.post("/solve-turnstile")
async def solve_turnstile(req: TurnstileSolveRequest) -> dict:
    started = time.monotonic()
    try:
        # Same egress rule as /fetch's browser: omitted -> default browser, "direct"/"off" -> server IP
        proxy = resolve_proxy(req.proxy)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    try:
        result = await _solve(req.sitekey.strip(), req.siteurl, proxy=proxy, timeout=req.timeout)
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail={"solved": False, "error": "timeout", "elapsed": round(time.monotonic() - started, 2)},
        ) from None
    except BrowserPoolBusy as exc:
        raise HTTPException(status_code=503, detail={"solved": False, "error": "busy", "detail": str(exc)}) from None
    except ProxySolvingDisabled as exc:
        raise HTTPException(status_code=409, detail={"solved": False, "error": "proxy_disabled", "detail": str(exc)}) from None
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail={"solved": False, "error": "turnstile_error", "detail": str(exc)}) from None

    return {
        "solved": True,
        "token": result["token"],
        "elapsed": result["elapsed"],
        # true = minted ahead during a burst (still single-use and never handed to anyone else)
        "pooled": result["pooled"],
        "siteurl": req.siteurl,
        # Egress actually used, credentials masked ("direct" when none)
        "proxy": mask_proxy(proxy),
    }


@router.get("/solve-turnstile/health")
async def solve_health() -> dict:
    from app.core import turnstile

    return {"status": "ok", "browsers": pool.stats(), "turnstile": turnstile.stats()}
