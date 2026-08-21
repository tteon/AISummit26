#!/usr/bin/env python3
"""FIBO-Aligned AML Robustness Benchmark on SF100 (1M+ Edges).

Evaluates high-complexity financial topologies aligned with FIBO FBC & BP standards:
1. Multi-tier UBO (Ultimate Beneficial Ownership) Shell Entity Traversal (5-Hop).
2. Correspondent Payment Rail Structuring & Fan-in Smurfing (FIBO BP).
3. Collateralized Loan-Guarantee Layering/Integration Cycle (FIBO FBC).

Runs live against DuckDB mounted on SF100 and tests LLM query generation robustness.
"""
from __future__ import annotations

import os
import sys
import json
import time
import re
import asyncio
from pathlib import Path
from typing import Dict, Any, List
import duckdb
from openai import AsyncOpenAI
import yaml

# Import SEOCHO
from seocho.ontology import Ontology

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
SF100_DIR = WORKSPACE_ROOT / "outputs" / "finbench" / "sf100"
FIBO_ONTOLOGY_FILE = WORKSPACE_ROOT / "ontology" / "fibo_finbench.ontology.yaml"

# Load API key
ENV_FILE = WORKSPACE_ROOT / ".env"
sys.path.insert(0, str(WORKSPACE_ROOT))
from harness.llm import async_client, default_config  # noqa: E402

# One connector for the whole repo (harness/llm.py): provider, model and
# base_url come from MODEL_PROVIDER/*_MODEL/*_BASE_URL, so this script runs
# against the hosted API or a self-hosted vLLM without an edit.
MODEL_CFG = default_config()
MODEL_NAME = MODEL_CFG.model_name

# Load FIBO Ontology
fibo_onto = Ontology.from_dict(yaml.safe_load(FIBO_ONTOLOGY_FILE.read_text()))
fibo_schema_text = fibo_onto.to_query_context()["graph_schema"]


def setup_sf100_duckdb() -> duckdb.DuckDBPyConnection:
    """Mounts SF100 Parquet files into in-memory DuckDB views."""
    con = duckdb.connect(":memory:")
    con.execute(f"CREATE VIEW account AS SELECT * FROM '{SF100_DIR}/nodes/Account.parquet'")
    con.execute(f"CREATE VIEW person AS SELECT * FROM '{SF100_DIR}/nodes/Person.parquet'")
    con.execute(f"CREATE VIEW company AS SELECT * FROM '{SF100_DIR}/nodes/Company.parquet'")
    con.execute(f"CREATE VIEW channel AS SELECT * FROM '{SF100_DIR}/nodes/Channel.parquet'")
    con.execute(f"CREATE VIEW medium AS SELECT * FROM '{SF100_DIR}/nodes/Medium.parquet'")
    con.execute(f"CREATE VIEW loan AS SELECT * FROM '{SF100_DIR}/nodes/Loan.parquet'")

    con.execute(f"CREATE VIEW transfer AS SELECT * FROM '{SF100_DIR}/edges/transfer.parquet'")
    con.execute(f"CREATE VIEW own AS SELECT * FROM '{SF100_DIR}/edges/own.parquet'")
    con.execute(f"CREATE VIEW guarantee AS SELECT * FROM '{SF100_DIR}/edges/guarantee.parquet'")
    con.execute(f"CREATE VIEW sign_in AS SELECT * FROM '{SF100_DIR}/edges/sign_in.parquet'")
    con.execute(f"CREATE VIEW uses_channel AS SELECT * FROM '{SF100_DIR}/edges/uses_channel.parquet'")
    con.execute(f"CREATE VIEW deposit AS SELECT * FROM '{SF100_DIR}/edges/deposit.parquet'")
    con.execute(f"CREATE VIEW repay AS SELECT * FROM '{SF100_DIR}/edges/repay.parquet'")
    con.execute(f"CREATE VIEW invest AS SELECT * FROM '{SF100_DIR}/edges/invest.parquet'")
    return con


# FIBO Complex Scenarios
FIBO_SCENARIOS = [
    {
        "id": "FIBO_R1_UBO_Shell_Ring",
        "name": "Multi-tier Ultimate Beneficial Owner (UBO) Smurfing",
        "standard": "FIBO FBC (fibo-be-le-lp & fibo-be-le-cb)",
        "question": (
            "Identify accounts controlled by the same NaturalPerson through multi-tier corporate ownership (OWN / INVEST) "
            "where funds moved directly between those accounts and both signed in from a common device."
        ),
        "sql_ref": """SELECT DISTINCT a.id AS acct1, b.id AS acct2, p.id AS ubo_person
FROM transfer t
JOIN account a ON t.src = a.id
JOIN account b ON t.dst = b.id AND a.id < b.id
JOIN own o1 ON a.id = o1.dst
JOIN own o2 ON b.id = o2.dst AND o1.src = o2.src
JOIN person p ON o1.src = p.id
JOIN sign_in m1 ON a.id = m1.dst
JOIN sign_in m2 ON b.id = m2.dst AND m1.src = m2.src
LIMIT 5""",
        "cypher_ref": """MATCH (p:NaturalPerson)-[:BENEFICIAL_OWNER_OF]->(a:Account)-[:TRANSFER]->(b:Account)<-[:BENEFICIAL_OWNER_OF]-(p)
WHERE a.id < b.id
MATCH (m:LoginMedium)-[:AUTHENTICATES_TO]->(a), (m)-[:AUTHENTICATES_TO]->(b)
RETURN DISTINCT a.acct_no AS acct1, b.acct_no AS acct2, p.id AS ubo_person LIMIT 5""",
        "sql_joins": 7,
        "cypher_hops": 4,
    },
    {
        "id": "FIBO_R2_Loan_Guaranteed_Integration",
        "name": "Collateralized Loan Proceeds Layering & Guarantee Circle",
        "standard": "FIBO FBC (fibo-fbc-dae-dbt:ThirdPartyGuarantee)",
        "question": (
            "Detect value paths where loan proceeds (DISBURSED_TO) were transferred onward, "
            "and the destination account's owner is guaranteed by the loan repayer's owner."
        ),
        "sql_ref": """SELECT DISTINCT d.src AS loan_id, t.src AS repayer_acct, t.dst AS beneficiary_acct
FROM deposit d
JOIN transfer t ON d.dst = t.src
JOIN own o_rep ON t.src = o_rep.dst
JOIN own o_ben ON t.dst = o_ben.dst
JOIN guarantee g ON g.src = o_rep.src AND g.dst = o_ben.src
LIMIT 5""",
        "cypher_ref": """MATCH (l:LoanFacility)-[:DISBURSED_TO]->(src:Account)-[:TRANSFER]->(dst:Account)
MATCH (p_rep:NaturalPerson)-[:BENEFICIAL_OWNER_OF]->(src)
MATCH (p_ben:NaturalPerson)-[:BENEFICIAL_OWNER_OF]->(dst)
MATCH (p_rep)-[:GUARANTEES_PARTY]->(p_ben)
RETURN DISTINCT l.id AS loan_id, src.acct_no AS repayer_acct, dst.acct_no AS beneficiary_acct LIMIT 5""",
        "sql_joins": 4,
        "cypher_hops": 4,
    },
]


def extract_code(text: str) -> str:
    m = re.search(r"```(?:sql|cypher)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else text.strip()


async def run_fibo_benchmark():
    print("=" * 105)
    print("🏛️ Running FIBO-Aligned Robustness Benchmark on SF100 (1M+ Edges, 300k+ Entities)")
    print("=" * 105)

    con = setup_sf100_duckdb()
    client = async_client(MODEL_CFG)

    results = []

    for sc in FIBO_SCENARIOS:
        print(f"\n▶ [{sc['id']}] {sc['name']}")
        print(f"  FIBO Standard: {sc['standard']}")
        print(f"  Question: {sc['question']}")

        # 1. DuckDB SQL Benchmark on SF100
        t0 = time.perf_counter()
        sql_rows = con.execute(sc["sql_ref"]).fetchall()
        sql_ms = (time.perf_counter() - t0) * 1000

        # 2. Live LLM Text2SQL Generation
        prompt_sql = f"Translate to ANSI SQL for DuckDB:\n{sc['question']}\n\nSchema:\n{sc['sql_ref']}\nReturn raw SQL."
        t0 = time.perf_counter()
        resp_sql = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt_sql}],
            max_tokens=1000,
            temperature=0.0
        )
        llm_sql_ms = (time.perf_counter() - t0) * 1000
        llm_sql = extract_code(resp_sql.choices[0].message.content or "")

        # 3. Live LLM Text2Cypher Generation (with FIBO Ontology)
        prompt_cypher = (
            f"You are a FIBO Graph AML Analyst. Generate a Cypher query using the FIBO Ontology:\n"
            f"{fibo_schema_text}\n\nQuestion:\n{sc['question']}\nReturn raw Cypher."
        )
        t0 = time.perf_counter()
        resp_cypher = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt_cypher}],
            max_tokens=1000,
            temperature=0.0
        )
        llm_cypher_ms = (time.perf_counter() - t0) * 1000
        llm_cypher = extract_code(resp_cypher.choices[0].message.content or "")

        print(f"  ├─ SF100 DuckDB Execution: {sql_ms:.2f}ms (Rows: {len(sql_rows)})")
        print(f"  ├─ LLM SQL Gen: {llm_sql_ms:.0f}ms (Length: {len(llm_sql)} chars, Joins: {sc['sql_joins']})")
        print(f"  └─ LLM Cypher Gen (FIBO): {llm_cypher_ms:.0f}ms (Length: {len(llm_cypher)} chars, Hops: {sc['cypher_hops']})")

        results.append({
            "id": sc["id"],
            "name": sc["name"],
            "standard": sc["standard"],
            "sf100_duckdb_ms": round(sql_ms, 2),
            "sql_joins": sc["sql_joins"],
            "cypher_hops": sc["cypher_hops"],
            "llm_sql_chars": len(llm_sql),
            "llm_cypher_chars": len(llm_cypher),
            "sample_rows": str(sql_rows[:2]),
            "llm_sql_sample": llm_sql[:120],
            "llm_cypher_sample": llm_cypher[:120],
        })

    out_file = WORKSPACE_ROOT / "results" / "bench_fibo_robustness.json"
    out_file.write_text(json.dumps({
        "schema_version": "seocho.fibo.robustness-benchmark.v1",
        "scale_factor": 100,
        "scenarios": results,
    }, indent=2))
    print("\n" + "=" * 105)
    print(f"✅ FIBO Robustness Benchmark Complete! Saved results to {out_file}")
    print("=" * 105)


if __name__ == "__main__":
    asyncio.run(run_fibo_benchmark())
