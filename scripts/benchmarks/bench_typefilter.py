#!/usr/bin/env python3
"""What GOpt's TypeFilterRemovalRule buys on this graph, measured on this run's queries.

Lyu et al. (*Enhancing Neo4j Query Efficiency with Seamless Integration of the GOpt
Optimization Framework*, VLDB 2024 LSGDA) report ~2× from a rule that drops a label
filter whenever the schema already proves the type: if every edge of type T ends on a
node of type X, then filtering the target of T by :X is work with no effect on the
result. Their rule runs inside the optimizer, from an APOC-extracted schema. This script
applies the same rule by hand, from this repo's ontology, to the queries the agents
actually settled on — so the claim is tested on our workload rather than transferred.

Each pair runs both spellings under PROFILE, several times, and the variant is only
reported if it returns exactly the same rows: a "faster" query that answers differently
is not an optimization, and the guard is what makes the measurement trustworthy.

The removals this schema licenses (every relationship type has one target type and one
source type, so all of these are provable):

    -[:TRANSFER]->(x:Account)        -[:WITHDRAW]->(x:Account)
    -[:USES_CHANNEL]->(x:Channel)    -[:DEPOSIT]->(x:Account)
    -[:REPAY]->(x:Loan)              -[:APPLY]->(x:Loan)
    (x:Person|Company)-[:OWN]->      (x:Medium)-[:SIGN_IN]->

  python scripts/bench_typefilter.py --password "$PW"
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import statistics
import time
from pathlib import Path

from neo4j import GraphDatabase

from runmeta import manifest

WS = "default"
# A union endpoint may only lose its label when the query names the whole union: a bare
# `:Person` on a Person|Company endpoint is a narrower filter that does real work, and
# removing it would change the answer, not just the plan.
UNION = r"(?:Person\|Company|Company\|Person)"

# (regex, replacement) — each drops a label the schema already implies. The property map
# (the tenant scope) is preserved; only the label is removed.
TARGET_RULES = [
    (rf"(-\[[^\]]*:{rel}[^\]]*\]->\s*\(\s*\w+)\s*:{lab}", r"\1")
    for rel, lab in (("TRANSFER", "Account"), ("WITHDRAW", "Account"),
                     ("USES_CHANNEL", "Channel"), ("DEPOSIT", "Account"),
                     ("REPAY", "Loan"), ("APPLY", "Loan"), ("OWN", "Account"),
                     ("SIGN_IN", "Account"), ("INVEST", "Company"),
                     ("GUARANTEE", UNION))
]
SOURCE_RULES = [
    (rf"(\(\s*\w+)\s*:{lab}(\s*(?:\{{[^}}]*\}})?\s*\)\s*-\[[^\]]*:{rel})", r"\1\2")
    for rel, lab in (("OWN", UNION), ("SIGN_IN", "Medium"), ("GUARANTEE", UNION),
                     ("APPLY", UNION), ("INVEST", UNION), ("TRANSFER", "Account"),
                     ("WITHDRAW", "Account"), ("USES_CHANNEL", "Account"),
                     ("REPAY", "Account"), ("DEPOSIT", "Loan"))
]


def strip_types(cypher: str) -> tuple[str, int]:
    out, n = cypher, 0
    for pat, rep in TARGET_RULES + SOURCE_RULES:
        out, k = re.subn(pat, rep, out)
        n += k
    return out, n


def rows_of(session, cypher, params, timeout):
    tx = session.begin_transaction(timeout=timeout)
    try:
        res = tx.run("PROFILE " + cypher, **params)
        rows = [tuple(sorted((k, str(v)) for k, v in r.items()))
                for _, r in zip(range(200), res)]
        summary = res.consume()
        tx.commit()
    finally:
        if not tx.closed():
            tx.close()

    def hits(plan):
        # the 6.x driver hands back plans as dicts; older ones as objects
        if plan is None:
            return 0
        if isinstance(plan, dict):
            args, kids = plan.get("args") or {}, plan.get("children") or []
        else:
            args = getattr(plan, "arguments", None) or {}
            kids = getattr(plan, "children", None) or []
        return int(args.get("DbHits", 0)) + sum(hits(k) for k in kids)

    return sorted(rows), hits(summary.profile)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--episodes", default="results/episodes/agent_interaction.json")
    p.add_argument("--password", required=True)
    p.add_argument("--uri", default="bolt://127.0.0.1:7687")
    p.add_argument("--user", default="neo4j")
    p.add_argument("--sf", type=int, default=100)
    p.add_argument("--repeats", type=int, default=4)
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--max-db-hits", type=int, default=30_000_000)
    p.add_argument("--pairs", type=int, default=6)
    args = p.parse_args()

    eps = json.loads(Path(args.episodes).read_text())["episodes"]
    seen, cands = set(), []
    for e in sorted(eps, key=lambda x: -x["db_hits"]):
        if e["sf"] != args.sf or not e["settled_cypher"]:
            continue
        q = e["settled_cypher"]
        if q in seen or e["db_hits"] > args.max_db_hits or "accept-cost" in q:
            continue
        variant, n = strip_types(q)
        if n == 0 or variant == q:
            continue
        seen.add(q)
        cands.append({"question_id": e["question_id"], "arm": e["arm"],
                      "database": e["database"], "anchor": e["anchor"],
                      "removed": n, "original": q, "variant": variant})
        if len(cands) >= args.pairs:
            break

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    out_rows = []
    for i, c in enumerate(cands, 1):
        params = {"workspace_id": WS, "ws": WS, "limit": 50}
        if c["anchor"] is not None:
            params["a"] = params["acct_no"] = c["anchor"]
        rec = {k: c[k] for k in ("question_id", "arm", "removed")}
        try:
            with driver.session(database=c["database"]) as s:
                # warm both spellings, and check the ORIGINAL against ITSELF first: a
                # query whose ORDER BY has ties returns a different top-N run to run, and
                # blaming the rule for that would be a false positive
                base_rows, base_hits = rows_of(s, c["original"], params, args.timeout)
                again, _ = rows_of(s, c["original"], params, args.timeout)
                rec["stable"] = base_rows == again
                var_rows, var_hits = rows_of(s, c["variant"], params, args.timeout)
                rec["same_rows"] = base_rows == var_rows
                b, v = [], []
                for i_rep in range(args.repeats):
                    # alternate which spelling goes first: whichever runs second inherits
                    # a page cache the other just warmed, and that bias is worth more
                    # than the effect being measured
                    order = ((c["original"], b), (c["variant"], v))
                    if i_rep % 2:
                        order = order[::-1]
                    for cypher, sink in order:
                        t = time.perf_counter()
                        rows_of(s, cypher, params, args.timeout)
                        sink.append((time.perf_counter() - t) * 1000)
            rec.update(db_hits_before=base_hits, db_hits_after=var_hits,
                       ms_before=round(statistics.median(b), 1),
                       ms_after=round(statistics.median(v), 1),
                       rows=len(base_rows), outcome="ok")
        except Exception as exc:
            rec.update(outcome="error", error=f"{type(exc).__name__}: {str(exc)[:120]}")
        rec["original"] = c["original"]
        rec["variant"] = c["variant"]
        out_rows.append(rec)
        print(f"[{i}/{len(cands)}] {rec['question_id']:12s} removed={rec['removed']} "
              f"stable={rec.get('stable')} same_rows={rec.get('same_rows')} "
              f"hits {rec.get('db_hits_before', 0):,} -> {rec.get('db_hits_after', 0):,} "
              f"| {rec.get('ms_before', 0):.0f} -> {rec.get('ms_after', 0):.0f} ms",
              flush=True)
    driver.close()

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(f"results/interface/typefilter_sf{args.sf}_{ts}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "manifest": manifest(bench="typefilter-removal",
                             rule="GOpt TypeFilterRemovalRule, applied by hand",
                             reference="Lyu et al., VLDB 2024 LSGDA",
                             sf=args.sf, repeats=args.repeats),
        "pairs": out_rows,
    }, indent=1, default=str))

    ok = [r for r in out_rows if r["outcome"] == "ok" and r["same_rows"]
          and r.get("stable")]
    unstable = [r for r in out_rows if r["outcome"] == "ok" and not r.get("stable")]
    differ = [r for r in out_rows if r["outcome"] == "ok" and r.get("stable")
              and not r["same_rows"]]
    print(f"\nverified pairs (query stable AND rows identical): {len(ok)}/{len(out_rows)}"
          + (f"   [{len(unstable)} excluded: original not row-stable]" if unstable else "")
          + (f"   [{len(differ)} excluded: rows differ — inspect by hand, a top-N under a "
             f"non-total ORDER BY is plan-dependent]" if differ else ""))
    for r in differ:
        print(f"    inspect: {r['question_id']} — rows differ; check for ties at the "
              f"LIMIT boundary before blaming the rule")
    for r in ok:
        dh = (r["db_hits_before"] - r["db_hits_after"]) / max(r["db_hits_before"], 1)
        sp = r["ms_before"] / max(r["ms_after"], 0.001)
        print(f"  {r['question_id']:12s} db hits {dh:+6.1%}   latency {sp:5.2f}x")
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
