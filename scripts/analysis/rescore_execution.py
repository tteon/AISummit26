#!/usr/bin/env python3
"""Score the query, not the sentence: re-execute what each design settled on, against gold.

Answer-level scoring asks whether the reply carried the right numbers. It cannot tell a
right answer from a lucky one, and it says nothing about the artefact an operator
actually ships — the Cypher. Every question in this run carries a reference query (its
`ref` field) whose execution produced the gold, and every episode records the query the
agent settled on, so the standard text2cypher measure is available after the fact:
**execution accuracy** — re-run the settled query and compare what it returns with what
the reference returned.

Comparison is by value, not by column name: models rename freely (`transfer_count` for
`n`), so a scalar question compares the multiset of returned values against the gold
row's values, and a list question compares the set of values in the returned rows
against the gold column's set. Two verdicts are recorded per query:

  exact   the returned values are exactly the gold values (nothing missing, nothing extra)
  covers  the gold values are all present, but the query returned more besides

Only distinct (scale, question, query) triples are executed — three repeats that settled
on the same text are one execution — and each runs under a bounded transaction, with a
timeout recorded as its own outcome rather than folded into "wrong".

  python scripts/rescore_execution.py --password "$PW"   # -> results/rescore_execution.json
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from neo4j import GraphDatabase

from runmeta import manifest

WS = "default"
CHAIN = ("labels", "ontology", "guardrail", "plan")


def norm(v):
    """Compare numbers as numbers and everything else as its string form."""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f.is_integer() else f"{f:.6g}"
    return str(v)


def gold_values(gold):
    if isinstance(gold, dict):
        return sorted(norm(v) for v in gold.values() if v is not None)
    return sorted(norm(v) for v in gold)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--episodes", default="results/agent_interaction.json")
    p.add_argument("--out", default="results/rescore_execution.json")
    p.add_argument("--uri", default="bolt://127.0.0.1:7687")
    p.add_argument("--user", default="neo4j")
    p.add_argument("--password", required=True)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--row-cap", type=int, default=50)
    p.add_argument("--sf", nargs="+", type=int, default=[1, 10, 100])
    args = p.parse_args()

    eps = json.loads(Path(args.episodes).read_text())["episodes"]
    chain = [e for e in eps if e["arm"] in CHAIN and e["settled_cypher"]
             and e["sf"] in args.sf]

    # one execution per distinct (scale, question, query); repeats reuse the verdict
    jobs = {}
    for e in chain:
        key = (e["sf"], e["question_id"], e["settled_cypher"])
        jobs.setdefault(key, {"database": e["database"], "anchor": e["anchor"],
                              "gold": e.get("score_gold"), "arms": set()})
        jobs[key]["arms"].add(e["arm"])

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    verdicts = {}
    for i, ((sf, qid, cypher), meta) in enumerate(sorted(jobs.items(),
                                                         key=lambda kv: kv[0][0]), 1):
        params = {"workspace_id": WS, "ws": WS, "limit": args.row_cap}
        if meta["anchor"] is not None:
            params["a"] = meta["anchor"]
            params["acct_no"] = meta["anchor"]
        rec = {"sf": sf, "question_id": qid, "arms": sorted(meta["arms"]),
               "outcome": "ok", "rows": 0}
        t0 = time.perf_counter()
        try:
            with driver.session(database=meta["database"]) as s:
                tx = s.begin_transaction(timeout=args.timeout)
                try:
                    res = tx.run(cypher, **params)
                    rows = [dict(r) for _, r in zip(range(args.row_cap), res)]
                    res.consume()
                    tx.commit()
                finally:
                    if not tx.closed():
                        tx.close()
        except Exception as exc:
            rec.update(outcome="timeout" if "erminat" in str(exc) else "error",
                       error=type(exc).__name__,
                       ms=round((time.perf_counter() - t0) * 1000, 1))
            verdicts[(sf, qid, cypher)] = rec
            print(f"[{i}/{len(jobs)}] SF{sf} {qid} -> {rec['outcome']}", flush=True)
            continue

        rec["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        rec["rows"] = len(rows)
        got = sorted(norm(v) for r in rows for v in r.values() if v is not None)
        want = gold_values(meta["gold"]) if meta["gold"] is not None else None
        if want is None:
            rec.update(outcome="no gold")
        else:
            rec["exact"] = got == want
            rec["covers"] = all(w in got for w in want)
            rec["gold_values"] = want[:10]
            rec["got_values"] = got[:10]
        print(f"[{i}/{len(jobs)}] SF{sf} {qid} rows={len(rows)} "
              f"exact={rec.get('exact')} covers={rec.get('covers')} "
              f"{rec['ms']:.0f}ms", flush=True)
        verdicts[(sf, qid, cypher)] = rec
    driver.close()

    per_episode = []
    for e in chain:
        v = verdicts[(e["sf"], e["question_id"], e["settled_cypher"])]
        per_episode.append({
            "arm": e["arm"], "sf": e["sf"], "question_id": e["question_id"],
            "repeat": e["repeat"], "difficulty": e["difficulty"],
            "answer_correct": e["score_correct"],
            "query_outcome": v["outcome"],
            "query_exact": v.get("exact"), "query_covers": v.get("covers"),
        })

    out = {
        "schema_version": "seocho.finbench.rescore-execution.v1",
        "manifest": manifest(rescoring="execution accuracy of the settled query",
                             source=args.episodes, tx_timeout_s=args.timeout,
                             row_cap=args.row_cap, distinct_queries=len(jobs)),
        "queries": [dict(k=list(k), **{kk: vv for kk, vv in v.items()})
                    for k, v in ((f"{sf}|{qid}", v)
                                 for (sf, qid, _), v in verdicts.items())],
        "episodes": per_episode,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1, default=str))

    print(f"\n{'arm':11s} {'answer ok':>10s} {'query exact':>12s} "
          f"{'query covers':>13s} {'unverified':>11s}")
    for arm in CHAIN:
        r = [x for x in per_episode if x["arm"] == arm]
        if not r:
            continue
        print(f"{arm:11s} {sum(x['answer_correct'] for x in r):>7d}/{len(r):<3d}"
              f" {sum(1 for x in r if x['query_exact']):>9d}/{len(r):<3d}"
              f" {sum(1 for x in r if x['query_covers']):>10d}/{len(r):<3d}"
              f" {sum(1 for x in r if x['query_outcome'] != 'ok'):>11d}")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
