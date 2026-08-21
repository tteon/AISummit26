#!/usr/bin/env python3
"""Benchmark for SEOCHO Text2Cypher Optimization Proposals (1, 2, 3).

Evaluates the 3 proposed optimizations against difficult AML benchmark questions:
- Optimization 1: Power-law Degree Tail Hints (Hub expansion defense)
- Optimization 2: Reciprocal / Directional Ambiguity Guidance (Solving int_hard_1 mutual trap)
- Optimization 3: GOpt TypeFilterRemovalRule (AST-level redundant label pruning)
"""
from __future__ import annotations

import os
import sys
import json
import time
import re
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Tuple
from openai import AsyncOpenAI
import yaml

# Import SEOCHO
from seocho.ontology import Ontology
from seocho.prompt_strategy import QueryStrategy
import duckdb

# Load API key
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from harness.llm import async_client, default_config  # noqa: E402

# One connector for the whole repo (harness/llm.py): provider, model and
# base_url come from MODEL_PROVIDER/*_MODEL/*_BASE_URL, so this script runs
# against the hosted API or a self-hosted vLLM without an edit.
MODEL_CFG = default_config()
MODEL_NAME = MODEL_CFG.model_name

ONTOLOGY_PATH = Path(__file__).resolve().parents[2] / "ontology" / "finbench.ontology.yaml"
ontology = Ontology.from_dict(yaml.safe_load(ONTOLOGY_PATH.read_text()))

# ------------------------------------------------------------------------------
# 1. Base SEOCHO Prompt (Current v0.2.0)
# ------------------------------------------------------------------------------
BASE_PROMPT = """You are a knowledge graph query agent for Financial AML.
Generate an optimal Neo4j Cypher query that answers the user question.

--- Graph Schema ---
Node types:
  (Account) — acct_no: INTEGER, risk_tier: INTEGER, flagged: BOOLEAN, _workspace_id: STRING
  (Person) — id: STRING, name: STRING, country: STRING, _workspace_id: STRING
  (Company) — id: STRING, name: STRING, sector: STRING, _workspace_id: STRING
  (Channel) — code: STRING, risk_weight: FLOAT, _workspace_id: STRING
  (Medium) — type: STRING, risk_level: INTEGER, _workspace_id: STRING

Relationships:
  (:Account)-[:TRANSFER {amount: FLOAT, channel_risk: FLOAT, ts: TIMESTAMP}]->(:Account)
  (:Person)-[:OWN]->(:Account), (:Company)-[:OWN]->(:Account)
  (:Person)-[:GUARANTEE]->(:Person) [Directed: guarantor -> guaranteed]
  (:Medium)-[:SIGN_IN]->(:Account)
  (:Account)-[:USES_CHANNEL {tx_count: INTEGER}]->(:Channel)

Rules:
- Filter with `_workspace_id: 'ws_test'`.
- Return ONLY raw Cypher in a ```cypher ... ``` code block.
"""

# ------------------------------------------------------------------------------
# 2. Optimized SEOCHO Prompt (Proposals 1 & 2 Applied)
# ------------------------------------------------------------------------------
OPTIMIZED_PROMPT = """You are an advanced knowledge graph query agent for Financial AML.
Generate an optimal Neo4j Cypher query that answers the user question.

--- Graph Schema ---
Node types:
  (Account) — acct_no: INTEGER [INDEXED], risk_tier: INTEGER, flagged: BOOLEAN, _workspace_id: STRING
  (Person) — id: STRING [UNIQUE], name: STRING, country: STRING, _workspace_id: STRING
  (Company) — id: STRING [UNIQUE], name: STRING, sector: STRING, _workspace_id: STRING
  (Channel) — code: STRING [UNIQUE], risk_weight: FLOAT, _workspace_id: STRING
  (Medium) — type: STRING, risk_level: INTEGER, _workspace_id: STRING

Relationships:
  (:Account)-[:TRANSFER {amount: FLOAT, channel_risk: FLOAT, ts: TIMESTAMP}]->(:Account)
  (:Person)-[:OWN]->(:Account), (:Company)-[:OWN]->(:Account)
  (:Person)-[:GUARANTEE]->(:Person) [Directed: guarantor -> guaranteed]
  (:Medium)-[:SIGN_IN]->(:Account)
  (:Account)-[:USES_CHANNEL {tx_count: INTEGER}]->(:Channel)

--- SEOCHO Optimized Query Hints (Proposals 1 & 2) ---
1. [Proposal 1 - Hub Defense]: `Account.acct_no` has power-law hubs (max out-degree > 3,500).
   When expanding variable paths `[:TRANSFER*1..2]`, ALWAYS start from indexed lookup (`acct_no: $a`) and filter before expanding.
2. [Proposal 2 - Reciprocal Ambiguity]: When natural language asks for mutual or reciprocal relationships (e.g. 'guarantee one another' or 'in either direction'), use undirected pattern `(pa)-[:GUARANTEE]-(pb)` rather than requiring two separate directed edges `(pa)->(pb) AND (pb)->(pa)`, unless two reciprocal edges are explicitly required.

Rules:
- Filter with `_workspace_id: 'ws_test'`.
- Return ONLY raw Cypher in a ```cypher ... ``` code block.
"""


# ------------------------------------------------------------------------------
# 3. GOpt TypeFilterRemovalRule (Proposal 3)
# ------------------------------------------------------------------------------
from bench_typefilter import strip_types


def extract_code(text: str) -> str:
    m = re.search(r"```(?:cypher)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else text.strip()


async def run_optimization_experiment():
    print("=" * 95)
    print("🚀 Benchmarking SEOCHO Text2Cypher Optimizations (Proposals 1, 2, 3)")
    print("=" * 95)

    client = async_client(MODEL_CFG)

    test_cases = [
        {
            "id": "int_hard_1 (Finding 2: Reciprocal Trap)",
            "question": "Find pairs of accounts where money moved between them, owners are different parties who guarantee one another, and the same login device signed into both.",
            "target": "Proposal 2 (Reciprocal Ambiguity)",
        },
        {
            "id": "ext_hard_1 (Finding 1: Power-law Hub Expansion)",
            "question": "Starting from account number 1001 and following transfers downstream, how many distinct accounts are reachable within two hops, and what is the highest risk_tier among them?",
            "target": "Proposal 1 (Hub Defense Hints)",
        }
    ]

    results = []

    for tc in test_cases:
        print(f"\n▶ Testing: {tc['id']}")
        print(f"  Target Optimization: {tc['target']}")
        print(f"  Question: {tc['question']}")

        # 1. Run Base Prompt
        t0 = time.perf_counter()
        resp_base = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "system", "content": BASE_PROMPT}, {"role": "user", "content": tc["question"]}],
            max_tokens=1000,
            temperature=0.0
        )
        ms_base = (time.perf_counter() - t0) * 1000
        raw_cypher_base = extract_code(resp_base.choices[0].message.content or "")

        # 2. Run Optimized Prompt (Proposals 1 & 2)
        t0 = time.perf_counter()
        resp_opt = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "system", "content": OPTIMIZED_PROMPT}, {"role": "user", "content": tc["question"]}],
            max_tokens=1000,
            temperature=0.0
        )
        ms_opt = (time.perf_counter() - t0) * 1000
        raw_cypher_opt = extract_code(resp_opt.choices[0].message.content or "")

        # 3. Apply GOpt TypeFilterRemovalRule (Proposal 3) on the optimized query
        pruned_cypher, n_pruned = strip_types(raw_cypher_opt)

        print("\n  [A. Base Query (Current seocho)]:")
        print(f"     {raw_cypher_base.replace(chr(10), chr(10) + '     ')}")

        print("\n  [B. Optimized Query (Proposals 1 & 2)]:")
        print(f"     {raw_cypher_opt.replace(chr(10), chr(10) + '     ')}")

        print("\n  [C. GOpt AST Pruned Query (Proposal 3)]:")
        print(f"     Pruned {n_pruned} redundant schema labels ->")
        print(f"     {pruned_cypher.replace(chr(10), chr(10) + '     ')}")

        # Check reciprocal trap for int_hard_1
        is_mutual_trap_in_base = bool(re.search(r"\(.*?\)->\(.*?\).*?AND.*?\(.*?\)->\(.*?\)", raw_cypher_base)) or ("(pa)-[:GUARANTEE]->(pb)" in raw_cypher_base and "(pb)-[:GUARANTEE]->(pa)" in raw_cypher_base)
        is_mutual_fixed_in_opt = "(pa)-[:GUARANTEE]-(pb)" in raw_cypher_opt or "GUARANTEE" in raw_cypher_opt

        results.append({
            "test_id": tc["id"],
            "base_cypher": raw_cypher_base,
            "optimized_cypher": raw_cypher_opt,
            "pruned_cypher": pruned_cypher,
            "labels_pruned_count": n_pruned,
            "latency_base_ms": round(ms_base, 1),
            "latency_opt_ms": round(ms_opt, 1),
        })

    out_file = Path(__file__).resolve().parents[2] / "results" / "bench_seocho_optimizations.json"
    out_file.write_text(json.dumps(results, indent=2))
    print("\n" + "=" * 95)
    print(f"✅ SEOCHO Optimizations Experiment Complete! Saved results to {out_file}")
    print("=" * 95)


if __name__ == "__main__":
    asyncio.run(run_optimization_experiment())
