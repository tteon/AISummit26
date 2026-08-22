#!/usr/bin/env python3
"""How much of the CPU can the graph path actually use, and what stops it?

The runtime A/B settled where the CPU goes: on our query shapes the Cypher engine spends
0.4-0.7 ms per call and does not move with concurrency, while the client side grows from 0.6
to 3.9 ms as batch size goes 1 -> 8, taking 46% -> 94% of the call. Choosing a different engine
or a different Cypher runtime cannot fix that. So the question becomes the one this measures:
**how many cores does the exchange actually keep busy, and which lever raises it?**

Throughput alone cannot answer that, because it hides two different failures behind the same
number: work that will not parallelise, and work that parallelises onto cores something else
already owns. So this samples `utime+stime` from `/proc` per process and reports **cores
busy** for the client and the database separately. On this host that ceiling is 8 physical
cores (16 threads), and the database container shares them with the client unless pinned --- a
confound in every CPU number taken before this.

Four levers, each a hypothesis about what caps utilisation:

* **threads vs processes** — Python hydrates rows under the GIL. If threads plateau where
  processes keep scaling, the GIL is the ceiling and no amount of Cypher tuning moves it.
* **core affinity** — client and database pinned to disjoint core sets, so contention stops
  being invisible. Costs each side half the machine; the question is whether it is still net
  positive.
* **payload shape** — four properties per row, one property per row, or aggregated
  server-side to a single row. This does not parallelise the work, it deletes it.
* **fetch size and row materialisation** — how many PULL round trips, and whether each record
  becomes a Python dict at all.
* **the Rust wire codec** — `neo4j-rust-ext` is installed and active here, so every client CPU
  number this project has recorded was already taken *with* it. What it buys was never
  measured. It replaces `pack`/`unpack` in `neo4j._codec.packstream.v1` by importing
  `neo4j._rust`, so blocking that one import in a subprocess gives the pure-Python codec and
  the comparison the claim needs. Both arms run out-of-process for exactly that reason.

    python3 scripts/benchmarks/bench_cpu_parallel_util.py --password "$NEO4J_PASSWORD" \
        --database finbenchl100 --db-container aisummit-simtest
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import multiprocessing as mp
import os
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "runmeta", REPO_ROOT / "scripts" / "analysis" / "runmeta.py")
runmeta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runmeta)  # type: ignore[union-attr]

TICKS = os.sysconf("SC_CLK_TCK")
WS = "default"

# Same anchored two-hop question, asked three ways. `wide` is what the harness actually sends;
# `narrow` returns a quarter of the properties; `server_agg` moves the aggregation into Cypher
# so one row comes back instead of two hundred. The Cypher differs, the answer does not.
SHAPES: Dict[str, str] = {
    "wide": ("MATCH (a:Account {acct_no:$a,_workspace_id:$ws})-[t:TRANSFER]->"
             "(b:Account {_workspace_id:$ws}) "
             "RETURN b.acct_no AS acct, t.amount AS amount, t.ts AS ts, t.channel AS channel "
             "ORDER BY t.ts DESC LIMIT $limit"),
    "narrow": ("MATCH (a:Account {acct_no:$a,_workspace_id:$ws})-[t:TRANSFER]->"
               "(b:Account {_workspace_id:$ws}) "
               "RETURN b.acct_no AS acct ORDER BY t.ts DESC LIMIT $limit"),
    "server_agg": ("MATCH (a:Account {acct_no:$a,_workspace_id:$ws})-[t:TRANSFER]->"
                   "(b:Account {_workspace_id:$ws}) "
                   "RETURN count(t) AS n, sum(t.amount) AS total, max(t.ts) AS latest"),
}


def proc_cpu_s(pid: int) -> Optional[float]:
    """Process CPU seconds, summed over every thread the process owns."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
        # comm can contain spaces and parentheses, so fields are counted from the last ')'.
        after = raw[raw.rindex(b")") + 2:].split()
        return (int(after[11]) + int(after[12])) / TICKS
    except Exception:
        return None


def container_cgroup_cpu_path(container: Optional[str]) -> Optional[Path]:
    """The cgroup that accounts for *every* process in the container.

    Reading `/proc/<container main pid>/stat` was wrong and reported the database as using
    zero cores: the container's main process is the entrypoint, and the JVM doing the work is
    its child. cgroup v2 accounts for the whole container in one place.
    """
    if not container:
        return None
    out = subprocess.run(["docker", "inspect", "--format", "{{.Id}}", container],
                         capture_output=True, text=True)
    cid = out.stdout.strip()
    if not cid:
        return None
    for cand in (Path(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/cpu.stat"),
                 Path(f"/sys/fs/cgroup/docker/{cid}/cpu.stat")):
        if cand.is_file():
            return cand
    return None


def cgroup_cpu_s(path: Optional[Path]) -> Optional[float]:
    if not path:
        return None
    try:
        for line in path.read_text().splitlines():
            if line.startswith("usage_usec"):
                return int(line.split()[1]) / 1e6
    except Exception:
        return None
    return None


class CpuSampler:
    """Cores busy over a window, for this process and for the database container.

    The database side is read from its cgroup rather than a pid, because the JVM is a child of
    the container's entrypoint. Measuring it matters because on this host its cores are the
    client's cores: without pinning, the two compete for the same eight."""

    def __init__(self, self_pid: Optional[int], db_cgroup: Optional[Path]):
        self.self_pid = self_pid
        self.db_cgroup = db_cgroup
        self.t0 = 0.0
        self.c0: Optional[float] = None
        self.d0: Optional[float] = None

    def start(self) -> None:
        self.t0 = time.perf_counter()
        self.c0 = proc_cpu_s(self.self_pid) if self.self_pid else None
        self.d0 = cgroup_cpu_s(self.db_cgroup)

    def stop(self) -> Dict[str, Any]:
        wall = max(time.perf_counter() - self.t0, 1e-9)
        out: Dict[str, Any] = {"wall_s": round(wall, 4)}
        c1 = proc_cpu_s(self.self_pid) if self.self_pid else None
        if self.c0 is not None and c1 is not None:
            out["client_cpu_s"] = round(c1 - self.c0, 4)
            out["client_cores_busy"] = round((c1 - self.c0) / wall, 3)
        d1 = cgroup_cpu_s(self.db_cgroup)
        if self.d0 is not None and d1 is not None:
            out["db_cpu_s"] = round(d1 - self.d0, 4)
            out["db_cores_busy"] = round((d1 - self.d0) / wall, 3)
        return out


def make_driver(uri: str, user: str, password: str):
    from neo4j import GraphDatabase
    return GraphDatabase.driver(uri, auth=(user, password))


def run_calls(driver, database: str, cypher: str, params: Dict[str, Any], n: int,
              fetch_size: Optional[int], consume: str) -> List[float]:
    """`n` sequential calls on one connection. Returns per-call wall times in ms."""
    kw = {} if fetch_size is None else {"fetch_size": fetch_size}
    times: List[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        with driver.session(database=database, **kw) as s:
            res = s.run(cypher, **params)
            if consume == "dict":
                rows = [dict(r) for r in res]          # what the harness does
            elif consume == "data":
                rows = res.data()                       # driver-side dict building
            elif consume == "values":
                rows = [r.values() for r in res]        # tuples, no key hashing
            elif consume == "discard":
                rows = None                             # server still computes and sends
            res.consume()
        times.append((time.perf_counter() - t0) * 1000)
    return times


_CODEC_RUNNER = r"""
import json, sys, time

if %(block)r:
    class _Block:
        # The driver selects its codec with a bare `try: import neo4j._rust`, so denying that
        # one import is the whole switch. Has to happen before neo4j is imported at all, which
        # is why this arm is a subprocess rather than a flag.
        def find_spec(self, name, path=None, target=None):
            if name == "neo4j._rust" or name.startswith("neo4j._rust."):
                raise ImportError("blocked to measure the pure-Python codec")
            return None
    sys.meta_path.insert(0, _Block())

from concurrent.futures import ThreadPoolExecutor
from neo4j import GraphDatabase
from neo4j._codec.packstream import RUST_AVAILABLE

import resource
def _cpu():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime

cfg = json.loads(sys.argv[1])
driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))

def one_worker(_):
    out = []
    for _ in range(cfg["calls"]):
        t0 = time.perf_counter()
        with driver.session(database=cfg["database"]) as s:
            res = s.run(cfg["cypher"], **cfg["params"])
            rows = [dict(r) for r in res]
            res.consume()
        out.append((time.perf_counter() - t0) * 1000)
    return out

for _ in range(20):          # warm the connection pool before the interpreter start stops counting
    with driver.session(database=cfg["database"]) as s:
        list(s.run(cfg["cypher"], **cfg["params"]))
cpu0 = _cpu()
t0 = time.perf_counter()
with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
    per = list(pool.map(one_worker, range(cfg["workers"])))
wall = time.perf_counter() - t0
cpu_s = _cpu() - cpu0
driver.close()
print(json.dumps({"rust_available": RUST_AVAILABLE, "wall_s": wall, "cpu_s": cpu_s,
                  "latencies_ms": [t for c in per for t in c]}))
"""


def codec_cell(*, conn: Dict[str, str], database: str, cypher: str, params: Dict[str, Any],
               workers: int, calls_per_worker: int, block_rust: bool,
               db_cgroup: Optional[Path]) -> Dict[str, Any]:
    """One arm of the Rust-vs-Python codec comparison, run out of process."""
    cfg = {"uri": conn["uri"], "user": conn["user"], "password": conn["password"],
           "database": database, "cypher": cypher, "params": params,
           "workers": workers, "calls": calls_per_worker}
    runner = _CODEC_RUNNER % {"block": block_rust}
    sampler = CpuSampler(None, db_cgroup)
    sampler.start()
    proc = subprocess.run([sys.executable, "-c", runner, json.dumps(cfg)],
                          capture_output=True, text=True)
    cpu = sampler.stop()
    if proc.returncode != 0:
        return {"codec": "python" if block_rust else "rust", "ok": False,
                "error": (proc.stderr or "")[-400:]}
    res = json.loads(proc.stdout.strip().splitlines()[-1])
    lat = sorted(res["latencies_ms"])
    # The child's own rusage over its timed section — the parent's wall clock includes the
    # interpreter start, which is not part of what is being compared.
    cpu["wall_s"] = round(res["wall_s"], 4)
    cpu["client_cpu_s"] = round(res["cpu_s"], 4)
    cpu["client_cores_busy"] = round(res["cpu_s"] / max(res["wall_s"], 1e-9), 3)
    return {
        "codec": "python" if block_rust else "rust",
        "rust_available_in_child": res["rust_available"], "ok": True,
        "shape": "wide", "mode": "subprocess-threads", "workers": workers,
        "consume": "dict", "fetch_size": None, "calls": len(lat),
        "throughput_per_s": round(len(lat) / res["wall_s"], 2),
        "p50_ms": round(lat[len(lat) // 2], 3),
        "p90_ms": round(lat[min(int(0.9 * (len(lat) - 1)), len(lat) - 1)], 3),
        "cpu": cpu,
        "cores_busy_total": (None if cpu.get("db_cores_busy") is None
                             else round(cpu["client_cores_busy"] + cpu["db_cores_busy"], 3)),
    }


def _self_cpu_s() -> float:
    import resource
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def _proc_worker(args: Tuple[Any, ...]) -> Dict[str, Any]:
    """One worker process. It reports its own CPU around the timed section only.

    Charging `RUSAGE_CHILDREN` from the parent was wrong: with `spawn`, every child pays a
    fresh interpreter start and a full `neo4j` import, and at these call counts that startup
    dominated — process mode appeared to burn 4.6 cores to serve one worker."""
    uri, user, password, database, cypher, params, n, fetch_size, consume = args
    d = make_driver(uri, user, password)
    try:
        cpu0 = _self_cpu_s()
        t0 = time.perf_counter()
        lat = run_calls(d, database, cypher, params, n, fetch_size, consume)
        return {"latencies_ms": lat, "cpu_s": _self_cpu_s() - cpu0,
                "wall_s": time.perf_counter() - t0}
    finally:
        d.close()


def _init_worker(barrier: Any) -> None:
    """Pool initializer: import the driver, then wait for every sibling to do the same.

    Warming with `pool.map` of no-op tasks does **not** work and quietly wrecked this
    measurement. `Pool.map` hands tasks to whichever worker is free, so with 16 trivial tasks
    the first worker up can take several and the rest are still spawning when sampling starts —
    their interpreter start, `neo4j` import and module re-exec then land inside the timed
    window. Process mode measured 2,376 calls/s that way against 7,130 for the same work with
    the workers genuinely up: a 3x error, all of it startup. A barrier in the initializer is
    deterministic, because Pool runs the initializer once per worker at construction and every
    worker must arrive before any task runs.
    """
    from neo4j import GraphDatabase  # noqa: F401 — paid here, not in the timed section
    barrier.wait()


def cell(*, conn: Dict[str, str], database: str, shape: str, workers: int, mode: str,
         calls_per_worker: int, fetch_size: Optional[int], consume: str,
         params: Dict[str, Any], db_cgroup: Optional[Path]) -> Dict[str, Any]:
    cypher = SHAPES[shape]

    if mode == "threads":
        sampler = CpuSampler(os.getpid(), db_cgroup)
        driver = make_driver(conn["uri"], conn["user"], conn["password"])
        try:
            sampler.start()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                per = list(pool.map(
                    lambda _: run_calls(driver, database, cypher, params,
                                        calls_per_worker, fetch_size, consume),
                    range(workers)))
        finally:
            cpu = sampler.stop()
            driver.close()
        lat = sorted(t for chunk in per for t in chunk)
    else:
        payload = (conn["uri"], conn["user"], conn["password"], database, cypher, params,
                   calls_per_worker, fetch_size, consume)
        ctx = mp.get_context("spawn")
        # Every worker has to be alive *and* have imported the driver before sampling starts,
        # so the pool's initializer holds a barrier the whole cohort must reach.
        barrier = ctx.Barrier(workers)
        with ctx.Pool(processes=workers, initializer=_init_worker,
                      initargs=(barrier,)) as pool:
            # One real round of work per worker as well: the barrier proves the interpreters
            # are up, this proves each has a live connection.
            pool.map(_proc_worker, [(conn["uri"], conn["user"], conn["password"], database,
                                     cypher, params, 5, fetch_size, consume)] * workers)
            sampler = CpuSampler(None, db_cgroup)
            sampler.start()
            results = pool.map(_proc_worker, [payload] * workers)
            cpu = sampler.stop()
        child_cpu = sum(r["cpu_s"] for r in results)
        cpu["client_cpu_s"] = round(child_cpu, 4)
        cpu["client_cores_busy"] = round(child_cpu / cpu["wall_s"], 3)
        lat = sorted(t for r in results for t in r["latencies_ms"])
    total = len(lat)
    return {
        "shape": shape, "mode": mode, "workers": workers, "consume": consume,
        "fetch_size": fetch_size, "calls": total,
        "throughput_per_s": round(total / cpu["wall_s"], 2),
        "p50_ms": round(lat[len(lat) // 2], 3),
        "p90_ms": round(lat[min(int(0.9 * (len(lat) - 1)), len(lat) - 1)], 3),
        "cpu": cpu,
        # The headline: cores kept busy on each side, and their sum against the physical count.
        "cores_busy_total": (None if cpu.get("client_cores_busy") is None
                             or cpu.get("db_cores_busy") is None
                             else round(cpu["client_cores_busy"] + cpu["db_cores_busy"], 3)),
    }


def set_cpuset(container: Optional[str], cpus: Optional[str]) -> None:
    if not container:
        return
    subprocess.run(["docker", "update", "--cpuset-cpus", cpus or "", container],
                   check=False, capture_output=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", default="bolt://localhost:7688")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", required=True)
    ap.add_argument("--database", default="finbenchl100")
    ap.add_argument("--anchor", type=int, default=None)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--workers", default="1,2,4,8,16")
    ap.add_argument("--calls-per-worker", type=int, default=200)
    ap.add_argument("--warmup-calls", type=int, default=300)
    ap.add_argument("--db-container", default="aisummit-simtest")
    ap.add_argument("--client-cpus", default="0-3",
                    help="core set for the client in the pinned pass")
    ap.add_argument("--db-cpus", default="4-7",
                    help="core set for the database in the pinned pass")
    ap.add_argument("--skip-pinned", action="store_true")
    ap.add_argument("--out", default="results/bench/cpu_parallel_util.json")
    args = ap.parse_args()

    conn = {"uri": args.uri, "user": args.user, "password": args.password}
    db_cgroup = container_cgroup_cpu_path(args.db_container)
    if args.db_container and db_cgroup is None:
        print("[warn] container cgroup cpu.stat not found — database CPU will be absent "
              "rather than guessed", flush=True)

    driver = make_driver(**conn)
    anchor = args.anchor
    if anchor is None:
        with driver.session(database=args.database) as s:
            p99 = s.run("MATCH (a:Account) RETURN percentileDisc(a._out_degree,0.99) AS p"
                        ).single()["p"]
            anchor = s.run("MATCH (a:Account) WHERE a._out_degree>=$p "
                           "RETURN min(a.acct_no) AS a", p=p99).single()["a"]
    params = {"a": anchor, "ws": WS, "limit": args.limit}
    # Warm the page cache and the plan cache before anything is timed, for the same reason the
    # runtime sweep needed it: otherwise the first cell measures the cold start.
    for i in range(args.warmup_calls):
        run_calls(driver, args.database, SHAPES["wide" if i % 2 else "server_agg"],
                  params, 1, None, "dict")
    driver.close()
    print(f"[bench] anchor={anchor} db={args.database} db_cgroup={db_cgroup} "
          f"cores={os.cpu_count()}", flush=True)

    worker_counts = [int(x) for x in args.workers.split(",")]
    cells: List[Dict[str, Any]] = []

    def add(**kw: Any) -> None:
        c = cell(conn=conn, database=args.database, params=params, db_cgroup=db_cgroup, **kw)
        c["pinned"] = kw.get("_pinned", False)
        cells.append(c)
        cpu = c["cpu"]
        print(f"  {c['mode']:9s} w={c['workers']:>2d} {c['shape']:10s} "
              f"consume={c['consume']:7s} fetch={str(c['fetch_size']):6s} "
              f"{c['throughput_per_s']:>8.1f}/s p50={c['p50_ms']:>7.2f}ms "
              f"client={cpu.get('client_cores_busy')} db={cpu.get('db_cores_busy')} "
              f"total={c['cores_busy_total']}", flush=True)

    # 1. The GIL question: does thread scaling stop where process scaling does not?
    print("\n[1] threads vs processes (shape=wide, consume=dict)", flush=True)
    for mode in ("threads", "processes"):
        for w in worker_counts:
            add(shape="wide", workers=w, mode=mode, consume="dict",
                fetch_size=None, calls_per_worker=args.calls_per_worker)

    # 2. Payload shape: deleting work rather than parallelising it.
    print("\n[2] payload shape (threads, consume=dict)", flush=True)
    for shape in ("wide", "narrow", "server_agg"):
        for w in (4, 8):
            add(shape=shape, workers=w, mode="threads", consume="dict",
                fetch_size=None, calls_per_worker=args.calls_per_worker)

    # 3. What materialising a row costs, holding the query fixed.
    print("\n[3] row materialisation (threads, shape=wide, w=8)", flush=True)
    for consume in ("dict", "data", "values", "discard"):
        add(shape="wide", workers=8, mode="threads", consume=consume,
            fetch_size=None, calls_per_worker=args.calls_per_worker)

    # 4. PULL batching.
    print("\n[4] fetch size (threads, shape=wide, w=8)", flush=True)
    for fs in (100, 1000, -1):
        add(shape="wide", workers=8, mode="threads", consume="dict",
            fetch_size=fs, calls_per_worker=args.calls_per_worker)

    # 5. The Rust codec, finally measured rather than assumed. Both arms are subprocesses so
    #    the comparison is symmetric — the blocked arm cannot be an in-process special case.
    print("\n[5] wire codec: rust vs pure python (subprocess, shape=wide)", flush=True)
    codec_cells: List[Dict[str, Any]] = []
    for w in (1, 4, 8):
        for block in (False, True):
            c = codec_cell(conn=conn, database=args.database, cypher=SHAPES["wide"],
                           params=params, workers=w, calls_per_worker=args.calls_per_worker,
                           block_rust=block, db_cgroup=db_cgroup)
            codec_cells.append(c)
            cells.append(c)
            if not c.get("ok"):
                print(f"  codec={c['codec']:6s} w={w} FAILED {c.get('error','')[:120]}",
                      flush=True)
                continue
            cpu = c["cpu"]
            print(f"  codec={c['codec']:6s} w={w:>2d} rust_in_child={c['rust_available_in_child']} "
                  f"{c['throughput_per_s']:>8.1f}/s p50={c['p50_ms']:>7.2f}ms "
                  f"client={cpu.get('client_cores_busy')} db={cpu.get('db_cores_busy')}",
                  flush=True)

    # 6. Stop client and database from sharing cores. Each side loses half the machine, so
    #    this is only a win if contention was costing more than the halving does.
    pinned: List[Dict[str, Any]] = []
    if not args.skip_pinned and args.db_container:
        print(f"\n[6] pinned: client -> {args.client_cpus}, db -> {args.db_cpus}", flush=True)
        set_cpuset(args.db_container, args.db_cpus)
        try:
            os.sched_setaffinity(0, _parse_cpus(args.client_cpus))
            for mode in ("threads", "processes"):
                for w in (4, 8):
                    before = len(cells)
                    add(shape="wide", workers=w, mode=mode, consume="dict",
                        fetch_size=None, calls_per_worker=args.calls_per_worker)
                    cells[before]["pinned"] = True
                    pinned.append(cells[before])
        finally:
            os.sched_setaffinity(0, set(range(os.cpu_count() or 1)))
            set_cpuset(args.db_container, "")

    report = {
        "schema_version": "seocho.finbench.cpu-parallel-util.v1",
        "question": "how many cores does the agent<->graph exchange keep busy, and what caps it?",
        "method": {
            "cores_busy": "utime+stime from /proc per process, divided by wall time; process "
                          "mode reads reaped-children rusage because the work is not in the "
                          "parent's counters",
            "ceiling": f"{os.cpu_count()} hardware threads on "
                       f"{runmeta.manifest().get('cpu_model')}, physical cores are half that",
            "confound_removed": "the pinned pass gives client and database disjoint core sets; "
                                "unpinned they share every core",
        },
        "manifest": runmeta.manifest(db_container=args.db_container),
        "config": {k: v for k, v in vars(args).items() if k != "password"},
        "anchor": anchor,
        "shapes": SHAPES,
        "cells": cells,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"\nwrote {out}")


def _parse_cpus(spec: str) -> set:
    cpus: set = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            cpus.update(range(int(a), int(b) + 1))
        elif part.strip():
            cpus.add(int(part))
    return cpus


if __name__ == "__main__":
    main()
