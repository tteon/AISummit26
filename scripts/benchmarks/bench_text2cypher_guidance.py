#!/usr/bin/env python3
"""Does constraining the decoder remove the repair loop, and what does it cost?

The seocho_native arm made the repair loop measurable: 8 generations needed 9 attempts against
MARA gpt-oss-120b, and one question spent 7 generations — 30.9 s of GPU — before answering
wrong. Every repair is a full re-prefill and a full decode, so the validator's rules are paid
for in serving time each time the model has to guess them.

They do not have to be guessed. The ontology is strict, so "only declared labels", "every node
carries the tenant scope" and "ends with LIMIT $limit" are expressible as a grammar
(`harness/cypher_grammar.py`) rather than as a rejection. This benchmark runs seocho's own
text2cypher both ways against the same endpoint and the same questions:

    json     seocho's default — response_format={"type":"json_object"}, validate, repair
    grammar  the same call with extra_body={"structured_outputs":{"grammar": ...}} instead

and reports attempts, wall time, whether the query survived validation, and whether it stayed
inside the grammar's subset. What it does not claim: that a grammar makes answers correct. It
makes a class of *structural* failure unrepresentable; semantics are still the model's problem,
and the samples show that plainly.

    python3 scripts/benchmarks/bench_text2cypher_guidance.py \\
        --provider vllm --model Qwen/Qwen2.5-1.5B-Instruct \\
        --base-url http://127.0.0.1:8000/v1 --password "$NEO4J_PASSWORD" \\
        --uri bolt://localhost:7688 --database finbenchl1 --repeats 3
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from harness.cypher_grammar import covers, grammar_from_policy  # noqa: E402
from harness.llm import model_config  # noqa: E402
from harness.seocho_bridge import (  # noqa: E402
    _ensure_seocho_on_path, enable_observability, guide_backend_with_grammar, make_llm_backend,
)

_spec = importlib.util.spec_from_file_location(
    "runmeta", REPO_ROOT / "scripts" / "analysis" / "runmeta.py")
runmeta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runmeta)  # type: ignore[union-attr]

WS = "default"

QUESTIONS = [
    ("incoming_count", "For account number {a}: how many incoming transfers has it received "
                       "in total, and what is their total value?"),
    ("largest_out", "For account number {a}: how many outgoing transfers are there, and what "
                    "is the single largest amount sent?"),
    ("risky_senders", "Which accounts sent money to account {a} over a channel whose "
                      "channel_risk is 5 or more?"),
    ("owners_two_hops", "Who owns the accounts that account {a} sent money to?"),
]


async def one_generation(*, backend, model: str, question: str, schema, params, policy,
                         explain) -> Dict[str, Any]:
    from seocho.query.text2cypher import generate_validated_cypher
    t0 = time.perf_counter()
    try:
        res = await generate_validated_cypher(
            question=question, schema=schema, params=params, policy=policy,
            backend=backend, model=model, explain=explain)
        ok_subset, reasons = covers(res.cypher, policy)
        return {"ok": True, "attempts": res.attempts, "explained": res.explained,
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "cypher": res.cypher, "in_subset": ok_subset, "subset_reasons": reasons}
    except Exception as exc:
        return {"ok": False, "attempts": None, "explained": None,
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", default="vllm")
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--uri", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", required=True)
    ap.add_argument("--database", default="finbenchl1")
    ap.add_argument("--ontology", default="ontology/finbench.ontology.yaml")
    ap.add_argument("--anchor", type=int, default=None)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--modes", default="json,grammar")
    ap.add_argument("--seocho-src", default=None)
    ap.add_argument("--seocho-otlp", default=None)
    ap.add_argument("--out", default="results/bench/text2cypher_guidance.json")
    args = ap.parse_args()

    obs = enable_observability(backend="otlp", endpoint=args.seocho_otlp,
                              source=args.seocho_src)
    _ensure_seocho_on_path(args.seocho_src)
    import yaml
    from neo4j import GraphDatabase
    from seocho.ontology import Ontology
    from seocho.query.hybrid_planner import policy_from_ontology

    onto = Ontology.from_dict(yaml.safe_load(Path(args.ontology).read_text()))
    policy = policy_from_ontology(onto)
    from harness.seocho_bridge import schema_map_from_ontology
    schema = schema_map_from_ontology(onto)

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    anchor = args.anchor
    if anchor is None:
        with driver.session(database=args.database) as s:
            p99 = s.run("MATCH (a:Account) RETURN percentileDisc(a._out_degree,0.99) AS p").single()["p"]
            anchor = s.run("MATCH (a:Account) WHERE a._out_degree>=$p RETURN min(a.acct_no) AS a",
                           p=p99).single()["a"]

    # Thresholds are parameters, not literals — and that is not a style preference here. The
    # policy forbids inlined literals, so a question about "channel_risk >= 5" is unanswerable
    # unless the harness binds a parameter for the threshold. Leaving it unbound made the
    # tightened grammar unsatisfiable, and a constrained decoder cannot reject: it decoded to
    # the 2000-token ceiling and returned nothing, 17.6 s per attempt instead of a 1.4 s
    # rejection. An interface that forbids literals must supply the parameters that replace
    # them, or it disagrees with itself and the decoder pays for the disagreement.
    params = {"workspace_id": WS, "a": anchor, "acct_no": anchor,
              "limit": int(getattr(policy, "max_result_rows", 50) or 50),
              "channel_risk": 5, "risk_tier": 5, "amount": 10_000_000, "ts": 0, "n": 5}
    grammar = grammar_from_policy(policy, params=sorted(params))
    cfg = model_config(provider=args.provider, model_name=args.model, base_url=args.base_url)

    async def explain(cypher: str, p: Dict[str, Any]) -> None:
        def _run() -> None:
            with driver.session(database=args.database) as session:
                session.run("EXPLAIN " + cypher, **p).consume()
        await asyncio.to_thread(_run)

    async def run_all() -> List[Dict[str, Any]]:
        samples: List[Dict[str, Any]] = []
        for mode in args.modes.split(","):
            backend = make_llm_backend(cfg)
            if mode == "grammar":
                backend = guide_backend_with_grammar(backend, grammar)
            for qid, template in QUESTIONS:
                for rep in range(args.repeats):
                    rec = await one_generation(backend=backend, model=cfg.model_name,
                                              question=template.format(a=anchor),
                                              schema=schema, params=params, policy=policy,
                                              explain=explain)
                    rec.update({"mode": mode, "question_id": qid, "repeat": rep})
                    samples.append(rec)
                    print(f"  {mode:8s} {qid:16s} r{rep} ok={rec['ok']} "
                          f"attempts={rec.get('attempts')} {rec['ms']:>7.0f}ms "
                          f"subset={rec.get('in_subset')} {rec.get('error','')[:60]}",
                          flush=True)
        return samples

    print(f"[bench] anchor={anchor} modes={args.modes} grammar={len(grammar)} chars "
          f"seocho={obs['seocho_version']}", flush=True)
    samples = asyncio.run(run_all())
    driver.close()

    def agg(mode: str) -> Dict[str, Any]:
        rows = [s for s in samples if s["mode"] == mode]
        ok = [s for s in rows if s["ok"]]
        att = [s["attempts"] for s in ok if s["attempts"]]
        return {
            "generations": len(rows),
            "validated": len(ok),
            "failed": len(rows) - len(ok),
            "attempts_total": sum(att),
            "attempts_mean": round(statistics.mean(att), 2) if att else None,
            "repairs": sum(a - 1 for a in att),
            "ms_mean": round(statistics.mean(s["ms"] for s in rows), 1),
            "ms_total": round(sum(s["ms"] for s in rows), 1),
            "in_subset": sum(1 for s in ok if s.get("in_subset")),
        }

    report = {
        "schema_version": "seocho.finbench.text2cypher-guidance.v1",
        "manifest": runmeta.manifest(),
        "endpoint": cfg.descriptor(),
        "seocho": obs,
        "anchor": anchor,
        "grammar_chars": len(grammar),
        "grammar": grammar,
        "by_mode": {m: agg(m) for m in args.modes.split(",")},
        "samples": samples,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print("\n=== by mode ===")
    for m, a in report["by_mode"].items():
        print(f"  {m:8s} {a}")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
