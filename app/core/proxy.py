"""Proxy URL validation, credential masking, identity keys and default routing.

Every egress identity (direct or a specific proxy) gets its own browser process and cookie namespace, because
cf_clearance is bound to the IP that earned it and CloakBrowser's timezone/locale/WebRTC flags are process-wide.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from urllib.parse import quote, unquote, urlsplit

from app.core.config import ALLOW_PRIVATE_TARGETS, WARP_PROXY

SUPPORTED_SCHEMES = ("http", "https", "socks5", "socks5h")

# Request values that explicitly ask for a direct connection
DIRECT_SENTINELS = ("direct", "off")

DIRECT_KEY = "direct"


def normalize_proxy(proxy: str | None) -> str | None:
    """Validate a proxy URL and return it in canonical form; raise ValueError when unusable.

    Fails closed: a malformed proxy must never silently fall back to a direct connection that leaks the server IP.
    """
    if proxy is None:
        return None
    raw = proxy.strip()
    if not raw:
        return None

    if "://" not in raw:
        raise ValueError(f"Proxy URL must start with a scheme ({', '.join(s + '://' for s in SUPPORTED_SCHEMES)})")
    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    if scheme not in SUPPORTED_SCHEMES:
        raise ValueError(f"Unsupported proxy scheme '{scheme or '?'}' (use one of: {', '.join(SUPPORTED_SCHEMES)})")
    if not parts.hostname:
        raise ValueError("Proxy URL is missing a host")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"Invalid proxy port: {exc}") from None
    if not port:
        raise ValueError("Proxy URL is missing a port")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("Proxy URL must not contain a path, query or fragment")
    if (parts.username is None) != (parts.password is None):
        raise ValueError("Proxy credentials must include both username and password")

    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    auth = ""
    if parts.username is not None:
        # Re-encode so passwords with '@', ':' or '=' survive both httpcloak and Chromium's --proxy-server parser
        auth = f"{quote(unquote(parts.username), safe='')}:{quote(unquote(parts.password or ''), safe='')}@"
    return f"{scheme}://{auth}{host}:{port}"


def mask_proxy(proxy: str | None) -> str:
    """Log-safe proxy representation: credentials are never written to logs or responses."""
    if not proxy:
        return "direct"
    try:
        parts = urlsplit(proxy)
        auth = "***@" if parts.username else ""
        return f"{parts.scheme}://{auth}{parts.hostname}:{parts.port}"
    except ValueError:
        return "invalid-proxy"


_CREDENTIALS_RE = re.compile(r"([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)


def redact(text: str) -> str:
    """Strip `user:pass@` from any URL inside free text (exception messages echo Chrome's --proxy-server flag)."""
    return _CREDENTIALS_RE.sub(r"\1***@", text)


def proxy_key(proxy: str | None) -> str:
    """Stable, non-reversible identity id for a proxy (used for browser workers and cookie namespaces)."""
    if not proxy:
        return DIRECT_KEY
    return "px-" + hashlib.sha256(proxy.encode("utf-8")).hexdigest()[:12]


def browser_proxy(proxy: str | None) -> str | None:
    """Proxy URL as Chromium expects it: socks5h is not a Chromium scheme, and Chromium already resolves
    hostnames through SOCKS5 proxies, so socks5h maps to socks5 without leaking DNS."""
    if proxy and proxy.startswith("socks5h://"):
        return "socks5://" + proxy[len("socks5h://"):]
    return proxy


_DEFAULT_EGRESS = normalize_proxy(WARP_PROXY) if WARP_PROXY else None


def resolve_proxy(requested: str | None, host: str = "") -> str | None:
    """Explicit proxy wins; 'direct'/'off' opt out; otherwise WARP_PROXY when set, else direct."""
    if requested and requested.strip().lower() in DIRECT_SENTINELS:
        return None
    if requested:
        return normalize_proxy(requested)
    return _DEFAULT_EGRESS


def forbidden_target(host: str | None) -> str | None:
    """Why a target host must not be fetched (None = allowed): loopback, private, link-local and other non-public IP
    literals, `localhost`, and dot-less names that only resolve inside the container network (e.g. `warp`)."""
    if ALLOW_PRIVATE_TARGETS or not host:
        return None
    name = host.strip("[]").rstrip(".").lower()
    if name == "localhost" or name.endswith((".localhost", ".internal", ".local")) or "." not in name and ":" not in name:
        return f"Target host '{host}' is internal; only public hosts can be fetched"
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return None
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if not ip.is_global:
        return f"Target address '{host}' is not a public IP"
    return None


def is_proxy_error(message: str | None) -> bool:
    """True when a network error was raised by the proxy hop rather than the target site."""
    if not message:
        return False
    low = message.lower()
    return any(
        marker in low
        for marker in (
            "err_proxy",
            "err_tunnel",
            "err_socks",
            "proxy connection",
            "proxy authentication",
            "proxy handshake",
            "proxy_error",
            "http 407",
            "status 407",
            "socks connect",
            "socks error",
            "socks5 connection",
            "socks4 connection",
        )
    )
