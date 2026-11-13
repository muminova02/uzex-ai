"""Benchmark dashboard/explorer endpoints against a running server (default http://127.0.0.1:8000).
Uncached = every request uses a never-seen filter combination (distinct day ranges); cached = repeat."""
import statistics
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
EP = ["/api/analytics/overview", "/api/analytics/trend", "/api/analytics/rank?dim=commodity&limit=10",
      "/api/analytics/rank?dim=region&limit=14", "/api/analytics/status", "/api/deals?size=25&sort=amount_uzs"]

if __name__ == "__main__":
    with httpx.Client(base_url=BASE, timeout=60) as c:
        print(f"{'endpoint':45s} {'uncached p50':>12s} {'uncached max':>12s} {'cached p50':>10s}")
        for ep in EP:
            sep = "&" if "?" in ep else "?"
            un, ca = [], []
            for i in range(12):  # 12 distinct date ranges over the full dataset, never seen before
                url = f"{ep}{sep}start=2022-01-{3 + i:02d}&end=2026-10-{30 - i:02d}"
                t = time.perf_counter(); r = c.get(url); un.append((time.perf_counter() - t) * 1000); r.raise_for_status()
                t = time.perf_counter(); c.get(url); ca.append((time.perf_counter() - t) * 1000)
            print(f"{ep:45s} {statistics.median(un):10.0f}ms {max(un):10.0f}ms {statistics.median(ca):8.1f}ms")
        t = time.perf_counter(); r = c.get("/api/deals/export.csv?commodity=gasoline&start=2025-01-01&end=2025-12-31")
        print(f"export 5 813 rows: {(time.perf_counter() - t) * 1000:.0f} ms, {len(r.content) / 1e6:.2f} MB, status {r.status_code}")
