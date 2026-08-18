#!/usr/bin/env python3
"""Where a result set's memory lives and who pays for it — pure-Python vs Rust PackStream.

Run the SAME script in two environments against the same database:

    a venv with plain `neo4j`            -> pure-Python PackStream decoder
    a venv with `neo4j-rust-ext` added   -> Rust PackStream decoder, identical driver API

The script detects which decoder is active and stamps every line, so two runs diff cleanly.

Per row count it measures the three costs a returned row incurs on the harness side:

    fetch      wall time to stream + materialize rows as Python dicts
    py-alloc   tracemalloc peak across the materialization — the PyObject cost per row,
               which is what "the runtime manages memory for you" actually charges
    rss        VmRSS delta, what the OS sees
    gc         collections triggered during the fetch
    payload    time and peak to turn the dicts into the model-facing string, JSON and CSV —
               the point where the driver axis meets the format axis

Per worker count it measures what an agent fleet feels:

    tool-call latency p50/p99 with T threads sharing one driver. A decoder that holds the
    GIL serializes every other episode's decode; if that is real, p99 inflates with T on
    the pure build and less on the rust build. The delta is the measured version of the
    claim — not asserted from the runtime's design, but observed or refuted.

    python scripts/bench_driver_memory.py --password "$PW" [--database neo4j]
"""
from __future__ import annotations

import argparse
import csv
import datetime
import gc
import io
import json
import multiprocessing
import resource
import statistics
import subprocess
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from neo4j import GraphDatabase

from runmeta import manifest

QUERY = "UNWIND range(1,$n) AS i RETURN i, toString(i) AS s, i*1.5 AS f"
ROW_SWEEP = [1_000, 10_000, 100_000]
WORKER_SWEEP = [1, 2, 4, 8]
CONCURRENT_ROWS = 2_000      # per call in the concurrency stage
CALLS_PER_WORKER = 25


def decoder() -> str:
    # neo4j-rust-ext installs `neo4j/_rust*.so`; when present, the v1 codec binds its
    # pack/unpack to it. Checking the codec's own binding (not just the .so) means a broken
    # install reports as pure-python instead of silently lying about what was measured.
    import neo4j._codec.packstream.v1 as v1
    return "rust" if getattr(v1, "_rust_unpack", None) is not None else "pure-python"


def rss_kb() -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


def collections() -> int:
    return sum(s["collections"] for s in gc.get_stats())


def cpu_now() -> tuple[float, int]:
    """Process CPU seconds (user+sys, all threads) and involuntary context switches.

    The pair separates two stories: CPU says how much work the client did per row; the
    involuntary switch count says how often a thread was thrown off the core while holding
    work — under the GIL that is the contention the p99 tail is made of.
    """
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return ru.ru_utime + ru.ru_stime, ru.ru_nivcsw


def server_cpu_reader(container: str):
    """Reader for the DB container's cumulative CPU, from its cgroup. None if unavailable.

    Client CPU alone cannot carry an efficiency claim — a client change that merely shifts
    work onto the server would look like a win. Charging both sides closes that hole.
    """
    try:
        cid = subprocess.check_output(["docker", "inspect", "--format", "{{.Id}}", container],
                                      text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None
    for pattern in (f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/cpu.stat",
                    f"/sys/fs/cgroup/docker/{cid}/cpu.stat"):
        path = Path(pattern)
        if path.exists():
            def read(path=path) -> float:
                for line in path.read_text().splitlines():
                    if line.startswith("usage_usec"):
                        return int(line.split()[1]) / 1e6
                return 0.0
            return read
    return None


def fetch(session_factory, n: int, *, cap: int | None = None):
    with session_factory() as s:
        result = s.run(QUERY, n=n)
        if cap is None:
            rows = [dict(r) for r in result]
        else:
            rows = [dict(r) for _, r in zip(range(cap), result)]
        result.consume()
    return rows


def _proc_worker(job) -> list[float]:
    """One OS process: own driver, own GIL. The control for the thread stage — if threads
    were capped by the interpreter and not by the box or the server, processes scale."""
    uri, user, password, database, n, calls_n = job
    drv = GraphDatabase.driver(uri, auth=(user, password))
    sess = lambda: drv.session(database=database)
    fetch(sess, n)  # warm this process's pool and plan cache
    lat = []
    for _ in range(calls_n):
        t0 = time.perf_counter()
        fetch(sess, n)
        lat.append((time.perf_counter() - t0) * 1000)
    drv.close()
    return lat


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", required=True)
    ap.add_argument("--database", default="neo4j")
    ap.add_argument("--repeat", type=int, default=10)
    ap.add_argument("--db-container", default="graphrag-neo4j",
                    help="docker container whose cgroup CPU is charged as server-side cost")
    ap.add_argument("--json", default=None,
                    help="where to write the machine-readable report "
                         "(default results/interface/driver_memory_<decoder>_<utc>.json)")
    args = ap.parse_args()

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    driver.verify_connectivity()
    sf = lambda: driver.session(database=args.database)
    dec = decoder()
    server_cpu = server_cpu_reader(args.db_container)
    report: dict = {"manifest": manifest(args.db_container, database=args.database,
                                         repeat=args.repeat, query=QUERY),
                    "row_sweep": [], "concurrency_threads": [], "concurrency_procs": []}
    print(f"decoder={dec}  database={args.database}  repeat={args.repeat}  "
          f"server-cgroup={'yes' if server_cpu else 'unavailable'}\n")

    for n in ROW_SWEEP:
        fetch(sf, n)  # warm the plan cache and the pool before anything is timed

        # timing pass — tracemalloc off, it distorts time
        times = []
        cpu0, ivcs0 = cpu_now()
        srv0 = server_cpu() if server_cpu else 0.0
        wall0 = time.perf_counter()
        for _ in range(args.repeat):
            t0 = time.perf_counter()
            rows = fetch(sf, n)
            times.append((time.perf_counter() - t0) * 1000)
        wall = time.perf_counter() - wall0
        cpu1, ivcs1 = cpu_now()
        srv1 = server_cpu() if server_cpu else 0.0
        cli_ms = (cpu1 - cpu0) * 1000 / args.repeat
        srv_ms = (srv1 - srv0) * 1000 / args.repeat

        # memory pass — one shot, instrumented
        gc.collect()
        rss0, gc0 = rss_kb(), collections()
        tracemalloc.start()
        rows = fetch(sf, n)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        rss1, gc1 = rss_kb(), collections()

        # payload pass — the dicts -> model-string step, both encodings
        tracemalloc.start()
        t0 = time.perf_counter()
        js = json.dumps({"rows": rows}, default=str)
        js_ms = (time.perf_counter() - t0) * 1000
        js_peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()

        tracemalloc.start()
        t0 = time.perf_counter()
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
        cs = buf.getvalue()
        cs_ms = (time.perf_counter() - t0) * 1000
        cs_peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()

        del rows
        print(f"[rows={n:,}] ({dec})")
        print(f"  fetch      median {statistics.median(times):8.1f} ms")
        print(f"  cpu        client {cli_ms:7.1f} ms/fetch ({cli_ms*1000/n:6.1f} us/row, "
              f"util {(cpu1-cpu0)/wall:4.0%})   server {srv_ms:7.1f} ms/fetch"
              + ("" if server_cpu else " (n/a)"))
        print(f"  ctx-sw     involuntary +{ivcs1-ivcs0} across {args.repeat} fetches")
        print(f"  py-alloc   peak {peak/2**20:8.1f} MiB   {peak/n:6.0f} B/row")
        print(f"  rss        delta {(rss1-rss0)/1024:7.1f} MiB   gc collections +{gc1-gc0}")
        print(f"  payload    json {js_ms:7.1f} ms peak {js_peak/2**20:6.1f} MiB "
              f"{len(js):>11,} chars")
        print(f"             csv  {cs_ms:7.1f} ms peak {cs_peak/2**20:6.1f} MiB "
              f"{len(cs):>11,} chars")
        report["row_sweep"].append({
            "rows": n, "decoder": dec, "fetch_ms_samples": [round(x, 2) for x in times],
            "client_cpu_ms_per_fetch": round(cli_ms, 2),
            "client_cpu_us_per_row": round(cli_ms * 1000 / n, 2),
            "client_util": round((cpu1 - cpu0) / wall, 3),
            "server_cpu_ms_per_fetch": round(srv_ms, 2) if server_cpu else None,
            "involuntary_ctx_switches": ivcs1 - ivcs0,
            "py_alloc_peak_bytes": peak, "py_alloc_bytes_per_row": round(peak / n),
            "rss_delta_kb": rss1 - rss0, "gc_collections": gc1 - gc0,
            "payload_json": {"ms": round(js_ms, 2), "peak_bytes": js_peak, "chars": len(js)},
            "payload_csv": {"ms": round(cs_ms, 2), "peak_bytes": cs_peak, "chars": len(cs)},
        })

    print(f"\n[concurrency] {CONCURRENT_ROWS:,} rows/call, "
          f"{CALLS_PER_WORKER} calls/worker, one shared driver")
    for t in WORKER_SWEEP:
        latencies: list[float] = []

        def one_call() -> None:
            t0 = time.perf_counter()
            fetch(sf, CONCURRENT_ROWS)
            latencies.append((time.perf_counter() - t0) * 1000)

        cpu0, ivcs0 = cpu_now()
        srv0 = server_cpu() if server_cpu else 0.0
        wall0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=t) as pool:
            list(pool.map(lambda _: one_call(), range(t * CALLS_PER_WORKER)))
        wall = time.perf_counter() - wall0
        cpu1, ivcs1 = cpu_now()
        srv1 = server_cpu() if server_cpu else 0.0
        ncalls = t * CALLS_PER_WORKER
        lat = sorted(latencies)
        p50 = lat[len(lat) // 2]
        p99 = lat[min(len(lat) - 1, int(len(lat) * 0.99))]
        # util is against ONE core on purpose: with the GIL, >100% is only reachable while
        # decode runs outside the interpreter, so this number is the GIL-escape gauge.
        print(f"  workers={t:<2} ({dec})  p50 {p50:7.1f} ms   p99 {p99:7.1f} ms   "
              f"client-util {(cpu1-cpu0)/wall:4.0%}   "
              f"cpu/call client {(cpu1-cpu0)*1000/ncalls:5.1f} ms"
              + (f" server {(srv1-srv0)*1000/ncalls:5.1f} ms" if server_cpu else "")
              + f"   ivcs +{ivcs1-ivcs0}")
        report["concurrency_threads"].append({
            "workers": t, "decoder": dec, "latency_ms_samples": [round(x, 1) for x in lat],
            "p50_ms": round(p50, 1), "p99_ms": round(p99, 1), "wall_s": round(wall, 2),
            "client_util": round((cpu1 - cpu0) / wall, 3),
            "client_cpu_ms_per_call": round((cpu1 - cpu0) * 1000 / ncalls, 2),
            "server_cpu_ms_per_call": (round((srv1 - srv0) * 1000 / ncalls, 2)
                                       if server_cpu else None),
            "involuntary_ctx_switches": ivcs1 - ivcs0,
        })

    # The control: same load, but N processes instead of N threads. Each process has its own
    # interpreter and its own GIL, so if the thread stage was capped by the runtime and not by
    # the box or the server, aggregate utilization scales here and per-call latency holds.
    print(f"\n[concurrency-procs] same load, one process per worker")
    for t in WORKER_SWEEP:
        jobs = [(args.uri, args.user, args.password, args.database,
                 CONCURRENT_ROWS, CALLS_PER_WORKER)] * t
        ru0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        srv0 = server_cpu() if server_cpu else 0.0
        wall0 = time.perf_counter()
        with multiprocessing.Pool(processes=t) as pool:
            latss = pool.map(_proc_worker, jobs)
        wall = time.perf_counter() - wall0
        ru1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        srv1 = server_cpu() if server_cpu else 0.0
        child_cpu = (ru1.ru_utime + ru1.ru_stime) - (ru0.ru_utime + ru0.ru_stime)
        lat = sorted(x for ls in latss for x in ls)
        ncalls = len(lat)
        p50 = lat[len(lat) // 2]
        p99 = lat[min(len(lat) - 1, int(len(lat) * 0.99))]
        print(f"  procs={t:<2}   ({dec})  p50 {p50:7.1f} ms   p99 {p99:7.1f} ms   "
              f"client-util {child_cpu/wall:4.0%}   "
              f"cpu/call client {child_cpu*1000/ncalls:5.1f} ms"
              + (f" server {(srv1-srv0)*1000/ncalls:5.1f} ms" if server_cpu else ""))
        report["concurrency_procs"].append({
            "procs": t, "decoder": dec, "latency_ms_samples": [round(x, 1) for x in lat],
            "p50_ms": round(p50, 1), "p99_ms": round(p99, 1), "wall_s": round(wall, 2),
            "client_util": round(child_cpu / wall, 3),
            "client_cpu_ms_per_call": round(child_cpu * 1000 / ncalls, 2),
            "server_cpu_ms_per_call": (round((srv1 - srv0) * 1000 / ncalls, 2)
                                       if server_cpu else None),
        })

    out = Path(args.json) if args.json else Path("results/interface") / (
        "driver_memory_{}_{}.json".format(
            dec, datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1))
    print(f"\nwrote {out}")

    driver.close()


if __name__ == "__main__":
    main()
