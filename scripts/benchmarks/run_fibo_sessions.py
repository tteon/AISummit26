#!/usr/bin/env python3
"""Long-running AML investigation sessions: context engineering vs prompt accumulation.

Experiment ③. Each session is a chain of anaphoric questions ("that account", "that owner")
whose gold parameters come from previous turns' gold answers, so a context policy that drops
the wrong thing loses the referent and the turn. Three policies:

    full_history   every prior Q&A verbatim — the prefix only ever grows, which is the shape
                   prefix caching was built for; the cost is tokens
    window         the last 2 turns verbatim — short, but the prefix shifts every turn, so the
                   cache re-prefills from the system block down
    case_file      a running fact sheet (entities established so far) — minimal tokens,
                   rewritten each turn, and the arm most likely to lose a referent

What gets measured per turn: correctness against the chained gold (exact rows,
alias-insensitive), generation time, attempts, and — via generate_usage — prompt/cached token
counts, which is where the three policies' serving costs separate. Run --validate-only first:
it executes the gold chain and refuses a turn whose gold is empty or all-null, same blindness
rule as the suite validator.

    python3 scripts/benchmarks/run_fibo_sessions.py --password "$PW" --provider mara --arms full_history
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

_spec2 = importlib.util.spec_from_file_location(
    "fibosuite", REPO_ROOT / "scripts" / "benchmarks" / "run_fibo_suite.py")
fibosuite = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(fibosuite)  # type: ignore[union-attr]
norm_rows = fibosuite.norm_rows

from harness.llm import add_provider_args, model_config  # noqa: E402


def resolve_params(turn: Dict[str, Any], gold_answers: Dict[str, List[Dict[str, Any]]],
                   anchor: int, ws: str) -> Dict[str, Any]:
    """Bind a turn's params, pulling `from:` references out of previous turns' gold rows."""
    bound: Dict[str, Any] = {"workspace_id": ws}
    for k, v in (turn.get("params") or {}).items():
        if v == "anchor":
            bound[k] = anchor
        elif isinstance(v, dict) and "from" in v:
            src = v["from"]
            rows = gold_answers.get(src["turn"])
            if not rows:
                raise ValueError(f'{turn["id"]}: param {k} references empty turn {src["turn"]}')
            bound[k] = rows[0][src["column"]]
        else:
            bound[k] = v
    return bound


def build_context(policy: str, history: List[Dict[str, str]]) -> str:
    """The conversation context each policy hands to the generator alongside the question."""
    if not history:
        return ""
    if policy == "full_history":
        lines = [f'Q: {h["q"]}\nA: {h["a"]}' for h in history]
        return "Conversation so far:\n" + "\n".join(lines) + "\n\n"
    if policy == "window":
        lines = [f'Q: {h["q"]}\nA: {h["a"]}' for h in history[-2:]]
        return "Recent conversation:\n" + "\n".join(lines) + "\n\n"
    if policy == "case_file":
        lines = [f'- {h["fact"]}' for h in history if h.get("fact")]
        return ("Case file (facts established so far):\n" + "\n".join(lines) + "\n\n"
                if lines else "")
    raise ValueError(policy)


async def main_async(args: Any) -> None:
    import yaml
    from harness.seocho_bridge import (_ensure_seocho_on_path, enable_observability,
                                       make_llm_backend, schema_map_from_ontology)
    obs = enable_observability(backend="otlp", endpoint=args.seocho_otlp,
                               source=args.seocho_src)
    _ensure_seocho_on_path(args.seocho_src)
    from seocho.ontology import Ontology
    from seocho.query.hybrid_planner import policy_from_ontology
    from seocho.query.text2cypher import generate_validated_cypher

    spec = yaml.safe_load(Path(args.sessions).read_text())
    onto = Ontology.from_dict(yaml.safe_load(Path(args.ontology).read_text()))
    qpolicy = policy_from_ontology(onto)
    schema = schema_map_from_ontology(onto)
    ws = spec.get("workspace_id", "default")
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

    # --- resolve and check the gold chains first; nothing spends until they hold ------------
    chains: Dict[str, Dict[str, Any]] = {}
    problems: List[str] = []
    for sess in spec["sessions"]:
        gold_answers: Dict[str, List[Dict[str, Any]]] = {}
        bound_by_turn: Dict[str, Dict[str, Any]] = {}
        for turn in sess["turns"]:
            try:
                bound = resolve_params(turn, gold_answers, anchor, ws)
                rows = run_query(" ".join(str(turn["gold"]).split()), bound)
            except Exception as exc:
                problems.append(f'{sess["id"]}/{turn["id"]}: gold chain broke — '
                                f'{type(exc).__name__}: {str(exc)[:120]}')
                rows, bound = [], {}
            if not rows:
                problems.append(f'{sess["id"]}/{turn["id"]}: gold returns 0 rows — blind')
            elif len(rows) == 1 and all(v in (0, None) for v in rows[0].values()):
                problems.append(f'{sess["id"]}/{turn["id"]}: gold all-zero/null — blind')
            gold_answers[turn["id"]] = rows
            bound_by_turn[turn["id"]] = bound
        chains[sess["id"]] = {"gold_answers": gold_answers, "bound": bound_by_turn}
    for p in problems:
        print(f"  ! {p}", flush=True)
    if args.validate_only or problems:
        print(f"chain validation: {'OK' if not problems else 'PROBLEMS'} "
              f"({sum(len(s['turns']) for s in spec['sessions'])} turns)")
        if args.validate_only:
            return
        if problems:
            raise SystemExit(2)

    # --- run the arms ------------------------------------------------------------------------
    rows_out: List[Dict[str, Any]] = []
    for arm in args.arms.split(","):
        for sess in spec["sessions"]:
            chain = chains[sess["id"]]
            history: List[Dict[str, str]] = []
            for turn in sess["turns"]:
                bound = chain["bound"][turn["id"]]
                gold_rows = norm_rows(chain["gold_answers"][turn["id"]])
                # The question text uses only non-referenced placeholders; referenced values
                # arrive through conversation context, never through the template — that is
                # the whole experiment.
                fmt = {k: v for k, v in bound.items()
                       if not isinstance((turn.get("params") or {}).get(k), dict)}
                question = turn["question"].format(a=anchor, **{k: v for k, v in fmt.items()
                                                                if k != "workspace_id"})
                context = build_context(arm, history)
                backend = make_llm_backend(cfg)
                usage_events: List[Dict[str, Any]] = []
                _orig = backend.acomplete
                async def _tracking(*a: Any, _o=_orig, _s=usage_events, **kw: Any):
                    r = await _o(*a, **kw)
                    u = getattr(r, "usage", None)
                    if u:
                        _s.append(dict(u))
                    return r
                backend.acomplete = _tracking  # type: ignore[method-assign]

                async def explain(cypher: str, p: Dict[str, Any]) -> None:
                    def _run() -> None:
                        with driver.session(database=args.database) as s:
                            s.run("EXPLAIN " + cypher, **p).consume()
                    await asyncio.to_thread(_run)

                rec: Dict[str, Any] = {"arm": arm, "session": sess["id"], "turn": turn["id"],
                                       "context_chars": len(context)}
                t0 = time.perf_counter()
                answer_text = ""
                try:
                    gen = await generate_validated_cypher(
                        question=context + question, schema=schema, params=bound,
                        policy=qpolicy, backend=backend, model=cfg.model_name,
                        explain=explain)
                    rec["generate_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                    rec["attempts"] = gen.attempts
                    rec["cypher"] = gen.cypher
                    got_rows = run_query(gen.cypher, dict(gen.params))
                    got = norm_rows(got_rows)
                    rec["rows"] = len(got)
                    rec["correct"] = got == gold_rows
                    answer_text = json.dumps(got_rows[:3], default=str)[:200]
                except Exception as exc:
                    rec["generate_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                    rec["error"] = f"{type(exc).__name__}: {str(exc)[:140]}"
                    rec["correct"] = False
                    answer_text = "ERROR"
                rec["generate_usage"] = {
                    "llm_calls": len(usage_events),
                    "prompt_tokens": sum(u.get("prompt_tokens", 0) for u in usage_events),
                    "completion_tokens": sum(u.get("completion_tokens", 0) for u in usage_events),
                    "cached_tokens": sum(u.get("cached_tokens", 0) for u in usage_events),
                }
                rows_out.append(rec)
                # History carries the GOLD answer, not the model's: the experiment isolates
                # what the context POLICY loses, not what a wrong earlier answer poisons.
                # (Error propagation is a real phenomenon, but mixing the two makes neither
                # measurable; a --carry-model-answers arm can test the other regime later.)
                gold_first = chain["gold_answers"][turn["id"]][0]
                history.append({
                    "q": question,
                    "a": json.dumps(chain["gold_answers"][turn["id"]][:3], default=str)[:200],
                    "fact": f'{turn["id"]}: ' + ", ".join(f"{k}={v}" for k, v in gold_first.items()),
                })
                u = rec["generate_usage"]
                print(f'  {arm:12s} {sess["id"]:18s} {turn["id"]:18s} '
                      f'correct={str(rec["correct"]):5s} ctx={rec["context_chars"]:>5d}ch '
                      f'prompt={u["prompt_tokens"]:>5d} cached={u["cached_tokens"]:>5d} '
                      f'{rec["generate_ms"]:>7.0f}ms {rec.get("error", "")[:40]}', flush=True)
    driver.close()

    def agg(arm: str) -> Dict[str, Any]:
        sel = [r for r in rows_out if r["arm"] == arm]
        if not sel:
            return {}
        u = [r["generate_usage"] for r in sel]
        pt = sum(x["prompt_tokens"] for x in u)
        return {"n": len(sel), "correct": sum(1 for r in sel if r["correct"]),
                "generate_ms_mean": round(sum(r["generate_ms"] for r in sel) / len(sel), 1),
                "prompt_tokens": pt,
                "cached_tokens": sum(x["cached_tokens"] for x in u),
                "cache_rate": round(sum(x["cached_tokens"] for x in u) / max(pt, 1), 4)}

    report = {
        "schema_version": "seocho.fibo.sessions-run.v1",
        "manifest": runmeta.manifest(db_container=args.db_container),
        "config": {k: v for k, v in vars(args).items() if k != "password"},
        "endpoint": cfg.descriptor(),
        "seocho": obs,
        "anchor": anchor,
        "summary": {arm: agg(arm) for arm in args.arms.split(",")},
        "rows": rows_out,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print("\n=== summary ===")
    for arm, v in report["summary"].items():
        print(f"  {arm:14s} {v}")
    print(f"wrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_provider_args(ap)
    ap.add_argument("--uri", default="bolt://localhost:7688")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", required=True)
    ap.add_argument("--database", default="finbenchl1")
    ap.add_argument("--sessions", default="configs/fibo_investigation_sessions.yaml")
    ap.add_argument("--ontology", default="ontology/finbench.ontology.yaml")
    ap.add_argument("--anchor", type=int, default=None)
    ap.add_argument("--arms", default="full_history,window,case_file")
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument("--seocho-src", default=None)
    ap.add_argument("--seocho-otlp", default=None)
    ap.add_argument("--db-container", default="aisummit-simtest")
    ap.add_argument("--out", default="results/bench/fibo_sessions_run.json")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
