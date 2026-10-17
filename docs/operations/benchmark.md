# Capacity benchmark

Measured live on production (2026-10-17) from inside the `anti` container, with the benchmark script in
`docs/operations/bench.py`.

**Host:** 2 vCPU, 4 GB RAM (≈1.9 GB available to us; other apps share the VPS), 2 GB swap, no container memory
limit. **Config:** `MAX_TABS=5`, `MAX_BROWSERS=1`, headed Chromium in Xvfb, `WARP_PROXY` set (two Chromium processes
alive: direct + WARP).

## Results

| Phase | Concurrency | Throughput | p50 | p95 | Peak container RAM |
| --- | --: | --: | --: | --: | --: |
| HTTP engine, tiny page | 10 | 3,580 req/min | 0.12 s | 0.35 s | 687 MB |
| HTTP engine | 30 | 6,300 req/min | 0.26 s | 0.34 s | 705 MB |
| HTTP engine | 60 | 6,580 req/min | 0.45 s | 0.71 s | 718 MB |
| Browser, light page | 1 tab | 20 pages/min | 3.0 s | 3.2 s | 747 MB |
| Browser, light page | 3 tabs | 49 pages/min | 3.5 s | 4.4 s | 842 MB |
| Browser, light page | 5 tabs | 71 pages/min | 4.1 s | 4.6 s | 929 MB |
| Browser, Cloudflare-protected page (clearance cached) | 1 tab | 17 pages/min | 3.7 s | 3.7 s | 804 MB |
| Browser, Cloudflare-protected page | 5 tabs | 52 pages/min | 5.4 s | 6.8 s | 1,013 MB |
| Mixed: 10 browser jobs + 150 HTTP at once | 40 | all succeeded in 9.2 s | — | HTTP 0.79 s | 962 MB |

A full first-time Cloudflare solve takes ~9–13 s of one tab (clicking, waiting for clearance). Every following
request for that host is an HTTP replay (~0.1 s).

## Memory model (PSS, shared pages split fairly)

| Item | Cost |
| --- | --: |
| Python app (uvicorn, httpcloak) | ~110–140 MB |
| One idle Chromium process (headed, with its renderer/GPU/utility children) | ~280 MB |
| Its Playwright driver (node) | ~80 MB |
| Each additional open tab, light page | ~40 MB |
| Each additional open tab, real-world page | ~60 MB (heavy SPAs: 100–150 MB) |
| HTTP engine at 60 concurrent requests | +30 MB |

Rule of thumb: **RAM ≈ 150 MB + 360 MB × browser processes + 60–100 MB × open tabs.**

## What 2 GB can handle

| Budget | Processes | Safe `MAX_TABS` | Browser throughput (estimate) |
| --- | --: | --: | --: |
| ~1.0 GB (today) | 2 | 5 (measured) | 50–70 pages/min |
| ~1.5 GB | 2 | 8–10 | 80–100 pages/min, CPU-bound |
| ~2.0 GB | 2–3 | 12–15 | limited by 2 vCPU, not RAM |

!!! warning "CPU, not RAM, is the next wall"
    p50 grew from 3.0 s (1 tab) to 4.1 s (5 tabs) on 2 vCPU. Beyond ~8–10 tabs, pages get slower rather than more
    numerous. Raise `MAX_TABS` in steps (5 → 8) and watch p95 and `/health` before going further. The estimates above
    8 tabs are extrapolated, not measured.

The HTTP engine is not the bottleneck: it served ~6,500 requests/minute while adding only ~30 MB.

## Re-running

```bash
cat docs/operations/bench.py | nd exec -A <account> -s anti -i -t 1800 <app-id> /app/venv/bin/python -u -
```

It sends real traffic (example.com, cloudflare.com trace, a public Cloudflare test page); keep runs short.
