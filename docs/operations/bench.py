"""Capacity benchmark run inside the anti container: memory and throughput of the HTTP and browser engines."""
import glob, json, os, statistics, threading, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "http://127.0.0.1:8000"
KEY = os.environ["API_KEY"]


def call(payload, timeout=240):
    req = urllib.request.Request(BASE + "/fetch", json.dumps(payload).encode(),
                                 {"X-API-Key": KEY, "Content-Type": "application/json"})
    t = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = json.load(r)
    except Exception as e:
        return {"status_code": -1, "err": repr(e)}, time.monotonic() - t
    return b, time.monotonic() - t


def mem_available_mb():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable"):
            return int(line.split()[1]) // 1024


def cgroup_mb():
    for p in ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
        try:
            return int(open(p).read()) // 1048576
        except OSError:
            pass
    return -1


def pss_by_kind():
    """Proportional memory (shared pages split fairly) per process family, in MB."""
    out = {"chromium": 0, "playwright-node": 0, "python": 0, "other": 0}
    for d in glob.glob("/proc/[0-9]*"):
        try:
            cmd = open(f"{d}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
            pss = next(int(l.split()[1]) for l in open(f"{d}/smaps_rollup") if l.startswith("Pss:"))
        except Exception:
            continue
        kind = ("chromium" if "/chrome" in cmd else "playwright-node" if "node" in cmd and "playwright" in cmd
                else "python" if "python" in cmd or "uvicorn" in cmd else "other")
        out[kind] += pss // 1024
    out["total"] = sum(out.values())
    return out


class Sampler:
    def __init__(self):
        self.peak_cg, self.min_avail, self.peak_pss, self._stop = 0, 10**9, {}, False
        self.t = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self._stop:
            self.peak_cg = max(self.peak_cg, cgroup_mb())
            self.min_avail = min(self.min_avail, mem_available_mb())
            p = pss_by_kind()
            if p["total"] > self.peak_pss.get("total", 0):
                self.peak_pss = p
            time.sleep(0.5)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self._stop = True
        self.t.join()


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def phase(name, payloads, workers):
    with Sampler() as s:
        t = time.monotonic()
        with ThreadPoolExecutor(workers) as ex:
            res = list(ex.map(call, payloads))
        wall = time.monotonic() - t
    ok = [dt for b, dt in res if b.get("status_code") == 200]
    bad = [b.get("status_code") for b, _ in res if b.get("status_code") != 200]
    print(f"[{name}] n={len(res)} conc={workers} ok={len(ok)} fail={bad[:8]} wall={wall:.1f}s "
          f"rate={len(ok)/wall*60:.0f}/min p50={pct(ok,0.5) if ok else 0:.2f}s p95={pct(ok,0.95) if ok else 0:.2f}s "
          f"| peak_container={s.peak_cg}MB min_host_avail={s.min_avail}MB peak_pss={s.peak_pss}")


print(f"== host MemAvailable={mem_available_mb()}MB container={cgroup_mb()}MB idle_pss={pss_by_kind()}")
print(f"== cpus={os.cpu_count()} load={open('/proc/loadavg').read().split()[:3]}")

# HTTP engine: tiny target, rising concurrency
url = "https://www.cloudflare.com/cdn-cgi/trace"
for conc in (10, 30, 60):
    phase(f"http c={conc}", [{"url": url, "force_http": True} for _ in range(conc * 4)], conc)

# Browser engine, one identity (direct), 1..5 tabs at once (MAX_TABS=5)
for conc in (1, 3, 5):
    phase(f"browser-light c={conc}", [{"url": f"https://example.com/?b={conc}{i}", "force_browser": True, "proxy": "direct"} for i in range(conc * 3)], conc)

# Heavier real page (Cloudflare-protected site with clearance cached for WARP), both browsers alive
for conc in (1, 5):
    phase(f"browser-heavy c={conc}", [{"url": "https://www.scrapingcourse.com/cloudflare-challenge", "force_browser": True, "cf_wait": 30} for _ in range(conc * 2)], conc)

# Mixed: 5 browser tabs busy while HTTP keeps flowing
with Sampler() as s:
    t = time.monotonic()
    with ThreadPoolExecutor(40) as ex:
        b_jobs = [ex.submit(call, {"url": f"https://example.com/?m={i}", "force_browser": True, **({"proxy": "direct"} if i % 2 else {})}) for i in range(10)]
        h_jobs = [ex.submit(call, {"url": url, "force_http": True}) for _ in range(150)]
        bres = [f.result() for f in b_jobs]
        hres = [f.result() for f in h_jobs]
    wall = time.monotonic() - t
print(f"[mixed] browser ok={sum(b.get('status_code')==200 for b,_ in bres)}/10 http ok={sum(b.get('status_code')==200 for b,_ in hres)}/150 "
      f"http_p95={pct([dt for _,dt in hres],0.95):.2f}s wall={wall:.1f}s | peak_container={s.peak_cg}MB min_host_avail={s.min_avail}MB peak_pss={s.peak_pss}")

time.sleep(5)
print(f"== after: host MemAvailable={mem_available_mb()}MB container={cgroup_mb()}MB pss={pss_by_kind()}")
