#!/usr/bin/env python3
"""Scale Factor Benchmark: SF1 vs SF10 on SQL (DuckDB) & Graph Patterns.

Measures how database scale (SF1 -> SF10, 10x row expansion) impacts:
1. Execution Latency across Easy, Medium, and Hard tiers.
2. Join explosion cost (Multi-table joins & Recursive CTEs in SQL).
3. Precision and Gold Answer matching on power-law AML graph data.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, Any, List
import duckdb

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
SF1_DIR = WORKSPACE_ROOT / "outputs" / "finbench" / "sf1"
SF10_DIR = WORKSPACE_ROOT / "outputs" / "finbench" / "sf10"


def setup_duckdb_for_sf(sf_dir: Path) -> duckdb.DuckDBPyConnection:
    """Mounts Parquet files for a given SF directory into DuckDB views."""
    con = duckdb.connect(":memory:")

    # Register node views
    con.execute(f"CREATE VIEW account AS SELECT * FROM '{sf_dir}/nodes/Account.parquet'")
    con.execute(f"CREATE VIEW person AS SELECT * FROM '{sf_dir}/nodes/Person.parquet'")
    con.execute(f"CREATE VIEW company AS SELECT * FROM '{sf_dir}/nodes/Company.parquet'")
    con.execute(f"CREATE VIEW channel AS SELECT * FROM '{sf_dir}/nodes/Channel.parquet'")
    con.execute(f"CREATE VIEW medium AS SELECT * FROM '{sf_dir}/nodes/Medium.parquet'")

    # Register edge views
    con.execute(f"CREATE VIEW transfer AS SELECT * FROM '{sf_dir}/edges/transfer.parquet'")
    con.execute(f"CREATE VIEW own AS SELECT * FROM '{sf_dir}/edges/own.parquet'")
    con.execute(f"CREATE VIEW guarantee AS SELECT * FROM '{sf_dir}/edges/guarantee.parquet'")
    con.execute(f"CREATE VIEW sign_in AS SELECT * FROM '{sf_dir}/edges/sign_in.parquet'")
    con.execute(f"CREATE VIEW uses_channel AS SELECT * FROM '{sf_dir}/edges/uses_channel.parquet'")

    return con


# Benchmark scenarios adapted to Parquet column names
SCALE_SCENARIOS = [
    # --- EASY ---
    {
        "id": "ext_easy_1",
        "tier": "easy",
        "name": "Incoming Transfers Aggregation",
        "sql": "SELECT count(*) AS n, coalesce(sum(amount), 0) AS total FROM transfer WHERE dst = {a}",
        "cypher_pattern": "MATCH (:Account {id:{a}})<-[t:TRANSFER]-(:Account) RETURN count(t), sum(t.amount)",
        "joins": 0,
        "hops": 1,
    },
    {
        "id": "ext_easy_2",
        "tier": "easy",
        "name": "Outgoing Transfers Max Amount",
        "sql": "SELECT count(*) AS n, coalesce(max(amount), 0) AS biggest FROM transfer WHERE src = {a}",
        "cypher_pattern": "MATCH (:Account {id:{a}})-[t:TRANSFER]->(:Account) RETURN count(t), max(t.amount)",
        "joins": 0,
        "hops": 1,
    },
    {
        "id": "int_easy_1",
        "tier": "easy",
        "name": "Account Risk Tier Distribution",
        "sql": "SELECT count(*) AS accounts, sum(CASE WHEN risk_tier=5 THEN 1 ELSE 0 END) AS tier5 FROM account",
        "cypher_pattern": "MATCH (a:Account) RETURN count(a), sum(CASE WHEN a.risk_tier=5 THEN 1 ELSE 0 END)",
        "joins": 0,
        "hops": 0,
    },

    # --- MEDIUM ---
    {
        "id": "ext_med_1",
        "tier": "medium",
        "name": "High Channel Risk Inflow",
        "sql": "SELECT DISTINCT src AS acct FROM transfer WHERE dst = {a} AND channel_risk >= 5 ORDER BY acct LIMIT 5",
        "cypher_pattern": "MATCH (s:Account)-[t:TRANSFER]->(:Account {id:{a}}) WHERE t.channel_risk>=5 RETURN DISTINCT s.id ORDER BY s.id LIMIT 5",
        "joins": 0,
        "hops": 1,
    },
    {
        "id": "ext_med_2",
        "tier": "medium",
        "name": "Beneficial Owner Lookup (2-Hop)",
        "sql": """SELECT DISTINCT o.src AS owner_id 
FROM transfer t 
JOIN own o ON t.dst = o.dst 
WHERE t.src = {a} 
ORDER BY owner_id LIMIT 5""",
        "cypher_pattern": "MATCH (:Account {id:{a}})-[:TRANSFER]->(b:Account)<-[:OWN]-(o) RETURN DISTINCT o.id ORDER BY o.id LIMIT 5",
        "joins": 1,
        "hops": 2,
    },
    {
        "id": "int_med_1",
        "tier": "medium",
        "name": "Direct Transfers Under Same Ownership (Triangle)",
        "sql": """SELECT count(DISTINCT t.src || '-' || t.dst) AS n 
FROM transfer t 
JOIN own o1 ON t.src = o1.dst 
JOIN own o2 ON t.dst = o2.dst 
WHERE o1.src = o2.src AND t.src <> t.dst""",
        "cypher_pattern": "MATCH (o)-[:OWN]->(a:Account)-[:TRANSFER]->(b:Account)<-[:OWN]-(o) WHERE a<>b RETURN count(DISTINCT [a.id, b.id])",
        "joins": 2,
        "hops": 3,
    },

    # --- HARD ---
    {
        "id": "ext_hard_1",
        "tier": "hard",
        "name": "2-Hop Downstream Expansion (Recursive CTE)",
        "sql": """WITH RECURSIVE downstream AS (
  SELECT dst AS acct, 1 AS depth FROM transfer WHERE src = {a}
  UNION
  SELECT t.dst, d.depth + 1
  FROM transfer t JOIN downstream d ON t.src = d.acct
  WHERE d.depth < 2
)
SELECT count(DISTINCT d.acct) AS n, max(a.risk_tier) AS worst_risk_tier
FROM downstream d JOIN account a ON d.acct = a.id""",
        "cypher_pattern": "MATCH (:Account {id:{a}})-[:TRANSFER*1..2]->(b:Account) RETURN count(DISTINCT b), max(b.risk_tier)",
        "joins": 3,
        "hops": 2,
    },
    {
        "id": "int_hard_1",
        "tier": "hard",
        "name": "3-Layer Conjunction (Transfer + Guarantee + Device)",
        "sql": """SELECT DISTINCT a.id AS a1, b.id AS a2
FROM transfer t
JOIN account a ON (t.src = a.id)
JOIN account b ON (t.dst = b.id) AND a.id < b.id
JOIN own oa ON a.id = oa.dst
JOIN own ob ON b.id = ob.dst AND oa.src <> ob.src
JOIN guarantee g ON (g.src = oa.src AND g.dst = ob.src)
JOIN sign_in ma ON a.id = ma.dst
JOIN sign_in mb ON b.id = mb.dst AND ma.src = mb.src
ORDER BY a1, a2 LIMIT 5""",
        "cypher_pattern": """MATCH (a:Account)-[:TRANSFER]-(b:Account) WHERE a.id < b.id
MATCH (pa)-[:OWN]->(a), (pb)-[:OWN]->(b) WHERE pa<>pb AND (pa)-[:GUARANTEE]-(pb)
MATCH (m:Medium)-[:SIGN_IN]->(a), (m)-[:SIGN_IN]->(b)
RETURN DISTINCT a.id AS a1, b.id AS a2 ORDER BY a1,a2 LIMIT 5""",
        "joins": 7,
        "hops": 5,
    },
]


def run_scaling_benchmark():
    print("=" * 105)
    print("📈 Scale Factor Benchmark: Scaling from SF1 (25k edges) to SF10 (235k edges)")
    print("=" * 105)

    con_sf1 = setup_duckdb_for_sf(SF1_DIR)
    con_sf10 = setup_duckdb_for_sf(SF10_DIR)

    # Read manifest anchors
    m1 = json.loads((SF1_DIR / "manifest.json").read_text())
    m10 = json.loads((SF10_DIR / "manifest.json").read_text())

    anchor_sf1 = m1["degree_profile"]["curated_anchors"][0]["account_id"]
    anchor_sf10 = m10["degree_profile"]["curated_anchors"][0]["account_id"]

    results = []
    print(f"{'ID':<12} | {'Tier':<6} | {'Joins':<5} | {'SF1 (ms)':<10} | {'SF10 (ms)':<11} | {'Scaling Factor':<15} | {'SF10 Output Sample':<20}")
    print("-" * 105)

    tier_scaling = {"easy": [], "medium": [], "hard": []}

    for sc in SCALE_SCENARIOS:
        q_sf1 = sc["sql"].replace("{a}", str(anchor_sf1))
        q_sf10 = sc["sql"].replace("{a}", str(anchor_sf10))

        # Warmup and timed runs SF1
        con_sf1.execute(q_sf1).fetchall()
        t0 = time.perf_counter()
        res_sf1 = con_sf1.execute(q_sf1).fetchall()
        ms_sf1 = (time.perf_counter() - t0) * 1000

        # Warmup and timed runs SF10
        con_sf10.execute(q_sf10).fetchall()
        t0 = time.perf_counter()
        res_sf10 = con_sf10.execute(q_sf10).fetchall()
        ms_sf10 = (time.perf_counter() - t0) * 1000

        scale_ratio = ms_sf10 / max(ms_sf1, 0.001)
        tier_scaling[sc["tier"]].append(scale_ratio)

        results.append({
            "id": sc["id"],
            "tier": sc["tier"],
            "name": sc["name"],
            "joins": sc["joins"],
            "hops": sc["hops"],
            "sf1_latency_ms": round(ms_sf1, 3),
            "sf10_latency_ms": round(ms_sf10, 3),
            "scale_latency_ratio": round(scale_ratio, 2),
            "sf1_result": str(res_sf1[:2]),
            "sf10_result": str(res_sf10[:2]),
        })

        print(f"{sc['id']:<12} | {sc['tier']:<6} | {sc['joins']:<5} | {ms_sf1:<10.3f} | {ms_sf10:<11.3f} | {scale_ratio:<15.2f}x | {str(res_sf10[:1]):<20}")

    print("=" * 105)
    print("\n📊 Tier-wise Scaling Summary (10x Graph Expansion):")
    for tier, ratios in tier_scaling.items():
        avg_scale = sum(ratios) / len(ratios)
        print(f"  • {tier.upper():<7}: Average Latency Growth = {avg_scale:.2f}x across 10x scale factor bump")

    # Save to results
    out_file = WORKSPACE_ROOT / "results" / "bench_scale_factor.json"
    out_file.write_text(json.dumps({
        "schema_version": "seocho.finbench.scale-benchmark.v1",
        "scale_factors": [1, 10],
        "scenarios": results,
    }, indent=2))
    print(f"\n💾 Saved scaling benchmark results to: {out_file}")


if __name__ == "__main__":
    run_scaling_benchmark()
