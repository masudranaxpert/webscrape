#!/usr/bin/env python3
"""End-to-end target-site direct video link extractor via Anti API."""

from __future__ import annotations

import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup


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


def extract_direct_video_url(
    target_url: str,
    api_base: str = os.environ.get("ANTI_URL", "https://your-domain.example"),
    api_key: str | None = None,
) -> dict[str, str | float]:
    """Extract Google direct video download link and measure elapsed latency."""
    t_start = time.perf_counter()
    fetch_endpoint = f"{api_base.rstrip('/')}/fetch"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    if api_key:
        headers["X-API-Key"] = api_key

    print(f"[*] Target URL: {target_url}")
    print(f"[*] API Endpoint: {fetch_endpoint}\n")

    client = httpx.Client(timeout=120.0)

    # Step 1: Harvest initial cookies and resolve main page Cloudflare wall
    s1_start = time.perf_counter()
    print("[1/3] Fetching main page & solving initial challenge...")
    r1 = client.post(
        fetch_endpoint,
        json={
            "url": target_url,
            "method": "GET",
            "force_browser": True,
            "cf_wait": 35.0,
            "wait_for_selector": ".btn-instant",
        },
        headers=headers,
    )
    r1.raise_for_status()
    d1 = r1.json()
    s1_time = time.perf_counter() - s1_start
    print(f"      Status: {d1.get('status_code')} | Via: {d1.get('meta', {}).get('via')} | Time: {s1_time:.2f}s")

    # Locate the legitimate Instant Download link (filter honeypots with tabindex="-1" or nofollow)
    soup1 = BeautifulSoup(d1.get("body", ""), "html.parser")
    real_link: str | None = None
    for a in soup1.find_all("a"):
        text = a.get_text().strip().lower()
        cls = " ".join(a.get("class", []))
        if "instant" in text or "btn-instant" in cls:
            tabindex = a.get("tabindex")
            rel = a.get("rel") or []
            if tabindex != "-1" and "nofollow" not in rel:
                real_link = a.get("href")
                break

    if not real_link:
        raise RuntimeError("No legitimate Instant Download link found in main page HTML.")

    instant_url = urljoin(target_url, real_link)
    print(f"      Resolved Instant URL: {instant_url}")

    # Step 2: Navigate to Instant Download page and solve Turnstile widget
    s2_start = time.perf_counter()
    print("\n[2/3] Resolving embedded Turnstile on Instant Download page...")
    r2 = client.post(
        fetch_endpoint,
        json={
            "url": instant_url,
            "method": "GET",
            "inject_cookies": d1.get("cookies", {}),
            "cf_wait": 35.0,
            "capture_token": True,
        },
        headers=headers,
    )
    r2.raise_for_status()
    d2 = r2.json()
    s2_time = time.perf_counter() - s2_start

    token2 = d2.get("turnstile_token")
    cookies2 = d2.get("cookies", {})
    print(f"      Status: {d2.get('status_code')} | Token Captured: {bool(token2)} | Time: {s2_time:.2f}s")

    if not token2:
        raise RuntimeError("Failed to capture Turnstile verification token from Instant page.")

    # Step 3: Fast-path POST submission with captured token and session cookies
    s3_start = time.perf_counter()
    print("\n[3/3] Submitting verification token via fast-path HTTP POST...")
    origin = f"{urlparse(instant_url).scheme}://{urlparse(instant_url).netloc}"
    post_payload = f"ts={token2}&cf-turnstile-response={token2}"
    r3 = client.post(
        fetch_endpoint,
        json={
            "url": instant_url,
            "method": "POST",
            "body": post_payload,
            "headers": {
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": instant_url,
                "Origin": origin,
            },
            "inject_cookies": cookies2,
            "force_http": True,
        },
        headers=headers,
    )
    r3.raise_for_status()
    d3 = r3.json()
    s3_time = time.perf_counter() - s3_start
    print(f"      Status: {d3.get('status_code')} | Via: {d3.get('meta', {}).get('via')} | Time: {s3_time:.2f}s")

    # Parse final direct Google video URL
    body3 = d3.get("body", "")
    google_links = re.findall(r"https://video-downloads\.googleusercontent\.com/[^\s\"'<>]+", body3)
    if not google_links:
        google_links = re.findall(r"https://[^\s\"'<>]*googleusercontent\.com/[^\s\"'<>]+", body3)

    if not google_links:
        raise RuntimeError(f"Video download link not found on final landing page (Title: {d3.get('title')}).")

    video_url = google_links[0]
    total_time = time.perf_counter() - t_start

    return {
        "video_url": video_url,
        "step1_time": s1_time,
        "step2_time": s2_time,
        "step3_time": s3_time,
        "total_time": total_time,
    }


def main() -> None:
    env = load_env(".env")
    api_key = env.get("API_KEY", "anti_secret_help_09_tto09hj")
    target_url = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "https://sub.target-site.example/f/vJ9XPcvnW8qQzylGg99SMt7F5zMHL1CpcXF5nt4p1Mh6s-DQnieAz2z6_BpgKQ"
    )

    try:
        results = extract_direct_video_url(target_url=target_url, api_key=api_key)
        print("\n" + "=" * 78)
        print("EXTRACTION SUCCESSFUL")
        print("=" * 78)
        print(f"Direct Video Download URL:\n{results['video_url']}\n")
        print("Execution Time Breakdown:")
        print(f"  Step 1 (Main Page + Wall Bypass): {results['step1_time']:.2f}s")
        print(f"  Step 2 (Instant Page + Turnstile): {results['step2_time']:.2f}s")
        print(f"  Step 3 (Token POST -> Direct Link): {results['step3_time']:.2f}s")
        print(f"  TOTAL ELAPSED TIME:               {results['total_time']:.2f}s")
        print("=" * 78)
    except Exception as err:
        print(f"\n[ERROR] Extraction failed: {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
