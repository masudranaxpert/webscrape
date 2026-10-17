#!/usr/bin/env python3
"""End-to-end multi-step stream extraction and challenge resolution test script."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser


def load_env(path: str = ".env") -> dict[str, str]:
    """Parse key-value pairs from .env file without external dependencies."""
    env: dict[str, str] = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("\"'")
    return env


def extract_media_link(
    target_url: str,
    api_base: str = os.environ.get("ANTI_URL", "http://127.0.0.1:8000"),
    api_key: str | None = None,
) -> dict[str, str | float]:
    """Extract direct media download link through multi-step challenge resolution."""
    t_start = time.perf_counter()
    fetch_endpoint = f"{api_base.rstrip('/')}/fetch"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    if api_key:
        headers["X-API-Key"] = api_key

    def _post_json(url: str, payload: dict) -> dict:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120.0) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # Initial page retrieval awaiting client-rendered elements
    s1_start = time.perf_counter()
    d1 = _post_json(
        fetch_endpoint,
        {
            "url": target_url,
            "method": "GET",
            "force_browser": True,
            "cf_wait": 35.0,
            "wait_for_selector": ".btn-instant",
        },
    )
    s1_time = time.perf_counter() - s1_start

    # Locate active target link while ignoring honeypots and nofollow anchors
    tree1 = LexborHTMLParser(d1.get("body", ""))
    sub_link: str | None = None
    for a in tree1.css("a"):
        text = a.text(strip=True).lower()
        cls = a.attributes.get("class") or ""
        if "instant" in text or "btn-instant" in cls:
            if a.attributes.get("tabindex") != "-1" and "nofollow" not in (a.attributes.get("rel") or ""):
                sub_link = a.attributes.get("href")
                break

    if not sub_link:
        raise RuntimeError("Target stream action link not found in initial response.")

    next_url = urljoin(target_url, sub_link)

    # Verification solve and automated button submission
    s2_start = time.perf_counter()
    d2 = _post_json(
        fetch_endpoint,
        {
            "url": next_url,
            "method": "GET",
            "force_browser": True,
            "cf_wait": 35.0,
            "click_selector": "#go-btn",
            "inject_cookies": d1.get("cookies", {}),
        },
    )
    s2_time = time.perf_counter() - s2_start

    # Resolve destination media URLs from response payload
    body2 = d2.get("body", "")
    final_url = d2.get("url", "")
    media_links = re.findall(r"https://[^\s\"'<>]*(?:googleusercontent|storage|cdn)[^\s\"'<>]+", body2)
    if not media_links and ("googleusercontent" in final_url or "cdn" in final_url):
        media_links = [final_url]

    if not media_links:
        raise RuntimeError(f"Direct media link not resolved (landing page status: {d2.get('status_code')}).")

    return {
        "media_url": media_links[0],
        "step1_time": s1_time,
        "step2_time": s2_time,
        "total_time": time.perf_counter() - t_start,
    }


def main() -> None:
    env = load_env(".env")
    api_key = env.get("API_KEY")
    target_url = sys.argv[1] if len(sys.argv) > 1 else "https://example-streaming.site/f/sample-video-id"

    try:
        results = extract_media_link(target_url=target_url, api_key=api_key)
        print("Resolved Media URL:", results["media_url"])
        print(f"Elapsed: {results['total_time']:.2f}s (step 1: {results['step1_time']:.2f}s, step 2: {results['step2_time']:.2f}s)")
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
