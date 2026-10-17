"""Pydantic schemas with OpenAPI examples for request and response validation."""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.proxy import DIRECT_SENTINELS, forbidden_target, normalize_proxy


class RequestMeta(BaseModel):
    """Execution diagnostics and routing metadata."""

    via: str = Field(
        ...,
        description="Active engine used to resolve the request ('http' or 'browser').",
        examples=["http"],
    )
    request_type: str = Field(
        ...,
        description="Execution mode ('http_request' or 'browser').",
        examples=["http_request"],
    )
    preset: str | None = Field(
        default=None,
        description="Wire fingerprint profile preset applied.",
        examples=["chrome-latest-windows"],
    )
    cf_bypass_attempted: bool = Field(
        default=False,
        description="True if challenge solver was triggered.",
        examples=[False],
    )
    cache_hit: bool = Field(
        default=False,
        description="True if request utilized pre-cached domain session cookies.",
        examples=[True],
    )
    cookies_used: int = Field(
        default=0,
        description="Number of session cookies attached to the outbound request.",
        examples=[2],
    )
    user_agent: str | None = Field(
        default=None,
        description="Effective User-Agent header string used for the request.",
        examples=["Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/134.0.0.0 Safari/537.36"],
    )
    body_truncated: bool = Field(
        default=False,
        description=(
            "True when the HTTP engine did not return the full body: video/audio/image/archive/attachment responses "
            "and bodies over MAX_RESPONSE_MB are not read at all, unknown-length bodies are cut at the limit. "
            "`status_code`, `headers` and `url` (the final redirect target) are still valid."
        ),
        examples=[False],
    )
    body_note: str | None = Field(
        default=None,
        description="Why the body was skipped or cut (set together with `body_truncated`).",
        examples=[None],
    )


class RedirectHop(BaseModel):
    """One navigation step: where the engine went, what the server answered, and where it pointed next."""

    url: str = Field(..., description="Address requested at this step.", examples=["https://example.com/go"])
    status: int = Field(..., description="HTTP status the server answered with.", examples=[302])
    location: str | None = Field(
        default=None,
        description="Absolute `Location` target when this step was a redirect, otherwise null.",
        examples=["https://example.com/next"],
    )


class FetchRequest(BaseModel):
    """Payload schema for POST /fetch."""

    url: str = Field(
        ...,
        description="Target destination URL.",
        examples=["https://example.com/protected-page"],
    )
    method: str = Field(
        default="GET",
        description="HTTP method.",
        examples=["GET"],
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Custom request headers to merge into fingerprint order.",
        examples=[{"Accept-Language": "en-US,en;q=0.9"}],
    )
    body: str | None = Field(
        default=None,
        description="Request body payload for POST/PUT/PATCH requests.",
        examples=[None],
    )

    # Browser execution settings
    force_browser: bool = Field(
        default=False,
        description="Skip fast path and execute directly via headless browser.",
        examples=[False],
    )
    force_http: bool = Field(
        default=False,
        description=(
            "HTTP only: never escalate to the headless browser. The raw HTTP response is returned as-is, "
            "even when it is a 403/429/503 or a challenge page. Cannot be combined with `force_browser`."
        ),
        examples=[False],
    )
    follow_redirects: bool = Field(
        default=True,
        description=(
            "false = stop at the target's first response and do not follow redirects: `status_code` is the 3xx, "
            "`redirect_url` holds the absolute `Location` (or a Refresh target), and the download/next page is never "
            "requested. If Cloudflare challenges the URL, the browser only clears the challenge on the site's root "
            "page (it never opens the target URL), then the HTTP engine reads the target's first response with that "
            "clearance. Cannot be combined with `click_selector` or `wait_for_selector`."
        ),
        examples=[True],
    )
    cf_wait: float = Field(
        default=25.0,
        ge=1.0,
        le=120.0,
        description="Maximum seconds to wait for Turnstile resolution and for `wait_for_selector` to appear.",
        examples=[25.0],
    )
    wait_for_selector: str | None = Field(
        default=None,
        description=(
            "CSS selector that must exist in the live DOM before the page is returned. Forces the browser engine "
            "if the fast-path HTML lacks it; use for content injected after an embedded Turnstile is solved. "
            "If it never appears within `cf_wait`, the page is returned as it is and `logs` says the selector timed out."
        ),
        examples=[".server-btn"],
    )
    click_selector: str | None = Field(
        default=None,
        description="CSS selector to click once the page/Turnstile is ready before capturing DOM. Implies force_browser.",
        examples=["button#download"],
    )
    page_load_state: str = Field(
        default="complete",
        description="Browser page load strategy ('complete' or 'interactive').",
        examples=["complete"],
    )

    # Network & fingerprint settings
    proxy: str | None = Field(
        default=None,
        description=(
            "Egress proxy: 'http://user:pass@host:port', 'https://…' or 'socks5://user:pass@host:port' (socks5h accepted). "
            "A proxy (or 'direct') pins both the HTTP and the browser engine to it. Omitted: the HTTP engine goes "
            "direct and only a browser escalation uses the server's WARP_PROXY; once that browser earned clearance "
            "for the host, its HTTP requests follow through WARP. Proxied solving can be disabled server-side "
            "(MAX_BROWSERS=0 → 409). "
            "Invalid proxies are rejected (422) instead of silently connecting direct."
        ),
        examples=[None],
    )
    preset: str = Field(
        default="chrome-latest-windows",
        description="Fingerprint profile ('chrome-latest-windows', 'chrome-latest-linux', 'firefox-latest').",
        examples=["chrome-latest-windows"],
    )
    user_agent: str | None = Field(
        default=None,
        description="Custom User-Agent header override.",
        examples=[None],
    )
    http_version: str | None = Field(
        default=None,
        description="HTTP protocol override ('h1', 'h2', 'h3').",
        examples=[None],
    )
    timeout: int = Field(
        default=30,
        ge=1,
        le=120,
        description="Request timeout in seconds.",
        examples=[30],
    )

    # Cookie settings
    session_id: str | None = Field(
        default=None,
        description="Isolated session identifier to prevent cookie collisions between concurrent scraping flows.",
        examples=["task-dl-1"],
    )
    cookie_ttl: int = Field(
        default=86400,
        description="Cache duration in seconds for harvested cookies (Default: 86400s / 24 hours).",
        examples=[86400],
    )
    inject_cookies: dict[str, str] = Field(
        default_factory=dict,
        description="Custom cookies to inject before navigation.",
        examples=[{"session_token": "abc123xyz"}],
    )
    cookie_allowlist: list[str] | None = Field(
        default=None,
        description="Allowed cookie names or glob/regex patterns (e.g. ['cf_*', '__cf*', 'PHPSESSID', 'custom_token*']). If None, defaults to essential bypass & session patterns.",
        examples=[["cf_*", "__cf*", "PHPSESSID", "my_site_pref_*"]],
    )
    cookie_limit: int = Field(
        default=35,
        description="Maximum number of cookies allowed in request header before truncating oldest to prevent HTTP 400 buffer overflow.",
        examples=[35],
    )

    # HTML parsing settings
    selector: str | None = Field(
        default=None,
        description="CSS selector to extract from HTML.",
        examples=["h1.title"],
    )
    selector_all: bool = Field(
        default=False,
        description="Return array of all matches if true, or first match if false.",
        examples=[False],
    )
    selector_attr: str = Field(
        default="text",
        description="Attribute to extract ('text', 'html', 'href', 'src', etc.).",
        examples=["text"],
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "url": "https://example.com/items",
                    "method": "GET",
                    "force_browser": False,
                    "preset": "chrome-latest-windows",
                    "selector": "a.download-link",
                    "selector_attr": "href",
                    "selector_all": True,
                    "cookie_ttl": 86400,
                }
            ]
        }
    }

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("URL must be a string")
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https:// (file:// and internal protocols are forbidden)")
        from urllib.parse import urlparse
        parsed = urlparse(v)
        if not parsed.hostname:
            raise ValueError("URL must contain a valid hostname")
        reason = forbidden_target(parsed.hostname)
        if reason:
            raise ValueError(reason)
        return v

    @field_validator("proxy", "user_agent", "http_version", "selector", "wait_for_selector", mode="before")
    @classmethod
    def clean_dummy_strings(cls, v: Any) -> Any:
        if isinstance(v, str):
            val = v.strip()
            if val.lower() in ("string", "none", "null", ""):
                return None
            return val
        return v

    @field_validator("selector", "wait_for_selector")
    @classmethod
    def validate_css(cls, v: str | None) -> str | None:
        # Reject a broken selector up front (422) instead of failing after the fetch or browser job already ran
        if v is not None:
            from selectolax.lexbor import LexborHTMLParser

            try:
                LexborHTMLParser("<p></p>").css_first(v)
            except Exception as exc:
                raise ValueError(f"Invalid CSS selector {v!r}: {exc}") from None
        return v

    @field_validator("proxy")
    @classmethod
    def validate_proxy(cls, v: str | None) -> str | None:
        if v is None or v.lower() in DIRECT_SENTINELS:
            return v.lower() if v else v
        return normalize_proxy(v)

    @field_validator("method", mode="before")
    @classmethod
    def clean_method(cls, v: Any) -> str:
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
        return "GET"

    @model_validator(mode="after")
    def check_engine_flags(self) -> "FetchRequest":
        if self.force_http and (self.force_browser or self.click_selector):
            raise ValueError("force_http cannot be combined with force_browser or click_selector")
        if not self.follow_redirects and (self.click_selector or self.wait_for_selector):
            raise ValueError(
                "follow_redirects=false cannot be combined with click_selector or wait_for_selector: both need the "
                "browser to open the page, and the browser always follows redirects"
            )
        return self


class FetchResponse(BaseModel):
    """Response payload schema for POST /fetch."""

    status_code: int = Field(
        ...,
        description="HTTP status code from target server.",
        examples=[200],
    )
    headers: dict[str, str] = Field(
        ...,
        description="Response headers returned by target host.",
        examples=[{"content-type": "text/html; charset=UTF-8"}],
    )
    body: str = Field(
        ...,
        description="Raw response body content.",
        examples=["<!DOCTYPE html><html><head><title>Example Page</title></head><body><h1>Content</h1></body></html>"],
    )
    cookies: dict[str, str] = Field(
        ...,
        description="Session and clearance cookies harvested from the request.",
        examples=[{"cf_clearance": "abc.123.xyz", "session_id": "98765"}],
    )
    url: str = Field(
        ...,
        description=(
            "Final resolved URL after redirects. For a download-gateway link this is the file's address: when the page "
            "turns into a file download the file itself is never fetched, only this URL is returned."
        ),
        examples=["https://example.com/items"],
    )
    redirect_url: str | None = Field(
        default=None,
        description=(
            "Where the response points but was not followed: the absolute `Location` of a 3xx answered with "
            "`follow_redirects=false`, or a Refresh target. Null otherwise."
        ),
        examples=[None],
    )
    redirect_chain: list[RedirectHop] | None = Field(
        default=None,
        description=(
            "Every step of the redirect chain (HTTP engine) or of the browser's navigation including after "
            "`click_selector` (browser engine): url, status and `location` per hop. Null when there was no redirect."
        ),
    )
    protocol: str | None = Field(
        default=None,
        description="Negotiated protocol version (e.g. 'h2', 'h1.1').",
        examples=["h2"],
    )
    extracted: list[str] | None = Field(
        default=None,
        description="Results parsed by CSS selector if specified in request.",
        examples=[["https://example.com/download/1.zip", "https://example.com/download/2.zip"]],
    )
    logs: list[str] = Field(
        default_factory=list,
        description="Step-by-step pipeline execution trace log.",
        examples=[
            [
                "[INIT] Resolving destination host: example.com",
                "[CACHE] Checked domain cookies -> Miss",
                "[HTTP] Direct TLS fingerprint request -> 200 OK (312ms)",
                "[DONE] Request resolved via HTTP Engine",
            ]
        ],
    )
    meta: RequestMeta = Field(
        ...,
        description="Execution diagnostics and engine routing metadata.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "status_code": 200,
                    "headers": {
                        "content-type": "text/html; charset=UTF-8",
                        "server": "cloudflare",
                    },
                    "body": "<!DOCTYPE html><html>...</html>",
                    "cookies": {
                        "cf_clearance": "abc123clearance",
                        "session": "user_tok_48291",
                    },
                    "url": "https://example.com/items",
                    "protocol": "h2",
                    "extracted": [
                        "https://example.com/download/file1.zip",
                        "https://example.com/download/file2.zip",
                    ],
                    "meta": {
                        "via": "http",
                        "request_type": "http_request",
                        "preset": "chrome-latest-windows",
                        "cf_bypass_attempted": False,
                        "cache_hit": True,
                        "cookies_used": 2,
                    },
                }
            ]
        }
    }
