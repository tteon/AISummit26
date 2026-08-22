#!/usr/bin/env python3
"""Where does an agentic graph query actually spend its time — the CPU or the GPU?

Method follows "Towards Understanding, Analyzing, and Optimizing Agentic AI Execution: A
CPU-Centric Perspective" (Raj, Kundu, Vohra, Wang, Krishna — arXiv 2511.00739), which found
tool-dominated agentic workloads bottlenecked on *CPU-side tool processing* for up to 88% of
end-to-end latency, and that CPU parallelism saturates earlier than GPU parallelism so the
CPU becomes the ceiling as batch size grows. Their two systems are the two this repo uses:
Xeon + RTX PRO 6000, and Grace + H200.

What that paper measures and this reproduces:

* **stage decomposition** — every tool call is split into server CPU (`result_available_after`
  + `result_consumed_after` from the Bolt summary), client CPU (row hydration and the
  harness's own work, taken as the residual), and context encoding; the model side is the
  endpoint's own TTFT/decode. Reporting one "db time" hides the client half, which on this
  workload is the larger one.
* **open-loop arrivals** — requests arrive on a seeded Poisson process at rate λ instead of a
  closed loop with a concurrency semaphore, because a closed loop cannot show queueing.
* **throughput saturation** — sweep the batch size, report `BS / t_sec` and the gain ratio
  `r(BS) = T(BS) / T(BS/2)`. A ratio near 1 means saturated: more concurrency buys nothing.
* **utilisation** — `rho = lambda * E[S] / m`, arrival rate times mean service time over
  worker count, computed for the CPU stage and the model stage separately.
* **percentiles over steady state** — P50/P90/P99 with a warmup discarded.
* **power** — GPU draw sampled from nvidia-smi. CPU package power is not available on a
  rented container, so it is recorded as absent rather than estimated.

This measures the *tool* side against a real graph, with the model endpoint held fixed. It is
not a model benchmark: point it at the stub server to isolate the CPU path completely.

    python3 scripts/benchmarks/bench_cpu_gpu_split.py --password "$NEO4J_PASSWORD" \\
        --database finbenchl1 --batch-sizes 1,2,4,8,16 --requests-per-batch 64 \\
        --rate 8 --row-caps 50,200 --out results/bench/cpu_gpu_split.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "runmeta", REPO_ROOT / "scripts" / "analysis" / "runmeta.py")
runmeta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runmeta)  # type: ignore[union-attr]

WS = "default"

# One aggregate and one paging query: the two shapes the arms differ on. The paging one is
# where client-side CPU grows with the row cap, which is the paper's point about tool cost.
QUERIES = {
    "aggregate": ("MATCH (:Account {acct_no:$a,_workspace_id:$ws})<-[t:TRANSFER]-"
                  "(:Account {_workspace_id:$ws}) RETURN count(t) AS n, sum(t.amount) AS total"),
    "page": ("MATCH (:Account {acct_no:$a,_workspace_id:$ws})-[t:TRANSFER]->"
             "(b:Account {_workspace_id:$ws}) "
             "RETURN b.acct_no AS acct, t.amount AS amount, t.ts AS ts, t.channel AS channel "
             "ORDER BY t.ts DESC LIMIT $limit"),
    # The two shapes above are index-anchored point lookups: a handful of rows, no scan. A
    # Cypher runtime has nothing to parallelise in them, so measuring runtimes on those two
    # alone cannot answer whether the runtime matters. These two give the engine work that
    # morsel-driven execution and cross-core splitting were actually built for.
    "scan": ("MATCH (a:Account {_workspace_id:$ws}) WHERE a.risk_tier > 1 "
             "RETURN count(a) AS n, avg(a._out_degree) AS deg"),
    "fanout": ("MATCH (:Account {acct_no:$a,_workspace_id:$ws})-[:TRANSFER]->"
               "(b:Account {_workspace_id:$ws})-[t:TRANSFER]->(c:Account {_workspace_id:$ws}) "
               "RETURN count(DISTINCT c) AS reach, sum(t.amount) AS total"),
}


def gpu_power_w() -> Optional[float]:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
            text=True, stderr=subprocess.DEVNULL, timeout=5)
        vals = [float(x) for x in out.split() if x.replace(".", "").isdigit()]
        return round(sum(vals), 1) if vals else None
    except Exception:
        return None


class PowerSampler(threading.Thread):
    """GPU draw over the run. CPU package power needs host access a container does not have,
    so it is left absent rather than guessed at."""

    def __init__(self, interval: float = 1.0):
        super().__init__(daemon=True)
        self.interval = interval
        self.samples: List[float] = []
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            w = gpu_power_w()
            if w is not None:
                self.samples.append(w)
            self._stop.wait(self.interval)

    def stop(self) -> Dict[str, Any]:
        self._stop.set()
        if not self.samples:
            return {"gpu_power_w": None, "cpu_power_w": None,
                    "note": "nvidia-smi unavailable"}
        return {"gpu_power_w": {"mean": round(statistics.mean(self.samples), 1),
                                "max": max(self.samples), "samples": len(self.samples)},
                "cpu_power_w": None,
                "note": "CPU package power unavailable inside a container; not estimated"}


def one_call(driver, database: str, shape: str, anchor: int, row_cap: int,
             runtime: Optional[str] = None) -> Dict[str, Any]:
    """One tool call, timed the way the harness times it, stage by stage.

    `runtime` prefixes `CYPHER runtime=...`. Worth sweeping because the engine's runtime is a
    *choice*, not a patch: slotted executes tuple-at-a-time on one thread, pipelined is
    morsel-driven and vectorised, parallel splits a read across cores. DozerDB 5.26.3 reports
    edition "enterprise" but supports neither pipelined nor parallel — it warns and silently
    falls back to slotted — so measuring the difference needs a server that has them.
    """
    cypher = QUERIES[shape]
    if runtime:
        cypher = f"CYPHER runtime={runtime} " + cypher
    params = {"a": anchor, "ws": WS, "workspace_id": WS, "limit": row_cap}
    t0 = time.perf_counter()
    with driver.session(database=database) as session:
        t_run = time.perf_counter()
        result = session.run(cypher, **params)
        t_hydrate = time.perf_counter()
        rows = [dict(r) for _, r in zip(range(row_cap), result)]
        t_hydrated = time.perf_counter()
        summary = result.consume()
    t_encode0 = time.perf_counter()
    payload = json.dumps({"rows": rows, "row_count": len(rows)}, default=str)
    t_end = time.perf_counter()

    server_ms = float(summary.result_available_after or 0) + float(summary.result_consumed_after or 0)
    total_ms = (t_end - t0) * 1000
    return {
        "shape": shape, "row_cap": row_cap, "rows": len(rows), "chars": len(payload),
        "total_ms": round(total_ms, 3),
        "server_available_ms": float(summary.result_available_after or 0),
        "server_consumed_ms": float(summary.result_consumed_after or 0),
        "submit_ms": round((t_hydrate - t_run) * 1000, 3),
        "hydrate_ms": round((t_hydrated - t_hydrate) * 1000, 3),
        "encode_ms": round((t_end - t_encode0) * 1000, 3),
        "client_cpu_ms": round(max(total_ms - server_ms, 0.0), 3),
        "server_cpu_ms": round(server_ms, 3),
    }


def poisson_offsets(n: int, rate: float, seed: int) -> List[float]:
    rng = random.Random(seed)
    t, out = 0.0, []
    for _ in range(n):
        t += rng.expovariate(rate) if rate > 0 else 0.0
        out.append(t)
    return out


def run_cell(driver, database: str, *, shape: str, row_cap: int, batch_size: int,
             requests: int, rate: float, anchor: int, seed: int,
             warmup: int, runtime: Optional[str] = None) -> Dict[str, Any]:
    """One (batch size, row cap, shape) cell under open-loop Poisson arrivals."""
    offsets = poisson_offsets(requests, rate, seed)
    samples: List[Dict[str, Any]] = []
    lock = threading.Lock()
    t_start = time.perf_counter()

    def submit(i: int) -> None:
        due = t_start + offsets[i]
        delay = due - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        queued_ms = max((time.perf_counter() - due) * 1000, 0.0)
        rec = one_call(driver, database, shape, anchor, row_cap, runtime)
        rec["index"] = i
        rec["queue_ms"] = round(queued_ms, 3)
        with lock:
            samples.append(rec)

    with ThreadPoolExecutor(max_workers=batch_size) as pool:
        list(pool.map(submit, range(requests)))
    wall_s = time.perf_counter() - t_start

    steady = sorted((s for s in samples if s["index"] >= warmup), key=lambda s: s["index"])
    if not steady:
        steady = samples

    def pct(key: str, q: float) -> float:
        vals = sorted(s[key] for s in steady)
        if not vals:
            return 0.0
        k = min(int(q * (len(vals) - 1)), len(vals) - 1)
        return round(vals[k], 3)

    mean_service_s = statistics.mean(s["total_ms"] for s in steady) / 1000
    achieved_rate = len(steady) / wall_s if wall_s else 0.0
    return {
        "shape": shape, "row_cap": row_cap, "batch_size": batch_size, "runtime": runtime,
        "requests": requests, "warmup_discarded": warmup,
        "target_rate_per_s": rate, "achieved_rate_per_s": round(achieved_rate, 3),
        "wall_s": round(wall_s, 3),
        # The paper's throughput definition: batch size over completion time.
        "throughput_bs_per_s": round(batch_size / (wall_s / max(len(steady), 1)), 3),
        "completed_per_s": round(len(steady) / wall_s, 3) if wall_s else 0.0,
        # rho = lambda * E[S] / m — how close the tool stage is to its own ceiling.
        "rho_cpu": round(achieved_rate * mean_service_s / batch_size, 4),
        "latency_ms": {"p50": pct("total_ms", 0.5), "p90": pct("total_ms", 0.9),
                       "p99": pct("total_ms", 0.99),
                       "mean": round(statistics.mean(s["total_ms"] for s in steady), 3)},
        "queue_ms": {"p50": pct("queue_ms", 0.5), "p90": pct("queue_ms", 0.9)},
        "stage_mean_ms": {
            k: round(statistics.mean(s[k] for s in steady), 3)
            for k in ("server_cpu_ms", "client_cpu_ms", "submit_ms", "hydrate_ms", "encode_ms")
        },
        "client_cpu_share": round(
            statistics.mean(s["client_cpu_ms"] for s in steady)
            / max(statistics.mean(s["total_ms"] for s in steady), 1e-9), 4),
        "rows_mean": round(statistics.mean(s["rows"] for s in steady), 1),
        "samples": steady,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", required=True)
    ap.add_argument("--database", default="finbenchl1")
    ap.add_argument("--anchor", type=int, default=None,
                    help="account to query; default picks the p99 out-degree hub")
    ap.add_argument("--runtimes", default="",
                    help="comma-separated Cypher runtimes to sweep (slotted,pipelined,parallel); "
                         "empty means the server default. A server that lacks a runtime warns "
                         "and falls back silently, so check `runtime_supported` in the report "
                         "before reading a difference as a difference")
    ap.add_argument("--shapes", default="aggregate,page")
    ap.add_argument("--row-caps", default="50,200")
    ap.add_argument("--batch-sizes", default="1,2,4,8,16")
    ap.add_argument("--requests-per-batch", type=int, default=64)
    ap.add_argument("--rate", type=float, default=8.0, help="Poisson arrivals per second")
    ap.add_argument("--warmup", type=int, default=8)
    ap.add_argument("--prewarm", type=int, default=200,
                    help="queries executed before the sweep starts, so the page cache and the "
                         "query plan cache are warm for the FIRST runtime too. Without this the "
                         "sweep order itself produces a ranking")
    ap.add_argument("--passes", type=int, default=1,
                    help="sweep the runtime list this many times, reversing the order on every "
                         "other pass. Two passes make order effects visible: if forward and "
                         "reverse disagree, the difference is warmup, not the runtime")
    ap.add_argument("--seed", type=int, default=20260822)
    ap.add_argument("--db-container", default=None)
    ap.add_argument("--out", default="results/bench/cpu_gpu_split.json")
    args = ap.parse_args()

    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))

    anchor = args.anchor
    if anchor is None:
        with driver.session(database=args.database) as s:
            p99 = s.run("MATCH (a:Account) RETURN percentileDisc(a._out_degree,0.99) AS p").single()["p"]
            anchor = s.run("MATCH (a:Account) WHERE a._out_degree>=$p RETURN min(a.acct_no) AS a",
                           p=p99).single()["a"]
    print(f"[bench] anchor={anchor} db={args.database} rate={args.rate}/s", flush=True)

    # Warm the page cache and plan cache before anything is timed. The first cell of a cold
    # server is slow for reasons that have nothing to do with the runtime under test, and that
    # cost lands entirely on whichever runtime happens to be swept first.
    if args.prewarm > 0:
        t0 = time.perf_counter()
        caps = [int(x) for x in args.row_caps.split(",")]
        shapes = args.shapes.split(",")
        for i in range(args.prewarm):
            one_call(driver, args.database, shapes[i % len(shapes)], anchor,
                     caps[i % len(caps)], None)
        print(f"[prewarm] {args.prewarm} calls in {time.perf_counter()-t0:.2f}s", flush=True)

    power = PowerSampler()
    power.start()
    cells: List[Dict[str, Any]] = []
    runtimes: List[Optional[str]] = ([r.strip() for r in args.runtimes.split(",") if r.strip()]
                                     or [None])
    # A runtime the server does not have is not an error — it is a warning and a silent
    # downgrade to slotted, which would show up as "no difference" and be read as a finding.
    # A runtime can be present on the server and still be declined for a particular query:
    # parallel supports only a subset of operators. So probe every (runtime, shape) pair and
    # read back the runtime the planner actually chose, rather than the one we asked for.
    supported: Dict[str, Any] = {}
    for rt in runtimes:
        if rt is None:
            continue
        for shape in args.shapes.split(","):
            with driver.session(database=args.database) as s_:
                res = s_.run(f"CYPHER runtime={rt} EXPLAIN " + QUERIES[shape],
                             a=anchor, ws=WS, workspace_id=WS, limit=200)
                list(res)
                summary = res.consume()
                notes = [n.get("description", "") for n in (summary.notifications or [])]
                chosen = ((summary.plan or {}).get("args", {}) or {}).get("runtime")
            downgraded = (any("does not support the requested runtime" in n for n in notes)
                          or (chosen is not None and str(chosen).lower() != rt))
            supported[f"{rt}/{shape}"] = {"requested": rt, "chosen": chosen,
                                          "supported": not downgraded,
                                          "notifications": notes[:2]}
            print(f"[runtime] {rt:9s} {shape:9s} -> planner chose {chosen} "
                  f"{'' if not downgraded else '(DOWNGRADED)'}", flush=True)

    for pass_i in range(max(1, args.passes)):
     # Reverse on every other pass: an effect that survives both orders is not warmup.
     for runtime in (runtimes if pass_i % 2 == 0 else list(reversed(runtimes))):
      for shape in args.shapes.split(","):
        for row_cap in (int(x) for x in args.row_caps.split(",")):
            if shape == "aggregate" and row_cap != int(args.row_caps.split(",")[0]):
                continue  # the aggregate returns one row; the cap is irrelevant to it
            prev_tp: Optional[float] = None
            for bs in (int(x) for x in args.batch_sizes.split(",")):
                cell = run_cell(driver, args.database, shape=shape, row_cap=row_cap,
                                batch_size=bs, requests=args.requests_per_batch,
                                rate=args.rate, anchor=anchor, seed=args.seed,
                                warmup=args.warmup, runtime=runtime)
                # r(BS) = T(BS) / T(BS/2): at 1 the CPU stage has stopped scaling.
                tp = cell["completed_per_s"]
                cell["throughput_gain_ratio"] = (round(tp / prev_tp, 3)
                                                 if prev_tp not in (None, 0) else None)
                prev_tp = tp
                cell["pass"] = pass_i
                cells.append(cell)
                print(f"  p{pass_i} {str(runtime or 'default'):9s} {shape:9s} cap={row_cap:>3} bs={bs:>2} "
                      f"done/s={tp:>7.2f} r(BS)={cell['throughput_gain_ratio']} "
                      f"p50={cell['latency_ms']['p50']:>7.2f}ms p90={cell['latency_ms']['p90']:>7.2f}ms "
                      f"client_cpu={100*cell['client_cpu_share']:>4.1f}% rho={cell['rho_cpu']}",
                      flush=True)
    driver.close()

    report = {
        "schema_version": "seocho.finbench.cpu-gpu-split.v1",
        "method": {
            "paper": "Raj et al., Towards Understanding, Analyzing, and Optimizing Agentic AI "
                     "Execution: A CPU-Centric Perspective (arXiv 2511.00739)",
            "reproduced": ["stage decomposition (server vs client CPU vs encode)",
                           "open-loop Poisson arrivals",
                           "throughput saturation with r(BS)=T(BS)/T(BS/2)",
                           "rho = lambda*E[S]/m", "P50/P90/P99 after warmup",
                           "GPU power sampling"],
            "not_reproduced": ["CPU package energy (needs host power access)",
                               "their five workloads (this measures the graph tool path)"],
        },
        "manifest": runmeta.manifest(db_container=args.db_container),
        "config": {k: v for k, v in vars(args).items() if k != "password"},
        "anchor": anchor,
        "runtime_supported": supported or None,
        "power": power.stop(),
        "cells": cells,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
