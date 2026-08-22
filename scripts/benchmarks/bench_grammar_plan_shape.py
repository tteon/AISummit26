#!/usr/bin/env python3
"""Does the ontology-derived grammar stop text2cypher from writing a scan?

The runtime A/B found that a Cypher scan costs 4.2x more on DozerDB than it would on an
engine with the parallel runtime, while an index-anchored query costs the same on both. That
makes "never emit a scan" a performance property of the *generator*, not of the engine — and
the grammar is the only thing between the model and the query. So: does it hold?

Two questions, answered two different ways, because sampling a decoder cannot prove a
negative:

1. **What the grammar admits.** `xgrammar` — the same engine vLLM constrains with — compiles
   the EBNF and decides membership exactly (`accept_string` + `is_completed`). If a
   scan-shaped query is a member, the grammar permits scans no matter what the model happened
   to emit. That is a proof, not a measurement.
2. **What the plans actually are.** Every query the two arms generated, plus the probes, run
   through `EXPLAIN` on the real graph; the plan's leaf operators say whether the engine seeks
   an index or scans a label, and the estimated rows say how much that costs. `PROFILE` gives
   db hits.

The distinction that matters here is *where the anchor goes*. Grammar mode puts it in a WHERE
clause, because `node ::= "(" var? label_part scope ")"` admits only the tenant scope inside
the node map. JSON mode inlines it in the map. Those are the same query semantically and can
plan very differently, so this also checks whether the grammar's node rule is costing us the
seek.

    python3 scripts/benchmarks/bench_grammar_plan_shape.py --password "$NEO4J_PASSWORD" \
        --uri bolt://localhost:7690 --database finbenchl100
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "runmeta", REPO_ROOT / "scripts" / "analysis" / "runmeta.py")
runmeta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runmeta)  # type: ignore[union-attr]

from harness.cypher_grammar import covers, grammar_from_policy  # noqa: E402
from harness.seocho_bridge import _ensure_seocho_on_path  # noqa: E402

# Leaf operators that read a whole label or the whole store. These are the shape the runtime
# A/B showed costs 4.2x on an engine without the parallel runtime.
SCAN_OPS = ("AllNodesScan", "NodeByLabelScan", "DirectedAllRelationshipsScan",
            "UndirectedAllRelationshipsScan", "DirectedRelationshipTypeScan",
            "UndirectedRelationshipTypeScan")
SEEK_OPS = ("NodeIndexSeek", "NodeUniqueIndexSeek", "NodeIndexSeekByRange",
            "NodeIndexContainsScan", "NodeIndexEndsWithScan", "NodeByIdSeek",
            "NodeByElementIdSeek")
# An index scan reads the whole index: cheaper than a label scan, still every row.
INDEX_SCAN_OPS = ("NodeIndexScan", "DirectedRelationshipIndexScan",
                  "UndirectedRelationshipIndexScan")


def walk(plan: Any) -> List[Dict[str, Any]]:
    """Flatten a Bolt plan tree into its operator list."""
    if plan is None:
        return []
    out = [plan]
    for child in (plan.get("children") or []):
        out.extend(walk(child))
    return out


def classify(plan: Any, sweep_rows: float) -> Dict[str, Any]:
    ops = walk(plan)
    names = [str(o.get("operatorType") or "").split("@")[0] for o in ops]
    leaves = [n for n in names if not (ops[names.index(n)].get("children"))]
    scans = [n for n in names if any(n.startswith(s) for s in SCAN_OPS)]
    seeks = [n for n in names if any(n.startswith(s) for s in SEEK_OPS)]
    idx_scans = [n for n in names if any(n.startswith(s) for s in INDEX_SCAN_OPS)]
    # The planner's own row estimate on the leaf that starts the query: the number that
    # decides whether this is a point lookup or a sweep.
    est = []
    for o in ops:
        args = o.get("args") or {}
        if not (o.get("children")):
            est.append(float(args.get("EstimatedRows") or 0))
    leaf_max = max(est) if est else None
    # Operator names lie here, and it took a wrong verdict to notice. The tenant scope
    # `{_workspace_id: $workspace_id}` is an indexed property, so a query that reads every
    # Account in the workspace plans as `NodeIndexSeek` — a *seek* by name, 200,470 rows by
    # cost. Classifying on the operator name therefore reports "no scans" for a query with
    # 6.4M db hits. What matters is how many rows the leaf touches, so that is the criterion;
    # the name-based flag is kept only to show the two disagree.
    return {
        "operators": names,
        "leaf_operators": leaves,
        "scan_ops": scans,
        "seek_ops": seeks,
        "index_scan_ops": idx_scans,
        "scan_operator_present": bool(scans or idx_scans),
        "leaf_estimated_rows_max": leaf_max,
        "sweep_rows_threshold": sweep_rows,
        "sweep_shaped": bool(leaf_max is not None and leaf_max >= sweep_rows),
    }


def explain(driver, database: str, cypher: str, params: Dict[str, Any],
            sweep_rows: float) -> Dict[str, Any]:
    try:
        with driver.session(database=database) as s:
            res = s.run("EXPLAIN " + cypher, **params)
            list(res)
            summary = res.consume()
        out = classify(summary.plan, sweep_rows)
        out["ok"] = True
        return out
    except Exception as exc:  # a generated query may simply not run
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


def profile(driver, database: str, cypher: str, params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        with driver.session(database=database) as s:
            res = s.run("PROFILE " + cypher, **params)
            rows = len(list(res))
            summary = res.consume()
        ops = walk(summary.profile)
        db_hits = sum(int((o.get("args") or {}).get("DbHits") or o.get("dbHits") or 0)
                      for o in ops)
        real_rows = sum(int((o.get("args") or {}).get("Rows") or o.get("rows") or 0)
                        for o in ops)
        return {"ok": True, "rows_returned": rows, "db_hits": db_hits,
                "operator_rows_total": real_rows,
                "server_ms": float(summary.result_available_after or 0)
                             + float(summary.result_consumed_after or 0)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


def grammar_member(compiled, text: str) -> bool:
    import xgrammar as xgr
    m = xgr.GrammarMatcher(compiled)
    try:
        return bool(m.accept_string(text) and m.is_completed())
    except Exception:
        return False


def envelope(cypher: str) -> str:
    """The grammar produces the JSON envelope seocho's contract expects, not bare Cypher."""
    return '{"cypher": "' + " ".join(cypher.split()) + '"}'


def collect_generated(paths: List[Path]) -> List[Dict[str, Any]]:
    """Every distinct query the guidance benchmark's arms actually generated."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for p in paths:
        if not p.exists():
            continue
        d = json.loads(p.read_text())
        endpoint = (d.get("endpoint") or {}).get("model") or p.stem
        for s in d.get("samples", []):
            c = " ".join((s.get("cypher") or "").split())
            if not c or c in seen:
                continue
            seen.add(c)
            out.append({"source": "generated", "origin": p.name, "model": endpoint,
                        "mode": s.get("mode"), "question_id": s.get("question_id"),
                        "cypher": c})
    return out


# Hand-written probes. The point of the first three is adversarial: they are the cheapest
# scan-shaped queries a model could write, so if the grammar admits them the guarantee is not
# there. The anchored pair isolates *where* the anchor sits, which is the one thing the
# grammar's node rule takes away.
PROBES = [
    {"source": "probe", "name": "label_scan_count", "intent": "scan",
     "cypher": "MATCH (a:Account { _workspace_id: $workspace_id }) "
               "RETURN count(a) AS n LIMIT $limit"},
    {"source": "probe", "name": "label_scan_filtered", "intent": "scan",
     "cypher": "MATCH (a:Account { _workspace_id: $workspace_id }) "
               "WHERE a.risk_tier >= $risk_tier RETURN count(a) AS n, "
               "avg(a.amount) AS avg_amount LIMIT $limit"},
    {"source": "probe", "name": "label_scan_two_hop", "intent": "scan",
     "cypher": "MATCH (a:Account { _workspace_id: $workspace_id })-[t:TRANSFER]->"
               "(b:Account { _workspace_id: $workspace_id }) "
               "RETURN count(t) AS n, sum(t.amount) AS total LIMIT $limit"},
    {"source": "probe", "name": "anchor_in_where", "intent": "anchored",
     "cypher": "MATCH (a:Account { _workspace_id: $workspace_id })-[t:TRANSFER]->"
               "(b:Account { _workspace_id: $workspace_id }) WHERE a.acct_no = $acct_no "
               "RETURN count(t) AS n, sum(t.amount) AS total LIMIT $limit"},
    {"source": "probe", "name": "anchor_in_node_map", "intent": "anchored",
     "cypher": "MATCH (a:Account { _workspace_id: $workspace_id, acct_no: $acct_no })"
               "-[t:TRANSFER]->(b:Account { _workspace_id: $workspace_id }) "
               "RETURN count(t) AS n, sum(t.amount) AS total LIMIT $limit"},
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", default="bolt://localhost:7690")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", required=True)
    ap.add_argument("--database", default="finbenchl100")
    ap.add_argument("--ontology", default="ontology/finbench.ontology.yaml")
    ap.add_argument("--anchor", type=int, default=None)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--reports", default="results/bench/text2cypher_guidance_20260822.json,"
                                         "results/bench/text2cypher_guidance_mara_20260822.json")
    ap.add_argument("--sweep-rows", type=float, default=1000.0,
                    help="a leaf touching at least this many estimated rows makes the query "
                         "sweep-shaped. Three orders of magnitude above a point lookup, and "
                         "far below any label in this graph, so the classification is not "
                         "sensitive to the exact value")
    ap.add_argument("--seocho-src", default=None)
    ap.add_argument("--db-container", default=None)
    ap.add_argument("--out", default="results/bench/grammar_plan_shape.json")
    args = ap.parse_args()

    import yaml
    # seocho is a resident submodule, not always a wheel; the bridge resolves whichever is
    # present so the benchmark does not depend on how it was installed.
    _ensure_seocho_on_path(args.seocho_src)
    from seocho.ontology import Ontology
    from seocho.query.hybrid_planner import policy_from_ontology
    onto = Ontology.from_dict(yaml.safe_load(Path(args.ontology).read_text()))
    policy = policy_from_ontology(onto)

    params: Dict[str, Any] = {"workspace_id": "default", "limit": args.limit,
                              "acct_no": args.anchor, "n": 1, "channel_risk": 0.5,
                              "risk_tier": 3, "amount": 1000.0}
    grammar = grammar_from_policy(policy, params=sorted(params))

    import xgrammar as xgr
    ti = xgr.TokenizerInfo([chr(c) for c in range(32, 127)], vocab_type=xgr.VocabType.RAW)
    compiled = xgr.GrammarCompiler(ti).compile_grammar(xgr.Grammar.from_ebnf(grammar))

    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    if args.anchor is None:
        with driver.session(database=args.database) as s:
            p99 = s.run("MATCH (a:Account) RETURN percentileDisc(a._out_degree,0.99) AS p"
                        ).single()["p"]
            args.anchor = s.run("MATCH (a:Account) WHERE a._out_degree>=$p "
                                "RETURN min(a.acct_no) AS a", p=p99).single()["a"]
        params["acct_no"] = args.anchor
    print(f"[bench] anchor={args.anchor} db={args.database}", flush=True)

    corpus = PROBES + collect_generated([Path(p) for p in args.reports.split(",")])
    rows: List[Dict[str, Any]] = []
    for item in corpus:
        cy = item["cypher"]
        in_grammar = grammar_member(compiled, envelope(cy))
        in_subset, subset_reasons = covers(cy, policy)
        rec = dict(item)
        rec.update({
            "in_grammar": in_grammar,
            "in_policy_subset": in_subset,
            "subset_reasons": subset_reasons,
            "explain": explain(driver, args.database, cy, params, args.sweep_rows),
            "profile": profile(driver, args.database, cy, params),
        })
        rows.append(rec)
        pl = rec["explain"]
        shape = ("?" if not pl.get("ok")
                 else ("SWEEP" if pl["sweep_shaped"] else "point"))
        est = pl.get("leaf_estimated_rows_max")
        hits = rec["profile"].get("db_hits")
        name = item.get("name") or f"{item.get('mode')}/{item.get('question_id')}"
        print(f"  {name:34s} grammar={'Y' if in_grammar else 'n'} "
              f"policy={'Y' if in_subset else 'n'} plan={shape:4s} "
              f"est_rows={est if est is None else round(est)} db_hits={hits} "
              f"leaves={','.join(pl.get('leaf_operators', [])[:3])}", flush=True)
    driver.close()

    admitted_scans = [r for r in rows if r["in_grammar"] and r["explain"].get("sweep_shaped")]
    report = {
        "schema_version": "seocho.finbench.grammar-plan-shape.v1",
        "question": "does the ontology-derived grammar prevent scan-shaped Cypher?",
        "method": {
            "membership": "xgrammar accept_string + is_completed on the JSON envelope — the "
                          "same engine vLLM constrains with, so membership is exact",
            "plan_shape": "EXPLAIN leaf operators. Classified by cost, not operator name: "
                          "the tenant scope is an indexed property, so a full sweep of a "
                          "label plans as NodeIndexSeek and a name-based test reports it as a "
                          "seek. sweep_shaped = a leaf touches >= --sweep-rows estimated rows",
            "cost": "PROFILE db hits and server-reported time",
        },
        "manifest": runmeta.manifest(db_container=args.db_container),
        "config": {k: v for k, v in vars(args).items() if k != "password"},
        "params": params,
        "grammar_chars": len(grammar),
        "grammar": grammar,
        "verdict": {
            # The headline: a single grammar-admitted scan settles it.
            "grammar_prevents_scans": not admitted_scans,
            "grammar_admitted_scan_shaped": [
                {"name": r.get("name") or f"{r.get('mode')}/{r.get('question_id')}",
                 "cypher": r["cypher"],
                 "leaf_operators": r["explain"].get("leaf_operators"),
                 "leaf_estimated_rows_max": r["explain"].get("leaf_estimated_rows_max"),
                 "leaf_operators_named_seek": not r["explain"].get("scan_operator_present"),
                 "db_hits": r["profile"].get("db_hits")}
                for r in admitted_scans],
        },
        "rows": rows,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"\ngrammar_prevents_scans = {report['verdict']['grammar_prevents_scans']}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
