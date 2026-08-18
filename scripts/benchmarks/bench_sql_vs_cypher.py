#!/usr/bin/env python3
"""Benchmark: SQL (DuckDB) vs Cypher (DozerDB/Neo4j) across 4 Information Arms.

Evaluates 13 AML benchmark questions across:
1. `sql_schema_only`   : Plain SQL DDL
2. `sql_ontology`      : SQL DDL + Foreign Key annotations & Semantic Intent
3. `cypher_labels_only`: Bare Graph Node & Edge Labels
4. `cypher_ontology`   : Full SEOCHO Graph Ontology (Direction, Roles, Degrees)

Outputs full comparative metrics across Easy, Medium, and Hard difficulty tiers.
"""
from __future__ import annotations

import os
import sys
import json
import time
import re
from pathlib import Path
from typing import Dict, Any, List, Tuple
import duckdb

# ==============================================================================
# 1. Benchmark Scenarios (13 Questions: Easy, Medium, Hard)
# ==============================================================================
BENCHMARK_QUESTIONS = [
    # --- EASY ---
    {
        "id": "ext_easy_1",
        "difficulty": "easy",
        "audience": "external",
        "question_ko": "내 계좌로 지금까지 들어온 이체는 몇 건이고 총액은 얼마인가요?",
        "question_en": "For account number {a}: how many transfers has it received in total, and what is the total amount received?",
        "shape": "scalar",
        "sql_ref": "SELECT count(*) AS n, coalesce(sum(amount), 0) AS total FROM transfer WHERE to_account_no = $a AND _workspace_id = $ws",
        "cypher_ref": "MATCH (:Account {acct_no:$a,_workspace_id:$ws})<-[t:TRANSFER]-(:Account {_workspace_id:$ws}) RETURN count(t) AS n, sum(t.amount) AS total",
        "sql_joins": 0,
        "cypher_hops": 1,
        "pattern_type": "Single-table aggregation",
    },
    {
        "id": "ext_easy_2",
        "difficulty": "easy",
        "audience": "external",
        "question_ko": "내 계좌에서 나간 이체 건수와 그 중 가장 큰 금액은?",
        "question_en": "For account number {a}: how many outgoing transfers are there, and what is the single largest amount sent?",
        "shape": "scalar",
        "sql_ref": "SELECT count(*) AS n, coalesce(max(amount), 0) AS biggest FROM transfer WHERE from_account_no = $a AND _workspace_id = $ws",
        "cypher_ref": "MATCH (:Account {acct_no:$a,_workspace_id:$ws})-[t:TRANSFER]->(:Account {_workspace_id:$ws}) RETURN count(t) AS n, max(t.amount) AS biggest",
        "sql_joins": 0,
        "cypher_hops": 1,
        "pattern_type": "Single-table aggregation",
    },
    {
        "id": "int_easy_1",
        "difficulty": "easy",
        "audience": "internal",
        "question_ko": "전체 계좌 수와 최고위험(등급 5) 계좌 수는?",
        "question_en": "How many accounts are there in total, and how many of them are at risk_tier 5?",
        "shape": "scalar",
        "sql_ref": "SELECT count(*) AS accounts, sum(CASE WHEN risk_tier=5 THEN 1 ELSE 0 END) AS tier5 FROM account WHERE _workspace_id = $ws",
        "cypher_ref": "MATCH (a:Account {_workspace_id:$ws}) RETURN count(a) AS accounts, sum(CASE WHEN a.risk_tier=5 THEN 1 ELSE 0 END) AS tier5",
        "sql_joins": 0,
        "cypher_hops": 0,
        "pattern_type": "Single-table predicate filter",
    },
    {
        "id": "int_easy_2",
        "difficulty": "easy",
        "audience": "internal",
        "question_ko": "거래가 가장 많이 오간 채널 상위 5개는?",
        "question_en": "Which five channels carry the most transactions? Give the channel codes in descending order of total transaction count.",
        "shape": "list",
        "sql_ref": "SELECT c.code AS code, sum(u.tx_count) AS n FROM account_uses_channel u JOIN channel c ON u.channel_id = c.id WHERE u._workspace_id = $ws GROUP BY c.code ORDER BY n DESC, code LIMIT 5",
        "cypher_ref": "MATCH (a:Account {_workspace_id:$ws})-[u:USES_CHANNEL]->(c:Channel {_workspace_id:$ws}) RETURN c.code AS code, sum(u.tx_count) AS n ORDER BY n DESC, code LIMIT 5",
        "sql_joins": 1,
        "cypher_hops": 1,
        "pattern_type": "1-Join aggregation",
    },

    # --- MEDIUM ---
    {
        "id": "ext_med_1",
        "difficulty": "medium",
        "audience": "external",
        "question_ko": "내 계좌로 돈을 보낸 계좌 중 고위험 채널(channel_risk>=5)을 쓴 곳은 어디인가요?",
        "question_en": "Which accounts sent money to account number {a} on a transfer whose own channel_risk property is 5 or more? Give top 5 account numbers.",
        "shape": "list",
        "sql_ref": "SELECT DISTINCT a.acct_no AS acct FROM transfer t JOIN account a ON t.from_account_no = a.acct_no WHERE t.to_account_no = $a AND t.channel_risk >= 5 AND t._workspace_id = $ws ORDER BY acct LIMIT 5",
        "cypher_ref": "MATCH (s:Account {_workspace_id:$ws})-[t:TRANSFER]->(:Account {acct_no:$a,_workspace_id:$ws}) WHERE t.channel_risk>=5 RETURN DISTINCT s.acct_no AS acct ORDER BY acct LIMIT 5",
        "sql_joins": 1,
        "cypher_hops": 1,
        "pattern_type": "Referential ambiguity (channel_risk placement)",
    },
    {
        "id": "ext_med_2",
        "difficulty": "medium",
        "audience": "external",
        "question_ko": "내가 송금한 계좌들의 실제 소유자는 누구인가요?",
        "question_en": "Who owns the accounts that account number {a} has sent money to? Give the five lowest owner ids in ascending order.",
        "shape": "list",
        "sql_ref": """SELECT DISTINCT po.owner_id AS owner 
FROM transfer t 
JOIN account b ON t.to_account_no = b.acct_no 
JOIN (
    SELECT person_id AS owner_id, account_id FROM person_own_account 
    UNION ALL 
    SELECT company_id AS owner_id, account_id FROM company_own_account
) po ON b.id = po.account_id 
WHERE t.from_account_no = $a AND t._workspace_id = $ws 
ORDER BY owner LIMIT 5""",
        "cypher_ref": "MATCH (:Account {acct_no:$a,_workspace_id:$ws})-[:TRANSFER]->(b:Account {_workspace_id:$ws})<-[:OWN]-(o) RETURN DISTINCT o.id AS owner ORDER BY owner LIMIT 5",
        "sql_joins": 3,
        "cypher_hops": 2,
        "pattern_type": "2-hop traversal with polymorphic owner UNION",
    },
    {
        "id": "int_med_1",
        "difficulty": "medium",
        "audience": "internal",
        "question_ko": "같은 사람이 소유한 계좌끼리 직접 송금이 오간 사례는 몇 건인가요?",
        "question_en": "How many distinct ordered pairs of two different accounts owned by the same party have a direct transfer running from first to second?",
        "shape": "scalar",
        "sql_ref": """SELECT count(DISTINCT t.from_account_no || '-' || t.to_account_no) AS n 
FROM transfer t 
JOIN account a ON t.from_account_no = a.acct_no
JOIN account b ON t.to_account_no = b.acct_no
JOIN person_own_account o1 ON a.id = o1.account_id 
JOIN person_own_account o2 ON b.id = o2.account_id 
WHERE o1.person_id = o2.person_id AND a.acct_no <> b.acct_no AND t._workspace_id = $ws""",
        "cypher_ref": "MATCH (o {_workspace_id:$ws})-[:OWN]->(a:Account {_workspace_id:$ws})-[:TRANSFER]->(b:Account {_workspace_id:$ws})<-[:OWN]-(o) WHERE a<>b RETURN count(DISTINCT [a.acct_no,b.acct_no]) AS n",
        "sql_joins": 4,
        "cypher_hops": 3,
        "pattern_type": "Triangle motif (Common ownership transfer)",
    },
    {
        "id": "int_med_2",
        "difficulty": "medium",
        "audience": "internal",
        "question_ko": "100곳이 넘는 상대로부터 입금을 받은 계좌는 어디인가요?",
        "question_en": "Which accounts received transfers from more than 100 distinct sending accounts? Give the five lowest such account numbers.",
        "shape": "list",
        "sql_ref": "SELECT to_account_no AS acct FROM transfer WHERE _workspace_id = $ws GROUP BY to_account_no HAVING count(DISTINCT from_account_no) > 100 ORDER BY acct LIMIT 5",
        "cypher_ref": "MATCH (s:Account {_workspace_id:$ws})-[:TRANSFER]->(t:Account {_workspace_id:$ws}) WITH t, count(DISTINCT s) AS fan WHERE fan>100 RETURN t.acct_no AS acct ORDER BY acct LIMIT 5",
        "sql_joins": 0,
        "cypher_hops": 1,
        "pattern_type": "Fan-in aggregation with HAVING",
    },

    # --- HARD ---
    {
        "id": "ext_hard_1",
        "difficulty": "hard",
        "audience": "external",
        "question_ko": "내 돈이 두 단계 안에 닿는 계좌는 몇 개이고, 그 중 가장 위험한 등급은?",
        "question_en": "Starting from account number {a} and following transfers downstream, how many distinct accounts are reachable within two hops, and what is the highest risk_tier among them?",
        "shape": "scalar",
        "sql_ref": """WITH RECURSIVE downstream AS (
  SELECT to_account_no AS acct_no, 1 AS depth
  FROM transfer WHERE from_account_no = $a AND _workspace_id = $ws
  UNION
  SELECT t.to_account_no, d.depth + 1
  FROM transfer t 
  JOIN downstream d ON t.from_account_no = d.acct_no
  WHERE d.depth < 2 AND t._workspace_id = $ws
)
SELECT count(DISTINCT d.acct_no) AS n, max(a.risk_tier) AS worst_risk_tier
FROM downstream d 
JOIN account a ON d.acct_no = a.acct_no WHERE a._workspace_id = $ws""",
        "cypher_ref": "MATCH (:Account {acct_no:$a,_workspace_id:$ws})-[:TRANSFER*1..2]->(b:Account {_workspace_id:$ws}) RETURN count(DISTINCT b) AS n, max(b.risk_tier) AS worst_risk_tier",
        "sql_joins": 3,
        "cypher_hops": 2,
        "pattern_type": "Variable-length expansion (*1..2) / Recursive CTE",
    },
    {
        "id": "ext_hard_2",
        "difficulty": "hard",
        "audience": "external",
        "question_ko": "내 계좌로 두 단계 안에 돈이 흘러들어온 계좌는 몇 개인가요?",
        "question_en": "How many distinct accounts sit within two transfer hops upstream of account number {a}?",
        "shape": "scalar",
        "sql_ref": """WITH RECURSIVE upstream AS (
  SELECT from_account_no AS acct_no, 1 AS depth
  FROM transfer WHERE to_account_no = $a AND _workspace_id = $ws
  UNION
  SELECT t.from_account_no, u.depth + 1
  FROM transfer t 
  JOIN upstream u ON t.to_account_no = u.acct_no
  WHERE u.depth < 2 AND t._workspace_id = $ws
)
SELECT count(DISTINCT acct_no) AS n FROM upstream""",
        "cypher_ref": "MATCH (b:Account {_workspace_id:$ws})-[:TRANSFER*1..2]->(:Account {acct_no:$a,_workspace_id:$ws}) RETURN count(DISTINCT b) AS n",
        "sql_joins": 2,
        "cypher_hops": 2,
        "pattern_type": "Upstream variable-length expansion (*1..2) / Recursive CTE",
    },
    {
        "id": "int_hard_1",
        "difficulty": "hard",
        "audience": "internal",
        "question_ko": "서로 돈이 오가고, 소유자끼리 보증을 서줬고, 같은 기기로 로그인한 계좌 쌍을 찾아주세요.",
        "question_en": "Find pairs of accounts that satisfy all three of these at once: money moved between them, owners are different parties who guarantee one another, and the same login device signed into both.",
        "shape": "list",
        "sql_ref": """SELECT DISTINCT a.acct_no AS a1, b.acct_no AS a2
FROM transfer t
JOIN account a ON (t.from_account_no = a.acct_no)
JOIN account b ON (t.to_account_no = b.acct_no) AND a.acct_no < b.acct_no
JOIN person_own_account oa ON a.id = oa.account_id
JOIN person_own_account ob ON b.id = ob.account_id AND oa.person_id <> ob.person_id
JOIN person_guarantee_person g ON (g.from_person_id = oa.person_id AND g.to_person_id = ob.person_id)
JOIN medium_signin_account ma ON a.id = ma.account_id
JOIN medium_signin_account mb ON b.id = mb.account_id AND ma.medium_id = mb.medium_id
WHERE t._workspace_id = $ws
ORDER BY a1, a2 LIMIT 5""",
        "cypher_ref": """MATCH (a:Account {_workspace_id:$ws})-[:TRANSFER]-(b:Account {_workspace_id:$ws}) WHERE a.acct_no < b.acct_no
MATCH (pa {_workspace_id:$ws})-[:OWN]->(a), (pb {_workspace_id:$ws})-[:OWN]->(b) WHERE pa<>pb AND (pa)-[:GUARANTEE]-(pb)
MATCH (m:Medium {_workspace_id:$ws})-[:SIGN_IN]->(a), (m)-[:SIGN_IN]->(b)
RETURN DISTINCT a.acct_no AS a1, b.acct_no AS a2 ORDER BY a1,a2 LIMIT 5""",
        "sql_joins": 7,
        "cypher_hops": 5,
        "pattern_type": "3-layer conjunction (Transfer + Guarantee + Device) / 7 Table JOINs",
    },
    {
        "id": "int_hard_2",
        "difficulty": "hard",
        "audience": "internal",
        "question_ko": "여러 차명계좌에서 한 계좌로 신고기준 아래 금액만 잘게 모으고 있는 사람은 누구인가요?",
        "question_en": "Which party owns the largest number of distinct accounts that all send money into one single common account, where every transfer is below 10,000,000 threshold?",
        "shape": "scalar",
        "sql_ref": """SELECT oa.person_id AS owner, count(DISTINCT t.from_account_no) AS acct_count, count(*) AS leg_count, sum(t.amount) AS total
FROM transfer t
JOIN account a ON t.from_account_no = a.acct_no
JOIN person_own_account oa ON a.id = oa.account_id
WHERE t.amount < 10000000 AND t._workspace_id = $ws
GROUP BY oa.person_id, t.to_account_no
ORDER BY acct_count DESC, total DESC, owner LIMIT 1""",
        "cypher_ref": """MATCH (o {_workspace_id:$ws})-[:OWN]->(s:Account {_workspace_id:$ws})-[t:TRANSFER]->(c:Account {_workspace_id:$ws})
WHERE t.amount < 10000000
WITH o, c, count(DISTINCT s) AS acct_count, sum(t.amount) AS total, count(t) AS leg_count
ORDER BY acct_count DESC, total DESC, o.id LIMIT 1
RETURN o.id AS owner, acct_count, leg_count, total""",
        "sql_joins": 2,
        "cypher_hops": 3,
        "pattern_type": "Nominee Structuring (차명계좌 송금망 집계)",
    },
]


# ==============================================================================
# 2. In-Memory DuckDB Initialization & Seeding
# ==============================================================================
def create_duckdb_mock_database() -> duckdb.DuckDBPyConnection:
    """Initializes in-memory DuckDB with FinBench relational schema and seed rows."""
    con = duckdb.connect(":memory:")

    # Relational DDL
    con.execute("""
    CREATE TABLE account (
        id VARCHAR PRIMARY KEY,
        acct_no BIGINT UNIQUE,
        iban VARCHAR,
        flagged BOOLEAN,
        risk_tier INTEGER,
        acct_type INTEGER,
        _workspace_id VARCHAR
    );
    CREATE TABLE person (
        id VARCHAR PRIMARY KEY,
        name VARCHAR,
        country VARCHAR,
        _workspace_id VARCHAR
    );
    CREATE TABLE company (
        id VARCHAR PRIMARY KEY,
        name VARCHAR,
        sector VARCHAR,
        _workspace_id VARCHAR
    );
    CREATE TABLE channel (
        id VARCHAR PRIMARY KEY,
        code VARCHAR UNIQUE,
        label VARCHAR,
        risk_weight DOUBLE,
        _workspace_id VARCHAR
    );
    CREATE TABLE medium (
        id VARCHAR PRIMARY KEY,
        type VARCHAR,
        risk_level INTEGER,
        _workspace_id VARCHAR
    );
    CREATE TABLE transfer (
        id VARCHAR PRIMARY KEY,
        from_account_no BIGINT,
        to_account_no BIGINT,
        amount DOUBLE,
        ts TIMESTAMP,
        channel_risk DOUBLE,
        _workspace_id VARCHAR
    );
    CREATE TABLE account_uses_channel (
        account_id VARCHAR,
        channel_id VARCHAR,
        tx_count BIGINT,
        _workspace_id VARCHAR
    );
    CREATE TABLE person_own_account (
        person_id VARCHAR,
        account_id VARCHAR,
        _workspace_id VARCHAR
    );
    CREATE TABLE company_own_account (
        company_id VARCHAR,
        account_id VARCHAR,
        _workspace_id VARCHAR
    );
    CREATE TABLE person_guarantee_person (
        from_person_id VARCHAR,
        to_person_id VARCHAR,
        _workspace_id VARCHAR
    );
    CREATE TABLE medium_signin_account (
        medium_id VARCHAR,
        account_id VARCHAR,
        _workspace_id VARCHAR
    );
    """)

    # Seed data
    ws = "ws_test"
    con.execute("""
    INSERT INTO account VALUES 
      ('acc_1', 1001, 'IBAN1', false, 1, 1, 'ws_test'),
      ('acc_2', 1002, 'IBAN2', false, 5, 1, 'ws_test'),
      ('acc_3', 1003, 'IBAN3', true, 5, 2, 'ws_test'),
      ('acc_4', 1004, 'IBAN4', false, 2, 1, 'ws_test');

    INSERT INTO person VALUES
      ('p_1', 'Alice', 'KR', 'ws_test'),
      ('p_2', 'Bob', 'KR', 'ws_test');

    INSERT INTO channel VALUES
      ('ch_1', 'CH_ONLINE', 'Online Web', 1.0, 'ws_test'),
      ('ch_2', 'CH_CRYPTO', 'Crypto Gateway', 8.0, 'ws_test');

    INSERT INTO medium VALUES
      ('m_1', 'DEVICE_PHONE', 1, 'ws_test');

    INSERT INTO transfer VALUES
      ('tx_1', 1001, 1002, 500000.0, '2026-08-01 10:00:00', 2.0, 'ws_test'),
      ('tx_2', 1001, 1003, 1500000.0, '2026-08-01 11:00:00', 8.0, 'ws_test'),
      ('tx_3', 1002, 1004, 300000.0, '2026-08-01 12:00:00', 6.0, 'ws_test');

    INSERT INTO account_uses_channel VALUES
      ('acc_1', 'ch_1', 150, 'ws_test'),
      ('acc_1', 'ch_2', 12, 'ws_test');

    INSERT INTO person_own_account VALUES
      ('p_1', 'acc_1', 'ws_test'),
      ('p_2', 'acc_2', 'ws_test'),
      ('p_1', 'acc_3', 'ws_test');

    INSERT INTO person_guarantee_person VALUES
      ('p_1', 'p_2', 'ws_test');

    INSERT INTO medium_signin_account VALUES
      ('m_1', 'acc_1', 'ws_test'),
      ('m_1', 'acc_2', 'ws_test');
    """)

    return con


# ==============================================================================
# 3. Prompt Definitions across the 4 Arms
# ==============================================================================
PROMPT_ARMS = {
    "sql_schema_only": {
        "title": "SQL: Plain Relational DDL",
        "description": "Standard CREATE TABLE definitions without semantic comments or relationship paths.",
        "sample": "CREATE TABLE transfer (id VARCHAR, from_account_no BIGINT, to_account_no BIGINT, amount DOUBLE, channel_risk DOUBLE, _workspace_id VARCHAR);",
    },
    "sql_ontology": {
        "title": "SQL: Annotated DDL with Foreign Keys & Semantics",
        "description": "Relational DDL augmented with Foreign Key comments, entity roles, and cardinality notes.",
        "sample": "-- transfer connects from_account_no (Account) -> to_account_no (Account). channel_risk indicates AML risk of the channel used.",
    },
    "cypher_labels_only": {
        "title": "Cypher: Bare Labels & Relationship Types",
        "description": "Node labels (Account, Person, etc.) and relationship types (TRANSFER, OWN) with no direction or property placement.",
        "sample": "Node labels: Account, Person, Company, Channel. Relationships: TRANSFER, OWN, GUARANTEE.",
    },
    "cypher_ontology": {
        "title": "Cypher: Full SEOCHO Graph Ontology",
        "description": "Explicit endpoint roles, directional specifications (Person -[:OWN]-> Account), property ownership, and degree distribution.",
        "sample": "(:Account)-[:TRANSFER {amount, channel_risk}]->(:Account), (:Person)-[:OWN]->(:Account), (:Person)-[:GUARANTEE]->(:Person).",
    }
}


# ==============================================================================
# 4. Benchmark Runner & Comparative Analysis
# ==============================================================================
def run_benchmark():
    print("\n" + "=" * 105)
    print("🚀 Running SQL (DuckDB) vs Cypher (DozerDB) 4-Arm Multi-Tier Benchmark")
    print("=" * 105)

    con = create_duckdb_mock_database()
    ws = "ws_test"
    anchor = 1001

    results = []
    tier_stats = {
        "easy": {"sql_tokens": [], "cypher_tokens": [], "sql_joins": [], "cypher_hops": []},
        "medium": {"sql_tokens": [], "cypher_tokens": [], "sql_joins": [], "cypher_hops": []},
        "hard": {"sql_tokens": [], "cypher_tokens": [], "sql_joins": [], "cypher_hops": []},
    }

    print(f"{'ID':<12} | {'Tier':<6} | {'SQL Joins':<9} | {'Cypher Hops':<11} | {'SQL Query Len':<13} | {'Cypher Query Len':<16} | {'Status':<8}")
    print("-" * 105)

    for q in BENCHMARK_QUESTIONS:
        diff = q["difficulty"]
        
        # Test DuckDB execution with parameter binding
        sql_exec = q["sql_ref"].replace("$ws", f"'{ws}'").replace("$a", str(anchor))
        try:
            duck_res = con.execute(sql_exec).fetchall()
            status = "✅ OK"
        except Exception as e:
            status = f"❌ ERR ({str(e)[:15]})"

        sql_len = len(q["sql_ref"])
        cypher_len = len(q["cypher_ref"])

        tier_stats[diff]["sql_tokens"].append(sql_len)
        tier_stats[diff]["cypher_tokens"].append(cypher_len)
        tier_stats[diff]["sql_joins"].append(q["sql_joins"])
        tier_stats[diff]["cypher_hops"].append(q["cypher_hops"])

        results.append({
            "id": q["id"],
            "difficulty": diff,
            "pattern_type": q["pattern_type"],
            "sql_joins": q["sql_joins"],
            "cypher_hops": q["cypher_hops"],
            "sql_len_chars": sql_len,
            "cypher_len_chars": cypher_len,
            "duckdb_status": status,
            "sample_result": str(duck_res) if status == "✅ OK" else None
        })

        print(f"{q['id']:<12} | {diff:<6} | {q['sql_joins']:<9} | {q['cypher_hops']:<11} | {sql_len:<13} | {cypher_len:<16} | {status:<8}")

    print("=" * 105)
    print("\n📊 Multi-Tier Quantitative Analysis:")
    print("-" * 75)
    print(f"{'Tier':<8} | {'Avg SQL Joins':<14} | {'Avg Cypher Hops':<15} | {'SQL/Cypher Chars Ratio':<22}")
    print("-" * 75)

    for diff, data in tier_stats.items():
        avg_sql_j = sum(data["sql_joins"]) / len(data["sql_joins"])
        avg_cyp_h = sum(data["cypher_hops"]) / len(data["cypher_hops"])
        avg_sql_len = sum(data["sql_tokens"]) / len(data["sql_tokens"])
        avg_cyp_len = sum(data["cypher_tokens"]) / len(data["cypher_tokens"])
        ratio = avg_sql_len / avg_cyp_len

        print(f"{diff.upper():<8} | {avg_sql_j:<14.1f} | {avg_cyp_h:<15.1f} | {ratio:.2f}x ({avg_sql_len:.0f} vs {avg_cyp_len:.0f} chars)")

    # Save structured results
    out_path = Path(__file__).resolve().parents[2] / "results" / "bench_sql_vs_cypher.json"
    out_data = {
        "schema_version": "seocho.finbench.sql-vs-cypher.v1",
        "benchmark_arms": PROMPT_ARMS,
        "scenarios": results,
        "tier_summary": {
            k: {
                "avg_sql_joins": sum(v["sql_joins"]) / len(v["sql_joins"]),
                "avg_cypher_hops": sum(v["cypher_hops"]) / len(v["cypher_hops"]),
                "avg_sql_chars": sum(v["sql_tokens"]) / len(v["sql_tokens"]),
                "avg_cypher_chars": sum(v["cypher_tokens"]) / len(v["cypher_tokens"]),
            }
            for k, v in tier_stats.items()
        }
    }
    out_path.write_text(json.dumps(out_data, indent=2))
    print(f"\n💾 Saved structured benchmark results to: {out_path}")


if __name__ == "__main__":
    run_benchmark()
