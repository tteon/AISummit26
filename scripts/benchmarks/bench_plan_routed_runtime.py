#!/usr/bin/env python3
"""Route the Cypher runtime per query from its execution plan, and see if it pays.

The proposed process: seocho takes a question, generates Cypher, reviews the execution plan
once, and picks the runtime from a threshold. This measures that process against the three
fixed policies it would replace.

The thresholds are not guessed. Two sweeps produced them:

* leaf `EstimatedRows` separates the shapes that a runtime can do anything with. Point-anchored
  queries plan to 1 row and `parallel` is *slower* on them (0.78-0.89x); sweeps plan to 10^4-10^5
  rows and `parallel` is 4.2x — at one request in flight.
* the worker-limit sweep showed why that qualifier matters. One `parallel` scan occupies 10.66
  cores on an 8-core host, so the second concurrent request has nothing left. Capping the
  workers does not reclaim anything — throughput falls with the cap — and by four in flight
  `pipelined` wins on both throughput and CPU (124 vs 92 calls/s, 8.1 vs 15.5 cores).

So the rule has two inputs: a *static* one from the plan, and a *dynamic* one from how many
requests are in flight. Only the first can be memoised, which is the interesting part of the
design: the plan review is per template and amortises to nothing, while the concurrency test
has to happen per call.

Memoisation is sound here precisely because the grammar forbids inlined literals. Every query
is parameterised, so one template is one plan, and the plan review can be cached by the query
string. This benchmark reports the hit rate rather than assuming it.

Workload is mixed on purpose. Routing cannot beat a fixed policy on a uniform workload — the
fixed policy would simply be the right one. The mix is mostly anchored questions with a
minority of global aggregates, which is what an agent asking about a graph actually produces.

    python3 scripts/benchmarks/bench_plan_routed_runtime.py --password "$NEO4J_PASSWORD"
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import statistics
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

_spec2 = importlib.util.spec_from_file_location(
    "cpuutil", REPO_ROOT / "scripts" / "benchmarks" / "bench_cpu_parallel_util.py")
cpuutil = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(cpuutil)  # type: ignore[union-attr]

WS = "default"

# The question set, labelled by the shape its Cypher plans to. `sweep` questions are the ones
# the grammar admits and cannot forbid without also forbidding every global aggregate.
QUESTIONS: List[Dict[str, Any]] = [
    {"id": "incoming_count", "class": "point",
     "cypher": "MATCH (a:Account {acct_no:$a,_workspace_id:$ws})<-[t:TRANSFER]-"
               "(b:Account {_workspace_id:$ws}) RETURN count(t) AS n, sum(t.amount) AS total"},
    {"id": "two_hop_reach", "class": "point",
     "cypher": "MATCH (:Account {acct_no:$a,_workspace_id:$ws})-[:TRANSFER]->"
               "(b:Account {_workspace_id:$ws})-[t:TRANSFER]->(c:Account {_workspace_id:$ws}) "
               "RETURN count(DISTINCT c) AS reach, sum(t.amount) AS total"},
    {"id": "recent_page", "class": "point",
     "cypher": "MATCH (a:Account {acct_no:$a,_workspace_id:$ws})-[t:TRANSFER]->"
               "(b:Account {_workspace_id:$ws}) RETURN b.acct_no AS acct, t.amount AS amount, "
               "t.ts AS ts ORDER BY t.ts DESC LIMIT $limit"},
    {"id": "portfolio_risk", "class": "sweep",
     "cypher": "MATCH (a:Account {_workspace_id:$ws}) WHERE a.risk_tier > $tier "
               "RETURN count(a) AS n, avg(a._out_degree) AS deg"},
    {"id": "flagged_total", "class": "sweep",
     "cypher": "MATCH (a:Account {_workspace_id:$ws}) WHERE a._out_degree >= $tier "
               "RETURN count(a) AS n, max(a._out_degree) AS worst"},
]


class PlanRouter:
    """Reviews a query's plan once, then routes every later call on the cached verdict.

    The plan review is a real `EXPLAIN` round trip. It is charged to the run and reported, so
    the amortisation claim is measured rather than asserted."""

    def __init__(self, driver, database: str, *, sweep_rows: float, parallel_max_inflight: int,
                 default_runtime: str = "pipelined", big_runtime: str = "parallel",
                 inflight_metric: str = "sweep"):
        self.driver = driver
        self.database = database
        self.sweep_rows = sweep_rows
        self.parallel_max_inflight = parallel_max_inflight
        self.inflight_metric = inflight_metric
        self.default_runtime = default_runtime
        self.big_runtime = big_runtime
        self._plans: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.explain_ms_total = 0.0

    def review(self, cypher: str, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            cached = self._plans.get(cypher)
            if cached is not None:
                self.hits += 1
                return cached
        t0 = time.perf_counter()
        with self.driver.session(database=self.database) as s:
            res = s.run("EXPLAIN " + cypher, **params)
            list(res)
            plan = res.consume().plan
        took = (time.perf_counter() - t0) * 1000
        info = cpuutil_classify(plan, self.sweep_rows)
        with self._lock:
            self._plans[cypher] = info
            self.misses += 1
            self.explain_ms_total += took
        return info

    def route(self, cypher: str, params: Dict[str, Any], inflight: int,
              inflight_heavy: int) -> Tuple[str, Dict]:
        info = self.review(cypher, params)
        # Static half: a plan with nothing to parallelise gets the default, always.
        if not info["sweep_shaped"]:
            return self.default_runtime, info
        # Dynamic half, and the part that cannot be cached: `parallel` needs the whole machine,
        # so it is only correct while the machine is free. What occupies the machine is other
        # parallel queries, not the point lookups sharing the connection pool.
        gate = inflight_heavy if self.inflight_metric == "sweep" else inflight
        if gate < self.parallel_max_inflight:
            return self.big_runtime, info
        return self.default_runtime, info


def cpuutil_classify(plan: Any, sweep_rows: float) -> Dict[str, Any]:
    """Leaf estimated rows, and whether that makes the query a sweep.

    Deliberately not the operator name: the tenant scope is an indexed property, so a sweep of
    a whole label plans as `NodeIndexSeek` and a name-based test calls it a point lookup."""
    ops: List[Any] = []

    def walk(node: Any) -> None:
        if node is None:
            return
        ops.append(node)
        for ch in (node.get("children") or []):
            walk(ch)

    walk(plan)
    leaf_est = [float((o.get("args") or {}).get("EstimatedRows") or 0)
                for o in ops if not (o.get("children"))]
    leaf_max = max(leaf_est) if leaf_est else 0.0
    return {"leaf_estimated_rows_max": leaf_max, "sweep_shaped": leaf_max >= sweep_rows}


class Inflight:
    """Two counters, because they answer different questions.

    `n` is every request in flight. `heavy` is only those running on the parallel runtime, and
    that is the one that matters: the contended resource is the core pool a parallel scan takes
    whole, and a point-anchored query on the default runtime does not compete for it. Gating on
    total in-flight was measurably wrong — with a tenth of the questions planning to a sweep,
    four workers put 0.4 sweeps in flight on average, so the gate refused parallel for
    contention that was not happening, and routing lost to always-parallel by 1.3x."""

    def __init__(self) -> None:
        self.n = 0
        self.heavy = 0
        self._lock = threading.Lock()

    def enter(self) -> Tuple[int, int]:
        with self._lock:
            self.n += 1
            return self.n, self.heavy

    def leave(self) -> None:
        with self._lock:
            self.n -= 1

    def enter_heavy(self) -> None:
        with self._lock:
            self.heavy += 1

    def leave_heavy(self) -> None:
        with self._lock:
            self.heavy -= 1


def run_policy(driver, database: str, *, policy: str, questions: List[Dict[str, Any]],
               weights: List[float], workers: int, calls: int, params: Dict[str, Any],
               router: Optional[PlanRouter], seed: int,
               db_cgroup: Optional[Path]) -> Dict[str, Any]:  # noqa: C901
    rng = random.Random(seed)
    plan_seq = [rng.choices(questions, weights=weights, k=1)[0] for _ in range(calls)]
    inflight = Inflight()
    recs: List[Dict[str, Any]] = []
    lock = threading.Lock()
    routed_counts: Dict[str, int] = {}

    def one(i: int) -> None:
        q = plan_seq[i]
        n, heavy = inflight.enter()
        try:
            if policy == "routed":
                assert router is not None
                runtime, _info = router.route(q["cypher"], params, n, heavy)
            else:
                runtime = policy
            is_heavy = runtime == "parallel"
            if is_heavy:
                inflight.enter_heavy()
            try:
                t0 = time.perf_counter()
                with driver.session(database=database) as s:
                    list(s.run(f"CYPHER runtime={runtime} " + q["cypher"], **params))
                ms = (time.perf_counter() - t0) * 1000
            finally:
                if is_heavy:
                    inflight.leave_heavy()
        finally:
            inflight.leave()
        with lock:
            recs.append({"class": q["class"], "id": q["id"], "runtime": runtime, "ms": ms})
            routed_counts[f'{q["class"]}:{runtime}'] = \
                routed_counts.get(f'{q["class"]}:{runtime}', 0) + 1

    sampler = cpuutil.CpuSampler(None, db_cgroup)
    sampler.start()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, range(calls)))
    cpu = sampler.stop()

    def pct(vals: List[float], q: float) -> Optional[float]:
        if not vals:
            return None
        vals = sorted(vals)
        return round(vals[min(int(q * (len(vals) - 1)), len(vals) - 1)], 3)

    by_class: Dict[str, Any] = {}
    for cls in ("point", "sweep"):
        v = [r["ms"] for r in recs if r["class"] == cls]
        by_class[cls] = {"calls": len(v), "p50_ms": pct(v, 0.5), "p90_ms": pct(v, 0.9),
                         "p99_ms": pct(v, 0.99),
                         "mean_ms": round(statistics.mean(v), 3) if v else None}
    allms = [r["ms"] for r in recs]
    return {
        "policy": policy, "workers": workers, "calls": len(recs),
        "throughput_per_s": round(len(recs) / cpu["wall_s"], 2),
        "wall_s": cpu["wall_s"],
        "db_cores_busy": cpu.get("db_cores_busy"),
        "p50_ms": pct(allms, 0.5), "p90_ms": pct(allms, 0.9), "p99_ms": pct(allms, 0.99),
        "by_class": by_class,
        "routed_counts": routed_counts,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", default="bolt://localhost:7690")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", required=True)
    ap.add_argument("--database", default="finbenchl100")
    ap.add_argument("--container", default="aisummit-ent")
    ap.add_argument("--anchor", type=int, default=7)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--tier", type=int, default=1)
    ap.add_argument("--sweep-rows", type=float, default=1000.0)
    ap.add_argument("--parallel-max-inflight", type=int, default=2,
                    help="a sweep gets the parallel runtime only while fewer than this many "
                         "are already running there — measured, not guessed: one parallel scan "
                         "takes 10.66 cores of an 8-core host")
    ap.add_argument("--inflight-metric", choices=("sweep", "all"), default="sweep",
                    help="which in-flight count gates the parallel runtime. 'sweep' counts "
                         "only queries already on it, which is the contended resource; 'all' "
                         "counts every request and was measurably too conservative")
    ap.add_argument("--sweep-share", type=float, default=0.1,
                    help="fraction of questions that plan to a sweep")
    ap.add_argument("--workers", default="1,2,4,8")
    ap.add_argument("--calls", type=int, default=400)
    ap.add_argument("--warmup-calls", type=int, default=120)
    ap.add_argument("--seed", type=int, default=20260822)
    ap.add_argument("--policies", default="slotted,pipelined,parallel,routed")
    ap.add_argument("--suite", default=None,
                    help="load the workload from a FIBO suite yaml instead of the built-in "
                         "QUESTIONS: each gold query becomes a workload item with its declared "
                         "params, so the runtime router is measured on the same queries the "
                         "text2cypher experiments generate against")
    ap.add_argument("--out", default="results/bench/plan_routed_runtime.json")
    args = ap.parse_args()

    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    params = {"a": args.anchor, "ws": WS, "limit": args.limit, "tier": args.tier}
    questions = QUESTIONS
    if args.suite:
        import yaml
        suite = yaml.safe_load(Path(args.suite).read_text())
        ws_id = suite.get("workspace_id", WS)
        questions = []
        merged: Dict[str, Any] = {"workspace_id": ws_id}
        for q in suite["questions"]:
            gold = " ".join(str(q["gold"]).split())
            for k, v in (q.get("params") or {}).items():
                merged.setdefault(k, args.anchor if v == "anchor" else v)
            # The class is decided by the router's own plan review at run time; the label here
            # only sets the sweep-share weighting, so gold-labelled families keep their mix.
            questions.append({"id": q["id"],
                              "class": "sweep" if not q.get("in_subset", True) else "point",
                              "cypher": gold})
        params = merged
        params["acct_no"] = params.get("acct_no", args.anchor)
    db_cgroup = cpuutil.container_cgroup_cpu_path(args.container)

    n_sweep = sum(1 for q in questions if q["class"] == "sweep")
    n_point = len(questions) - n_sweep
    weights = [(args.sweep_share / n_sweep) if q["class"] == "sweep"
               else ((1 - args.sweep_share) / n_point) for q in questions]

    for i in range(args.warmup_calls):
        q = questions[i % len(questions)]
        with driver.session(database=args.database) as s:
            list(s.run(q["cypher"], **params))
    print(f"[bench] anchor={args.anchor} sweep_share={args.sweep_share} "
          f"threshold={args.sweep_rows} rows, parallel while inflight<={args.parallel_max_inflight}",
          flush=True)

    # What the router decides, printed once so the routing table is inspectable rather than
    # implied by the aggregate numbers.
    probe = PlanRouter(driver, args.database, sweep_rows=args.sweep_rows,
                       parallel_max_inflight=args.parallel_max_inflight,
                       inflight_metric=args.inflight_metric)
    routing_table = []
    for q in questions:
        info = probe.review(q["cypher"], params)
        lo, _ = probe.route(q["cypher"], params, 1, 0)
        hi, _ = probe.route(q["cypher"], params, 8, args.parallel_max_inflight)
        routing_table.append({"id": q["id"], "class": q["class"],
                              "leaf_estimated_rows_max": info["leaf_estimated_rows_max"],
                              "sweep_shaped": info["sweep_shaped"],
                              "runtime_when_free": lo, "runtime_when_saturated": hi})
        print(f"  {q['id']:16s} {q['class']:6s} leaf_rows={info['leaf_estimated_rows_max']:>10.0f} "
              f"-> free={lo:9s} saturated={hi}", flush=True)
    print(f"  plan review cost: {probe.explain_ms_total/max(probe.misses,1):.2f} ms per "
          f"template, {probe.misses} templates", flush=True)

    cells: List[Dict[str, Any]] = []
    router_stats: List[Dict[str, Any]] = []
    for w in [int(x) for x in args.workers.split(",")]:
        for policy in args.policies.split(","):
            router = None
            if policy == "routed":
                # A cold router, so its EXPLAIN round trips are inside the measured run.
                router = PlanRouter(driver, args.database, sweep_rows=args.sweep_rows,
                                    parallel_max_inflight=args.parallel_max_inflight,
                                    inflight_metric=args.inflight_metric)
            c = run_policy(driver, args.database, policy=policy, questions=questions,
                           weights=weights, workers=w, calls=args.calls, params=params,
                           router=router, seed=args.seed, db_cgroup=db_cgroup)
            if router is not None:
                total = router.hits + router.misses
                c["router"] = {
                    "templates_reviewed": router.misses,
                    "cache_hits": router.hits, "lookups": total,
                    "hit_rate": round(router.hits / total, 4) if total else None,
                    "explain_ms_total": round(router.explain_ms_total, 3),
                    "explain_ms_per_call": round(router.explain_ms_total / max(total, 1), 4),
                }
                router_stats.append(c["router"])
            cells.append(c)
            r = c.get("router") or {}
            print(f"  w={w:>2d} {policy:9s} {c['throughput_per_s']:>8.1f}/s "
                  f"p50={c['p50_ms']:>8.2f} p90={c['p90_ms']:>8.2f} p99={c['p99_ms']:>8.2f} "
                  f"point_p50={c['by_class']['point']['p50_ms']:>7.2f} "
                  f"sweep_p50={c['by_class']['sweep']['p50_ms']} "
                  f"db_cores={c['db_cores_busy']}"
                  f"{'  explain/call=' + str(r['explain_ms_per_call']) + 'ms' if r else ''}",
                  flush=True)
    driver.close()

    report = {
        "schema_version": "seocho.finbench.plan-routed-runtime.v1",
        "question": "does reviewing the plan once and routing the runtime beat a fixed policy?",
        "method": {
            "router": "EXPLAIN once per query template (memoised by query string, which is "
                      "sound because the grammar forbids inlined literals), classify by leaf "
                      "EstimatedRows, then pick by live in-flight count",
            "thresholds": "from two measured sweeps, not chosen: parallel is slower on "
                          "point-anchored plans, and one parallel scan occupies 10.66 cores so "
                          "it is only correct while the machine is free",
            "baselines": "the same mixed workload under each fixed runtime",
            "db_cpu": "container cgroup cpu.stat",
        },
        "manifest": runmeta.manifest(db_container=args.container),
        "config": {k: v for k, v in vars(args).items() if k != "password"},
        "questions": questions,
        "routing_table": routing_table,
        "plan_review_ms_per_template": round(probe.explain_ms_total / max(probe.misses, 1), 3),
        "cells": cells,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
