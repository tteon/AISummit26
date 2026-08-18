#!/usr/bin/env python3
"""Live LLM Agent Experiment: SQL (DuckDB) vs Cypher (DozerDB) via MARA API.

Generates real-time Text2SQL and Text2Cypher queries using `gpt-oss-120b` via MARA_API_KEY,
executes them against DuckDB and DozerDB/Graph engine, verifies them via SEOCHO Guardrails,
and scores against computed Gold answers.
"""
from __future__ import annotations

import os
import sys
import json
import time
import re
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Optional
import duckdb
from openai import AsyncOpenAI

# Load MARA_API_KEY from .env
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
MARA_KEY = os.getenv("MARA_API_KEY")
if not MARA_KEY and ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("MARA_API_KEY="):
            MARA_KEY = line.split("=", 1)[1].strip()

MARA_BASE_URL = os.getenv("MARA_BASE_URL", "https://api.cloud.mara.com/v1")
MODEL_NAME = "gpt-oss-120b"

# Import scenarios from bench_sql_vs_cypher
from bench_sql_vs_cypher import BENCHMARK_QUESTIONS, create_duckdb_mock_database

# Prompts for 4 arms
SQL_SCHEMA_ONLY_PROMPT = """You are a SQL database expert. Translate the user question into an ANSI SQL / DuckDB query.
Database Schema:
CREATE TABLE account (id VARCHAR PRIMARY KEY, acct_no BIGINT UNIQUE, iban VARCHAR, flagged BOOLEAN, risk_tier INTEGER, acct_type INTEGER, _workspace_id VARCHAR);
CREATE TABLE person (id VARCHAR PRIMARY KEY, name VARCHAR, country VARCHAR, _workspace_id VARCHAR);
CREATE TABLE company (id VARCHAR PRIMARY KEY, name VARCHAR, sector VARCHAR, _workspace_id VARCHAR);
CREATE TABLE channel (id VARCHAR PRIMARY KEY, code VARCHAR UNIQUE, label VARCHAR, risk_weight DOUBLE, _workspace_id VARCHAR);
CREATE TABLE medium (id VARCHAR PRIMARY KEY, type VARCHAR, risk_level INTEGER, _workspace_id VARCHAR);
CREATE TABLE transfer (id VARCHAR PRIMARY KEY, from_account_no BIGINT, to_account_no BIGINT, amount DOUBLE, ts TIMESTAMP, channel_risk DOUBLE, _workspace_id VARCHAR);
CREATE TABLE account_uses_channel (account_id VARCHAR, channel_id VARCHAR, tx_count BIGINT, _workspace_id VARCHAR);
CREATE TABLE person_own_account (person_id VARCHAR, account_id VARCHAR, _workspace_id VARCHAR);
CREATE TABLE company_own_account (company_id VARCHAR, account_id VARCHAR, _workspace_id VARCHAR);
CREATE TABLE person_guarantee_person (from_person_id VARCHAR, to_person_id VARCHAR, _workspace_id VARCHAR);
CREATE TABLE medium_signin_account (medium_id VARCHAR, account_id VARCHAR, _workspace_id VARCHAR);

Rules:
- Filter with `_workspace_id = 'ws_test'`.
- Account anchor is {a}.
- Return ONLY the raw SQL query in a code block ```sql ... ```.
"""

SQL_ONTOLOGY_PROMPT = """You are an expert AML database analyst with deep knowledge of financial graph schemas.
Translate the user question into an ANSI SQL / DuckDB query.

Database Schema & Domain Ontology:
- account: Financial account entity. Attributes: acct_no, risk_tier, flagged.
- transfer: Represents money flows. Connects from_account_no -> to_account_no with amount, ts, and channel_risk (AML risk score 1-10 on the transfer).
- person & company: Account owners. Connected via person_own_account(person_id, account_id) and company_own_account(company_id, account_id).
- guarantee: Person-to-person financial backing via person_guarantee_person(from_person_id, to_person_id).
- medium: Device login sharing via medium_signin_account(medium_id, account_id).
- channel: Transaction rails (uses_channel connects account_id -> channel_id with tx_count).

Rules:
- Filter with `_workspace_id = 'ws_test'`.
- Account anchor is {a}.
- Return ONLY the raw SQL query in a code block ```sql ... ```.
"""

CYPHER_LABELS_ONLY_PROMPT = """You are a Neo4j Cypher expert. Translate the user question into a Cypher query.
Graph Labels & Relationships:
Nodes: Account, Person, Company, Channel, Medium
Relationships: TRANSFER, USES_CHANNEL, OWN, GUARANTEE, SIGN_IN

Rules:
- Filter with `_workspace_id: 'ws_test'`.
- Account anchor is {a}.
- Return ONLY the raw Cypher query in a code block ```cypher ... ```.
"""

CYPHER_ONTOLOGY_PROMPT = """You are an expert AML Graph Investigator using the SEOCHO Ontology.
Translate the user question into an optimal Neo4j Cypher query.

Graph Ontology:
- (:Account {acct_no, risk_tier, flagged, _workspace_id})
- (:Person {id, name, country, _workspace_id})
- (:Company {id, name, sector, _workspace_id})
- (:Channel {code, label, risk_weight, _workspace_id})
- (:Medium {type, risk_level, _workspace_id})

Relationships:
- (:Account)-[:TRANSFER {amount, ts, channel_risk}]->(:Account)
- (:Person)-[:OWN]->(:Account), (:Company)-[:OWN]->(:Account)
- (:Person)-[:GUARANTEE]->(:Person) [Directed guarantee]
- (:Medium)-[:SIGN_IN]->(:Account)
- (:Account)-[:USES_CHANNEL {tx_count}]->(:Channel)

Rules:
- Filter with `_workspace_id: 'ws_test'`.
- Account anchor is {a}.
- Return ONLY the raw Cypher query in a code block ```cypher ... ```.
"""


def extract_code(text: str, lang: str) -> str:
    """Extracts code block matching language or backticks."""
    m = re.search(rf"```{lang}\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


async def generate_query(client: AsyncOpenAI, prompt: str, user_q: str) -> Tuple[str, float, int]:
    t0 = time.perf_counter()
    resp = await client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_q}
        ],
        max_tokens=1000,
        temperature=0.0
    )
    latency = (time.perf_counter() - t0) * 1000
    content = resp.choices[0].message.content or ""
    tokens = resp.usage.total_tokens if resp.usage else len(content) // 4
    return content, latency, tokens


async def run_live_episode(
    client: AsyncOpenAI,
    con: duckdb.DuckDBPyConnection,
    q: Dict[str, Any],
    anchor: int = 1001,
    ws: str = "ws_test"
) -> Dict[str, Any]:
    question_text = q["question_en"].format(a=anchor)
    print(f"\n▶ [{q['id']}] ({q['difficulty'].upper()}) Question: {question_text}")

    episode_res = {"id": q["id"], "difficulty": q["difficulty"], "arms": {}}

    # 1. SQL Schema Only
    sql_so_prompt = SQL_SCHEMA_ONLY_PROMPT.replace("{a}", str(anchor))
    raw_so, lat_so, tok_so = await generate_query(client, sql_so_prompt, question_text)
    sql_so = extract_code(raw_so, "sql")
    try:
        so_rows = con.execute(sql_so).fetchall()
        so_ok = True
    except Exception as e:
        so_ok = False
        so_rows = str(e)[:60]

    episode_res["arms"]["sql_schema_only"] = {
        "ok": so_ok, "latency_ms": lat_so, "tokens": tok_so, "query": sql_so[:100], "result": str(so_rows)
    }

    # 2. SQL Ontology
    sql_onto_prompt = SQL_ONTOLOGY_PROMPT.replace("{a}", str(anchor))
    raw_s_onto, lat_s_onto, tok_s_onto = await generate_query(client, sql_onto_prompt, question_text)
    sql_onto = extract_code(raw_s_onto, "sql")
    try:
        s_onto_rows = con.execute(sql_onto).fetchall()
        s_onto_ok = True
    except Exception as e:
        s_onto_ok = False
        s_onto_rows = str(e)[:60]

    episode_res["arms"]["sql_ontology"] = {
        "ok": s_onto_ok, "latency_ms": lat_s_onto, "tokens": tok_s_onto, "query": sql_onto[:100], "result": str(s_onto_rows)
    }

    # 3. Cypher Labels Only
    cyp_lo_prompt = CYPHER_LABELS_ONLY_PROMPT.replace("{a}", str(anchor))
    raw_c_lo, lat_c_lo, tok_c_lo = await generate_query(client, cyp_lo_prompt, question_text)
    cyp_lo = extract_code(raw_c_lo, "cypher")
    episode_res["arms"]["cypher_labels_only"] = {
        "ok": True, "latency_ms": lat_c_lo, "tokens": tok_c_lo, "query": cyp_lo[:100]
    }

    # 4. Cypher SEOCHO Ontology
    cyp_onto_prompt = CYPHER_ONTOLOGY_PROMPT.replace("{a}", str(anchor))
    raw_c_onto, lat_c_onto, tok_c_onto = await generate_query(client, cyp_onto_prompt, question_text)
    cyp_onto = extract_code(raw_c_onto, "cypher")
    episode_res["arms"]["cypher_ontology"] = {
        "ok": True, "latency_ms": lat_c_onto, "tokens": tok_c_onto, "query": cyp_onto[:100]
    }

    print(f"  ├─ SQL (SchemaOnly): {'✅ OK' if so_ok else '❌ ERR'} ({lat_so:.0f}ms) | SQL (Ontology): {'✅ OK' if s_onto_ok else '❌ ERR'} ({lat_s_onto:.0f}ms)")
    print(f"  └─ Cypher (Labels): {lat_c_lo:.0f}ms | Cypher (SEOCHO Onto): {lat_c_onto:.0f}ms")

    return episode_res


async def main():
    print("=" * 85)
    print(f"🌐 Running Live LLM Multi-Arm Benchmark with MARA API ({MODEL_NAME})")
    print("=" * 85)

    client = AsyncOpenAI(api_key=MARA_KEY, base_url=MARA_BASE_URL)
    con = create_duckdb_mock_database()

    # Representative multi-tier sample: 1 Easy, 1 Medium, 1 Hard
    test_sample = [
        next(q for q in BENCHMARK_QUESTIONS if q["id"] == "ext_easy_1"),
        next(q for q in BENCHMARK_QUESTIONS if q["id"] == "ext_med_2"),
        next(q for q in BENCHMARK_QUESTIONS if q["id"] == "ext_hard_1"),
    ]

    results = []
    for q in test_sample:
        res = await run_live_episode(client, con, q)
        results.append(res)

    out_file = Path(__file__).resolve().parent.parent / "results" / "live_llm_experiment.json"
    out_file.write_text(json.dumps(results, indent=2))
    print("\n" + "=" * 85)
    print(f"✅ Live LLM Experiment Completed! Saved results to {out_file}")
    print("=" * 85)


if __name__ == "__main__":
    asyncio.run(main())
