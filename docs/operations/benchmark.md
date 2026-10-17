# Capacity benchmark

Measured live on production (2026-10-17) from inside the `anti` container, with the script in
`docs/operations/bench.py`.

**Host:** 2 vCPU, 4 GB RAM (≈2 GB available to us; other apps share the VPS), 2 GB swap, no container memory limit.
**Config:** `MAX_TABS=5`, `MAX_BROWSERS=1`, headed Chromium in Xvfb, `WARP_PROXY` set, so two Chromium processes run:
direct (keyless build, Chromium 146) and WARP (holds the license seat: Pro build, Chromium 154).

## Results

Two runs: before and after removing the fixed 2.5 s settle from every browser fetch and giving the license seat to
the WARP browser only.

| Phase | Concurrency | Before | After | p50 after | p95 after |
| --- | --: | --: | --: | --: | --: |
| HTTP engine, tiny page | 10 | 3,580 /min | 1,920 /min | 0.16 s | 0.77 s |
| HTTP engine | 30 | 6,300 /min | 4,710 /min | 0.36 s | 0.48 s |
| HTTP engine | 60 | 6,580 /min | 5,780 /min | 0.58 s | 0.77 s |
| Browser, light page | 3 tabs | 49 /min | **86 /min** | 2.1 s | 2.6 s |
| Browser, light page | 5 tabs | 71 /min | **109 /min** | 2.5 s | 3.4 s |
| Browser, Cloudflare-protected page (clearance cached) | 5 tabs | 52 /min | 56 /min | 5.1 s | 5.7 s |
| Mixed: 10 browser jobs + 150 HTTP at once | 40 | 9.2 s, all ok | 9.5 s, all ok | — | HTTP 1.7 s |

The HTTP engine did not change between the runs; its spread reflects host load at the time (load average 2.3 on
2 vCPU during the second run). A single light browser fetch now takes ~1.7–1.8 s of tab time instead of ~3 s.

A first-time Cloudflare solve takes ~9–13 s of one tab. Every following request for that host is an HTTP replay
(0.1–0.6 s, plus whatever the origin itself needs: some origins alone take ~0.5 s per page).

## Memory model

| Item | Cost (PSS) |
| --- | --: |
| Python app (uvicorn, httpcloak) | ~110–130 MB |
| One idle keyless Chromium process tree (146) | ~250–280 MB |
| One idle licensed Pro Chromium process tree (154) | ~300–350 MB |
| Each Playwright driver (one per Chromium process) | ~80–130 MB |
| Each additional open tab, light page | ~30–40 MB |
| Each additional open tab, real-world page | ~60–100 MB (heavy SPAs: 100–150 MB) |
| HTTP engine at 60 concurrent requests | +20–30 MB |

Peak container memory: **1.13 GB** at 5 light tabs, **1.46 GB** at 5 heavy tabs with both browsers alive.

Rule of thumb: **RAM ≈ 150 MB + ~400 MB × browser processes + 60–100 MB × open tabs.**

## What 2 GB can handle

| Budget | Processes | Safe `MAX_TABS` | Browser throughput (estimate) |
| --- | --: | --: | --: |
| ~1.5 GB (today) | 2 | 5 (measured) | ~110 light / ~55 heavy pages/min |
| ~2.0 GB | 2 | 8 | ~140–160 light pages/min, CPU-bound |

!!! warning "CPU, not RAM, is the next wall"
    On 2 vCPU, p50 grows with every extra tab (1.7 s at 1 tab → 2.5 s at 5). Raise `MAX_TABS` in steps (5 → 8) and
    watch p95 and `/health`; going past 8 makes pages slower rather than more numerous. Numbers above 5 tabs are
    extrapolated, not measured.

## Re-running

```bash
cat docs/operations/bench.py | nd exec -s anti -i -t 1800 <app-id> /app/venv/bin/python -u -
```

It sends real traffic (example.com, cloudflare.com trace, a public Cloudflare test page); keep runs short.
