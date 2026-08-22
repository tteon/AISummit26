#!/usr/bin/env python3
"""Validate the FIBO text2cypher suite before anything expensive consumes it.

A question suite is itself a measurement instrument, and the H200 run showed what an
uncalibrated one costs: threshold questions with no bound parameter were unwritable under the
grammar and literal-rejected without it, and the difference was misread as a property of the
grammar. So every claim this file's suite makes is checked mechanically:

* **gold runs** — EXPLAIN and then execute against the live graph with the declared params;
  a gold query that cannot run is not gold.
* **the params contract is closed** — every `$name` in the gold appears in `params`, and no
  literal number/string hides in the query (the policy would reject it at generation time,
  so it must not be smuggled in through the reference).
* **`in_subset` is true, not aspirational** — decided by xgrammar membership against the
  ontology-derived grammar built with exactly this question's parameter names, the same way
  the serving path builds it.
* **seocho's validator agrees** — `validate_text2cypher_fallback` on each in-subset gold, so
  the suite never ships a "correct" query the executor would refuse.

    python3 scripts/benchmarks/validate_fibo_suite.py --password "$NEO4J_PASSWORD"
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "runmeta", REPO_ROOT / "scripts" / "analysis" / "runmeta.py")
runmeta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runmeta)  # type: ignore[union-attr]

PARAM_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
# A bare number that is not part of a variable-length bound (*1..2) or an identifier.
LITERAL_NUM_RE = re.compile(r"(?<![\w$.*])(?<!\.\.)\d+(?:\.\d+)?(?![\w])")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", default="bolt://localhost:7688")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", required=True)
    ap.add_argument("--database", default="finbenchl1")
    ap.add_argument("--suite", default="configs/fibo_text2cypher_suite.yaml")
    ap.add_argument("--ontology", default="ontology/finbench.ontology.yaml")
    ap.add_argument("--anchor", type=int, default=None)
    ap.add_argument("--seocho-src", default=None)
    ap.add_argument("--db-container", default="aisummit-simtest")
    ap.add_argument("--out", default="results/analysis/fibo_suite_validation.json")
    args = ap.parse_args()

    import yaml
    from harness.seocho_bridge import _ensure_seocho_on_path
    _ensure_seocho_on_path(args.seocho_src)
    from seocho.ontology import Ontology
    from seocho.query.hybrid_planner import policy_from_ontology
    from seocho.query.workload_compiler import validate_text2cypher_fallback
    from harness.cypher_grammar import grammar_from_policy
    import xgrammar as xgr

    suite = yaml.safe_load(Path(args.suite).read_text())
    onto = Ontology.from_dict(yaml.safe_load(Path(args.ontology).read_text()))
    policy = policy_from_ontology(onto)
    ws = suite.get("workspace_id", "default")

    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    anchor = args.anchor
    if anchor is None:
        with driver.session(database=args.database) as s:
            p99 = s.run("MATCH (a:Account) RETURN percentileDisc(a._out_degree,0.99) AS p"
                        ).single()["p"]
            anchor = s.run("MATCH (a:Account) WHERE a._out_degree>=$p "
                           "RETURN min(a.acct_no) AS a", p=p99).single()["a"]

    ti = xgr.TokenizerInfo([chr(c) for c in range(32, 127)], vocab_type=xgr.VocabType.RAW)
    compiler = xgr.GrammarCompiler(ti)

    rows: List[Dict[str, Any]] = []
    problems: List[str] = []
    for q in suite["questions"]:
        gold = " ".join(str(q["gold"]).split())
        declared = dict(q.get("params") or {})
        bound = {k: (anchor if v == "anchor" else v) for k, v in declared.items()}
        bound["workspace_id"] = ws
        rec: Dict[str, Any] = {"id": q["id"], "family": q["family"],
                               "declared_in_subset": bool(q["in_subset"])}

        # 1. the params contract is closed
        used = set(PARAM_RE.findall(gold))
        missing = sorted(used - set(bound))
        rec["params_missing"] = missing
        if missing:
            problems.append(f'{q["id"]}: gold uses ${missing} not in params')
        literals = LITERAL_NUM_RE.findall(re.sub(r"\*\d+\.\.\d+", "", gold))
        rec["inlined_literals"] = literals
        if literals or '"' in gold or "'" in gold:
            problems.append(f'{q["id"]}: literal smuggled into gold: {literals}')

        # 2. gold runs
        try:
            with driver.session(database=args.database) as s:
                res = s.run(gold, **bound)
                data = [dict(r) for r in res]
                res.consume()
            rec["runs"] = True
            rec["rows"] = len(data)
            rec["sample"] = json.dumps(data[:2], default=str)[:160]
            # An aggregate-only query always returns one row; a listing query returning zero
            # makes the score degenerate — "empty equals empty" passes for any wrong query
            # that also returns nothing. The suite must not ship a gold that scores blind.
            if len(data) == 0:
                problems.append(f'{q["id"]}: gold returns 0 rows at this scale — '
                                f'scoring is blind (empty == empty)')
            elif len(data) == 1 and all(v in (0, None) for v in data[0].values()):
                problems.append(f'{q["id"]}: gold aggregates to all-zero/null — '
                                f'scoring is blind at this scale')
        except Exception as exc:
            rec["runs"] = False
            rec["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            problems.append(f'{q["id"]}: gold does not run — {rec["error"]}')

        # 3. in_subset is decided by the same grammar the serving path would build
        grammar = grammar_from_policy(policy, params=sorted(set(declared) | {"workspace_id"}
                                                            - {"anchor"}))
        comp = compiler.compile_grammar(xgr.Grammar.from_ebnf(grammar))
        m = xgr.GrammarMatcher(comp)
        member = bool(m.accept_string('{"cypher": "' + gold + '"}') and m.is_completed())
        rec["grammar_member"] = member
        if member != bool(q["in_subset"]):
            problems.append(f'{q["id"]}: in_subset={q["in_subset"]} but grammar membership={member}')

        # 4. seocho's validator agrees on the in-subset golds
        if q["in_subset"]:
            v = list(validate_text2cypher_fallback(gold, params=bound, policy=policy))
            rec["seocho_violations"] = v
            if v:
                problems.append(f'{q["id"]}: seocho validator rejects gold: {v}')
        rows.append(rec)
        status = "ok" if not any(p.startswith(q["id"] + ":") for p in problems) else "PROBLEM"
        print(f'  {q["id"]:24s} {q["family"]:9s} subset={str(q["in_subset"]):5s} '
              f'member={str(rec["grammar_member"]):5s} runs={rec.get("runs")} '
              f'rows={rec.get("rows", "-"):>3} {status}', flush=True)
    driver.close()

    inside = sum(1 for r in rows if r["declared_in_subset"])
    report = {
        "schema_version": "seocho.fibo.suite-validation.v1",
        "manifest": runmeta.manifest(db_container=args.db_container),
        "config": {k: v for k, v in vars(args).items() if k != "password"},
        "anchor": anchor,
        "counts": {"questions": len(rows), "inside_subset": inside,
                   "outside_subset": len(rows) - inside},
        "rows": rows,
        "problems": problems,
        "valid": not problems,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"\nvalid = {report['valid']}   ({inside} inside / {len(rows)-inside} outside)")
    for p in problems:
        print(f"  ! {p}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
