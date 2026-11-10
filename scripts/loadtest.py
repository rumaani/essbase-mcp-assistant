"""Simulate many users asking questions at once against a running `emcp web`.

    emcp web                                   # in one terminal (mock or real 26ai)
    python scripts/loadtest.py --users 100 --questions 5

Each simulated user asks `--questions` questions back to back (a mix across cubes,
including member lookups and top-N). Prints latency percentiles, errors, and the server's
own metrics (token fetches, peak parallel Essbase calls, busy rejections).
"""
from __future__ import annotations

import argparse
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

H = {"X-Requested-With": "essbase-mcp-assistant"}
MIX = [
    ("Sample.Basic", "Sales by market"),
    ("Sample.Basic", "revenue and cost of sales for Colas by market in Qtr1"),
    ("Sample.Basic", "top 3 products by sales with profit"),
    ("Sample.Basic", "bottom 2 markets by profit by year"),
    ("ASOSamp.Basic", "Units by geography"),
    ("ASOSamp.Basic", "Units for Store 099"),
    ("ASOSamp.Basic", "Price Paid for Store 117"),
    ("ASOSamp.Basic", "top 5 stores by units"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--users", type=int, default=100)
    ap.add_argument("--questions", type=int, default=5, help="questions per user")
    a = ap.parse_args()

    http = httpx.Client(base_url=a.url, headers=H, timeout=120,
                        limits=httpx.Limits(max_connections=a.users + 10))
    for cube in sorted({c for c, _ in MIX}):  # make sure every cube is loaded
        app, db = cube.split(".")
        if http.get("/api/context", params={"cube": cube}).status_code == 404:
            http.post("/api/context/build", json={"application": app, "database": db}).raise_for_status()
    before = http.get("/api/metrics").json()

    def user(u: int) -> list[tuple[float, int, bool]]:
        out = []
        for k in range(a.questions):
            cube, q = MIX[(u + k) % len(MIX)]
            t = time.perf_counter()
            r = http.post("/api/ask", json={"question": q, "cube": cube})
            ok = r.status_code == 200 and r.json().get("ok") and not r.json().get("error")
            out.append((time.perf_counter() - t, r.status_code, bool(ok)))
        return out

    started = time.perf_counter()
    with ThreadPoolExecutor(a.users) as pool:
        results = [x for batch in pool.map(user, range(a.users)) for x in batch]
    wall = time.perf_counter() - started
    after = http.get("/api/metrics").json()

    lat = sorted(r[0] * 1000 for r in results)
    codes: dict[int, int] = {}
    for _, c, _ in results:
        codes[c] = codes.get(c, 0) + 1
    pct = lambda p: lat[min(len(lat) - 1, int(p * len(lat)))]
    print(f"{a.users} users x {a.questions} questions = {len(results)} requests in {wall:.1f}s "
          f"({len(results) / wall:.0f} req/s)")
    print(f"latency ms  p50 {pct(.5):.0f}  p95 {pct(.95):.0f}  p99 {pct(.99):.0f}  "
          f"max {lat[-1]:.0f}  mean {statistics.mean(lat):.0f}")
    print(f"HTTP codes  {codes}   answered correctly: {sum(r[2] for r in results)}/{len(results)}")
    print(f"server      token fetches {after['token_fetches'] - before['token_fetches']}, "
          f"MCP session inits {after.get('initializations', 0) - before.get('initializations', 0)}, "
          f"Essbase calls {after.get('tool_calls', 0) - before.get('tool_calls', 0)}, "
          f"peak parallel {after['peak_in_flight']}/{after['max_concurrent_calls']}, "
          f"busy {after.get('busy_rejections', 0) - before.get('busy_rejections', 0)}, "
          f"errors {after.get('errors', 0) - before.get('errors', 0)}")


if __name__ == "__main__":
    main()
