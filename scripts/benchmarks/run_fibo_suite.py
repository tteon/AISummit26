#!/usr/bin/env python3
"""Run the FIBO text2cypher suite end to end: generate, validate, execute, score against gold.

This is the routing rehearsal (plan step 4). On an endpoint without constrained decoding it
measures the *baseline* the routing decision needs: per question, does unconstrained
generation through seocho's text2cypher already produce the right answer, how many attempts
does it take, and how does that split across the suite's in_subset labels? A question the
model already answers correctly gains nothing from a grammar; a question it fails inside the
subset is the grammar's case; a question it fails outside the subset is the widening/routing
case. The same runner pointed at a vLLM server with --grammar-mode does the enforced arms.

Scoring: the generated query's rows against the gold query's rows, both run with the same
bound params — order-insensitive, values normalised to strings, and column names ignored
(aliases are free). The suite validator already guarantees gold is non-blind at this scale.

    python3 scripts/benchmarks/run_fibo_suite.py --password "$NEO4J_PASSWORD" --provider mara
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "runmeta", REPO_ROOT / "scripts" / "analysis" / "runmeta.py")
runmeta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runmeta)  # type: ignore[union-attr]

from harness.llm import add_provider_args, model_config  # noqa: E402


def norm_rows(rows: List[Dict[str, Any]]) -> List[tuple]:
    """Order-insensitive, alias-insensitive row normal form."""
    out = []
    for r in rows:
        vals = []
        for v in r.values():
            if isinstance(v, float):
                v = round(v, 6)
            vals.append(str(v))
        out.append(tuple(sorted(vals)))
    return sorted(out)


async def run_suite(args: Any) -> None:
    import yaml
    from harness.seocho_bridge import (
        _ensure_seocho_on_path, enable_observability, guide_backend_with_grammar,
        make_llm_backend, schema_map_from_ontology)
    obs = enable_observability(backend="otlp", endpoint=args.seocho_otlp,
                               source=args.seocho_src)
    _ensure_seocho_on_path(args.seocho_src)
    from seocho.ontology import Ontology
    from seocho.query.hybrid_planner import policy_from_ontology
    from seocho.query.text2cypher import generate_validated_cypher
    from harness.cypher_grammar import grammar_from_policy

    suite = yaml.safe_load(Path(args.suite).read_text())
    onto = Ontology.from_dict(yaml.safe_load(Path(args.ontology).read_text()))
    policy = policy_from_ontology(onto)
    schema = schema_map_from_ontology(onto)
    ws = suite.get("workspace_id", "default")
    cfg = model_config(args)

    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    anchor = args.anchor
    if anchor is None:
        with driver.session(database=args.database) as s:
            p99 = s.run("MATCH (a:Account) RETURN percentileDisc(a._out_degree,0.99) AS p"
                        ).single()["p"]
            anchor = s.run("MATCH (a:Account) WHERE a._out_degree>=$p "
                           "RETURN min(a.acct_no) AS a", p=p99).single()["a"]

    def run_query(cypher: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        with driver.session(database=args.database) as s:
            res = s.run(cypher, **params)
            rows = [dict(r) for r in res]
            res.consume()
        return rows

    async def explain(cypher: str, params: Dict[str, Any]) -> None:
        def _run() -> None:
            with driver.session(database=args.database) as s:
                s.run("EXPLAIN " + cypher, **params).consume()
        await asyncio.to_thread(_run)

    rows_out: List[Dict[str, Any]] = []
    for q in suite["questions"]:
        declared = dict(q.get("params") or {})
        bound = {k: (anchor if v == "anchor" else v) for k, v in declared.items()}
        bound["workspace_id"] = ws
        gold_rows = norm_rows(run_query(" ".join(str(q["gold"]).split()), bound))
        question = q["question"].format(a=anchor,
                                        **{k: v for k, v in bound.items() if k != "workspace_id"})

        # A fresh backend per question so the grammar (when enforced) carries exactly this
        # question's parameter names — the executor's contract, measured the hard way.
        backend = make_llm_backend(cfg)
        use_grammar = (args.mode == "grammar"
                       or (args.mode == "routed" and bool(q["in_subset"])))
        if use_grammar:
            grammar = grammar_from_policy(policy, params=sorted(bound))
            backend = guide_backend_with_grammar(backend, grammar)
        # Per-generation token usage, cached tokens included: without it the KV-reuse half of
        # the claim is unprovable from the run. Same wrapper the e2e bridge uses.
        usage_events: List[Dict[str, Any]] = []
        _orig = backend.acomplete
        async def _tracking(*a: Any, _orig=_orig, _sink=usage_events, **kw: Any):
            r = await _orig(*a, **kw)
            u = getattr(r, "usage", None)
            if u:
                _sink.append(dict(u))
            return r
        backend.acomplete = _tracking  # type: ignore[method-assign]

        for rep in range(args.repeats):
            rec: Dict[str, Any] = {"id": q["id"], "family": q["family"],
                                   "in_subset": bool(q["in_subset"]), "repeat": rep,
                                   "mode": args.mode, "grammar_used": use_grammar}
            usage_events.clear()
            t0 = time.perf_counter()
            try:
                gen = await generate_validated_cypher(
                    question=question, schema=schema, params=bound, policy=policy,
                    backend=backend, model=cfg.model_name, explain=explain)
                rec["generate_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                rec["attempts"] = gen.attempts
                rec["cypher"] = gen.cypher
                got = norm_rows(run_query(gen.cypher, dict(gen.params)))
                rec["rows"] = len(got)
                rec["correct"] = got == gold_rows
                rec["nonempty_overlap"] = bool(set(got) & set(gold_rows))
            except Exception as exc:
                rec["generate_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                rec["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                rec["correct"] = False
            rec["generate_usage"] = {
                "llm_calls": len(usage_events),
                "prompt_tokens": sum(u.get("prompt_tokens", 0) for u in usage_events),
                "completion_tokens": sum(u.get("completion_tokens", 0) for u in usage_events),
                "cached_tokens": sum(u.get("cached_tokens", 0) for u in usage_events),
            }
            rows_out.append(rec)
            print(f'  {q["id"]:24s} {"in " if q["in_subset"] else "out"} r{rep} '
                  f'correct={str(rec["correct"]):5s} attempts={rec.get("attempts")} '
                  f'{rec["generate_ms"]:>8.0f}ms {rec.get("error", "")[:60]}', flush=True)
    driver.close()

    def agg(pred) -> Dict[str, Any]:
        sel = [r for r in rows_out if pred(r)]
        if not sel:
            return {"n": 0}
        return {"n": len(sel),
                "correct": sum(1 for r in sel if r["correct"]),
                "attempts_mean": round(sum(r.get("attempts") or 0 for r in sel) / len(sel), 2),
                "generate_ms_mean": round(sum(r["generate_ms"] for r in sel) / len(sel), 1)}

    summary = {
        "mode": args.mode,
        "all": agg(lambda r: True),
        "inside_subset": agg(lambda r: r["in_subset"]),
        "outside_subset": agg(lambda r: not r["in_subset"]),
        "usage": {
            "prompt_tokens": sum(r.get("generate_usage", {}).get("prompt_tokens", 0) for r in rows_out),
            "completion_tokens": sum(r.get("generate_usage", {}).get("completion_tokens", 0) for r in rows_out),
            "cached_tokens": sum(r.get("generate_usage", {}).get("cached_tokens", 0) for r in rows_out),
        },
    }
    report = {
        "schema_version": "seocho.fibo.suite-run.v1",
        "manifest": runmeta.manifest(db_container=args.db_container),
        "config": {k: v for k, v in vars(args).items() if k != "password"},
        "endpoint": cfg.descriptor(),
        "seocho": obs,
        "anchor": anchor,
        "summary": summary,
        "rows": rows_out,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print("\n=== summary ===")
    for k, v in summary.items():
        print(f"  {k:15s} {v}")
    print(f"wrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_provider_args(ap)
    ap.add_argument("--uri", default="bolt://localhost:7688")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", required=True)
    ap.add_argument("--database", default="finbenchl1")
    ap.add_argument("--suite", default="configs/fibo_text2cypher_suite.yaml")
    ap.add_argument("--ontology", default="ontology/finbench.ontology.yaml")
    ap.add_argument("--anchor", type=int, default=None)
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--mode", choices=("plain", "grammar", "routed"), default="plain",
                    help="plain: unconstrained. grammar: every question decodes under the "
                         "per-question grammar. routed: grammar only where the suite's "
                         "in_subset label says the gold is expressible — the routing "
                         "hypothesis, using the label as the router")
    ap.add_argument("--seocho-src", default=None)
    ap.add_argument("--seocho-otlp", default=None)
    ap.add_argument("--db-container", default="aisummit-simtest")
    ap.add_argument("--out", default="results/bench/fibo_suite_run.json")
    args = ap.parse_args()
    asyncio.run(run_suite(args))


if __name__ == "__main__":
    main()
