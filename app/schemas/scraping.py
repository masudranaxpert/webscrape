"""Pydantic schemas with OpenAPI examples for request and response validation."""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field, field_validator


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
    cf_wait: float = Field(
        default=10.0,
        description="Maximum seconds to wait for Turnstile challenge resolution.",
        examples=[10.0],
    )
    page_load_state: str = Field(
        default="complete",
        description="Browser page load strategy ('complete' or 'interactive').",
        examples=["complete"],
    )

    # Network & fingerprint settings
    proxy: str | None = Field(
        default=None,
        description="Proxy URL (e.g. 'http://proxy:8080' or 'socks5://127.0.0.1:1080').",
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
        description="Request timeout in seconds.",
        examples=[30],
    )

    # Cookie settings
    cookie_ttl: int = Field(
        default=3600,
        description="Cache duration in seconds for harvested cookies.",
        examples=[3600],
    )
    inject_cookies: dict[str, str] = Field(
        default_factory=dict,
        description="Custom cookies to inject before navigation.",
        examples=[{"session_token": "abc123xyz"}],
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
                    "cookie_ttl": 3600,
                }
            ]
        }
    }

    @field_validator("body", "proxy", "user_agent", "http_version", "selector", mode="before")
    @classmethod
    def clean_dummy_strings(cls, v: Any) -> Any:
        if isinstance(v, str):
            val = v.strip()
            if val.lower() in ("string", "none", "null", ""):
                return None
            return val
        return v

    @field_validator("method", mode="before")
    @classmethod
    def clean_method(cls, v: Any) -> str:
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
        return "GET"


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
        description="Final resolved URL after redirects.",
        examples=["https://example.com/items"],
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
