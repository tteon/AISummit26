#!/usr/bin/env python3
"""Can `runtime=parallel` be made usable under concurrency by capping its workers?

The runtime A/B left `parallel` looking like a latency optimisation that cannot be deployed:
4.2x on a sweep at one request in flight, and nothing by eight. The explanation was that it
takes every core on the first request, so there is none left for the second. If that is right,
then `server.cypher.parallel.worker_limit` should trade the single-query win for the ability to
keep the win under load — a smaller speedup that survives concurrency.

That knob is **not dynamic**, so each value needs its own server start. The container is
recreated per value against a persistent volume, which is why this is a script rather than a
sweep inside one process.

Reported against the same two shapes as the runtime A/B: `scan`, where parallel had something
to work with, and `fanout`, the shape our questions actually produce.

    python3 scripts/benchmarks/bench_parallel_worker_limit.py --password "$NEO4J_PASSWORD"
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "runmeta", REPO_ROOT / "scripts" / "analysis" / "runmeta.py")
runmeta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runmeta)  # type: ignore[union-attr]

_spec2 = importlib.util.spec_from_file_location(
    "cpuutil", REPO_ROOT / "scripts" / "benchmarks" / "bench_cpu_parallel_util.py")
cpuutil = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(cpuutil)  # type: ignore[union-attr]

WS = "default"
QUERIES = {
    "scan": ("MATCH (a:Account {_workspace_id:$ws}) WHERE a.risk_tier > $tier "
             "RETURN count(a) AS n, avg(a._out_degree) AS deg"),
    "fanout": ("MATCH (:Account {acct_no:$a,_workspace_id:$ws})-[:TRANSFER]->"
               "(b:Account {_workspace_id:$ws})-[t:TRANSFER]->(c:Account {_workspace_id:$ws}) "
               "RETURN count(DISTINCT c) AS reach, sum(t.amount) AS total"),
}


def start_server(container: str, image: str, volume: str, worker_limit: int,
                 bolt_port: int, http_port: int, password: str) -> None:
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    env = [
        "-e", f"NEO4J_AUTH=neo4j/{password}",
        "-e", "NEO4J_ACCEPT_LICENSE_AGREEMENT=eval",
        "-e", "NEO4J_server_memory_heap_initial__size=8G",
        "-e", "NEO4J_server_memory_heap_max__size=8G",
        "-e", "NEO4J_server_memory_pagecache_size=4G",
        # dots become underscores and underscores double, per the image's env convention.
        "-e", f"NEO4J_server_cypher_parallel_worker__limit={worker_limit}",
    ]
    subprocess.run(["docker", "run", "-d", "--name", container,
                    "-p", f"{bolt_port}:7687", "-p", f"{http_port}:7474",
                    "-v", f"{volume}:/data", *env, image],
                   check=True, capture_output=True)


def wait_ready(container: str, password: str, database: str, timeout_s: int = 120) -> bool:
    probe = ("PATH=$PATH:/var/lib/neo4j/bin; "
             f"cypher-shell -u neo4j -p {password} -d {database} 'RETURN 1;'")
    for _ in range(timeout_s):
        r = subprocess.run(["docker", "exec", container, "sh", "-c", probe],
                           capture_output=True)
        if r.returncode == 0:
            return True
        time.sleep(1)
    return False


def effective_limit(driver, database: str) -> Optional[str]:
    with driver.session(database=database) as s:
        rec = s.run("SHOW SETTINGS YIELD name, value "
                    "WHERE name = 'server.cypher.parallel.worker_limit' "
                    "RETURN value AS v").single()
    return rec["v"] if rec else None


def planner_runtime(driver, database: str, cypher: str, params: Dict[str, Any],
                    runtime: str) -> Optional[str]:
    """The runtime the planner actually chose — `parallel` declines per query, not per server."""
    with driver.session(database=database) as s:
        res = s.run(f"CYPHER runtime={runtime} EXPLAIN " + cypher, **params)
        list(res)
        plan = res.consume().plan or {}
    return ((plan.get("args") or {}).get("runtime"))


def measure(driver, database: str, cypher: str, params: Dict[str, Any], runtime: str,
            workers: int, calls_per_worker: int, db_cgroup: Optional[Path]) -> Dict[str, Any]:
    q = f"CYPHER runtime={runtime} " + cypher

    def one(_: int) -> List[float]:
        out = []
        for _ in range(calls_per_worker):
            t0 = time.perf_counter()
            with driver.session(database=database) as s:
                list(s.run(q, **params))
            out.append((time.perf_counter() - t0) * 1000)
        return out

    sampler = cpuutil.CpuSampler(None, db_cgroup)
    sampler.start()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        per = list(pool.map(one, range(workers)))
    cpu = sampler.stop()
    lat = sorted(t for c in per for t in c)
    return {
        "runtime": runtime, "workers": workers, "calls": len(lat),
        "throughput_per_s": round(len(lat) / cpu["wall_s"], 2),
        "p50_ms": round(lat[len(lat) // 2], 3),
        "p90_ms": round(lat[min(int(0.9 * (len(lat) - 1)), len(lat) - 1)], 3),
        "db_cores_busy": cpu.get("db_cores_busy"),
        "wall_s": cpu["wall_s"],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--password", required=True)
    ap.add_argument("--container", default="aisummit-ent")
    ap.add_argument("--image", default="neo4j:5.26-enterprise")
    ap.add_argument("--volume", default="aisummit-ent-data")
    ap.add_argument("--bolt-port", type=int, default=7690)
    ap.add_argument("--http-port", type=int, default=7475)
    ap.add_argument("--database", default="finbenchl100")
    ap.add_argument("--worker-limits", default="0,2,4,8")
    ap.add_argument("--runtimes", default="pipelined,parallel")
    ap.add_argument("--shapes", default="scan,fanout")
    ap.add_argument("--workers", default="1,2,4,8")
    ap.add_argument("--calls-per-worker", type=int, default=40)
    ap.add_argument("--warmup-calls", type=int, default=60)
    ap.add_argument("--anchor", type=int, default=7)
    ap.add_argument("--tier", type=int, default=1)
    ap.add_argument("--out", default="results/bench/parallel_worker_limit.json")
    args = ap.parse_args()

    from neo4j import GraphDatabase
    uri = f"bolt://localhost:{args.bolt_port}"
    params = {"a": args.anchor, "ws": WS, "tier": args.tier}
    cells: List[Dict[str, Any]] = []
    limits_seen: Dict[str, Any] = {}

    for wl in [int(x) for x in args.worker_limits.split(",")]:
        print(f"\n=== worker_limit={wl} (server restart) ===", flush=True)
        start_server(args.container, args.image, args.volume, wl,
                     args.bolt_port, args.http_port, args.password)
        if not wait_ready(args.container, args.password, args.database):
            print(f"[skip] server did not become ready for worker_limit={wl}", flush=True)
            continue
        # Re-resolve after every restart: the cgroup path contains the container id, so the
        # one resolved before the first `docker rm` is stale for every later value — which is
        # how the first run of this sweep came back with no database CPU at all.
        db_cgroup = cpuutil.container_cgroup_cpu_path(args.container)
        driver = GraphDatabase.driver(uri, auth=("neo4j", args.password))
        eff = effective_limit(driver, args.database)
        limits_seen[str(wl)] = eff
        print(f"  effective server.cypher.parallel.worker_limit = {eff}", flush=True)

        for shape in args.shapes.split(","):
            cypher = QUERIES[shape]
            # A fresh container has a cold page cache; without this the first cell of every
            # worker_limit measures the restart instead of the setting.
            for _ in range(args.warmup_calls):
                with driver.session(database=args.database) as s:
                    list(s.run(cypher, **params))
            for runtime in args.runtimes.split(","):
                chosen = planner_runtime(driver, args.database, cypher, params, runtime)
                downgraded = chosen is not None and str(chosen).lower() != runtime
                for w in [int(x) for x in args.workers.split(",")]:
                    c = measure(driver, args.database, cypher, params, runtime, w,
                                args.calls_per_worker, db_cgroup)
                    c.update({"worker_limit_requested": wl, "worker_limit_effective": eff,
                              "shape": shape, "planner_runtime": chosen,
                              "downgraded": downgraded})
                    cells.append(c)
                    print(f"  wl={wl:<2} {shape:7s} {runtime:9s} w={w:>2d} "
                          f"{c['throughput_per_s']:>8.1f}/s p50={c['p50_ms']:>8.2f}ms "
                          f"p90={c['p90_ms']:>8.2f}ms db_cores={c['db_cores_busy']}"
                          f"{'  DOWNGRADED' if downgraded else ''}", flush=True)
        driver.close()

    report = {
        "schema_version": "seocho.finbench.parallel-worker-limit.v1",
        "question": "does capping parallel's workers make it usable under concurrency?",
        "method": {
            "knob": "server.cypher.parallel.worker_limit — not dynamic, so one server start "
                    "per value against a persistent volume",
            "db_cpu": "container cgroup cpu.stat, so every process in the container counts",
            "downgrade_check": "planner runtime read from EXPLAIN per (runtime, shape)",
        },
        "manifest": runmeta.manifest(db_container=args.container),
        "config": {k: v for k, v in vars(args).items() if k != "password"},
        "queries": QUERIES,
        "worker_limits_effective": limits_seen,
        "cells": cells,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
