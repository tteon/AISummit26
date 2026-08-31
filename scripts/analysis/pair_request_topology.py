#!/usr/bin/env python3
"""Join raw agent episodes to global and anchor-local FinBench topology descriptors.

Scale is not used as a causal label here.  Each raw episode retains its own SF/question/anchor
and DB ledger, then receives the descriptor of the graph location it actually queried.
Bounded two-hop reach is intentional: an unbounded neighbourhood measurement would itself
become the hub workload we are trying to describe.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("runmeta", ROOT / "scripts/analysis/runmeta.py")
runmeta = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(runmeta)


def _one(session: Any, query: str, **params: Any) -> dict[str, Any]:
    row = session.run(query, **params).single()
    return dict(row) if row else {}


def anchor_descriptor(driver: Any, database: str, anchor: int, cap: int) -> dict[str, Any]:
    with driver.session(database=database) as session:
        out = _one(session, "MATCH (a:Account {acct_no:$a}) RETURN a._out_degree AS recorded_out_degree", a=anchor)
        incoming = _one(session, "MATCH (:Account)-[r:TRANSFER]->(:Account {acct_no:$a}) RETURN count(r) AS transfer_in", a=anchor)
        outgoing = _one(session, "MATCH (:Account {acct_no:$a})-[r:TRANSFER]->() RETURN count(r) AS transfer_out", a=anchor)
        peers = _one(session, "MATCH (a:Account {acct_no:$a})-[:TRANSFER]-(b:Account) RETURN count(DISTINCT b) AS direct_peers", a=anchor)
        reach = _one(session, "MATCH (:Account {acct_no:$a})-[:TRANSFER]->(b:Account) WITH b ORDER BY b.acct_no LIMIT $cap MATCH (b)-[:TRANSFER]->(c:Account) RETURN count(DISTINCT c) AS bounded_out_2hop_reach", a=anchor, cap=cap)
        return {"anchor": anchor, **out, **incoming, **outgoing, **peers, **reach,
                "twohop_first_frontier_cap": cap,
                "local_measurement_note": "two-hop reach is bounded at first frontier; clustering is supplied by global sampled profile"}


def pair(report_path: Path, profiles: dict[int, Path], uri: str, password: str,
         databases: dict[int, str], cap: int) -> dict[str, Any]:
    report = json.loads(report_path.read_text())
    profiles_data = {sf: json.loads(path.read_text()) for sf, path in profiles.items()}
    cache: dict[tuple[int, int], dict[str, Any]] = {}
    with GraphDatabase.driver(uri, auth=("neo4j", password)) as driver:
        episodes = []
        for sample in report["samples"]:
            sf, anchor = int(sample["sf"]), sample.get("anchor")
            if anchor is None or sf not in databases:
                continue
            key = (sf, int(anchor))
            if key not in cache:
                cache[key] = {"sf": sf, "database": databases[sf],
                              **anchor_descriptor(driver, databases[sf], int(anchor), cap)}
            profile = profiles_data[sf]
            episodes.append({
                "episode_id": sample.get("episode_id"), "sf": sf,
                "question_id": sample.get("question_id"), "repeat": sample.get("repeat"),
                "correct": sample.get("correct"), "error": sample.get("error"),
                "graph_trips": sample.get("graph_trips", 0), "db_hits": sample.get("db_hits", 0),
                "db_ms": sample.get("db_ms", 0.0), "anchor_topology": cache[key],
                "global_topology": {"nodes_with_edges": profile["nodes_with_edges"],
                    "transfer_edges": profile["multiplicity"]["edges"],
                    "avg_local_clustering": profile["clustering"]["avg_local_clustering"],
                    "pct_sampled_in_any_triangle": profile["clustering"]["pct_sampled_in_any_triangle"],
                    "directed_3_cycles_lower_bound": profile["motifs"]["directed_3_cycles"]},
            })
    return {"schema_version": "finbench.request-topology-pairing.v1",
            "manifest": runmeta.manifest(validation="episode topology pairing"),
            "inputs": {"report": str(report_path), "profiles": {str(k): str(v) for k, v in profiles.items()}},
            "method": {"unit": "raw agent episode", "join_key": ["sf", "anchor"],
                       "warning": "Two scales and one selected anchor per scale do not identify a causal topology effect; sweep curated anchor bands before fitting an effect."},
            "anchor_descriptors": list(cache.values()), "episodes": episodes,
            "valid": bool(episodes)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--profile", action="append", required=True, help="SF:path, repeat")
    parser.add_argument("--database", action="append", required=True, help="SF:database, repeat")
    parser.add_argument("--uri", default="bolt://localhost:7688")
    parser.add_argument("--password", required=True)
    parser.add_argument("--twohop-cap", type=int, default=200)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    profiles = {int(x.split(":", 1)[0]): Path(x.split(":", 1)[1]) for x in args.profile}
    databases = {int(x.split(":", 1)[0]): x.split(":", 1)[1] for x in args.database}
    result = pair(args.report, profiles, args.uri, args.password, databases, args.twohop_cap)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(f"valid={result['valid']} episodes={len(result['episodes'])} anchors={len(result['anchor_descriptors'])} wrote {args.out}")


if __name__ == "__main__":
    main()
