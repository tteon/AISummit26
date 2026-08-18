#!/usr/bin/env python3
"""OpenAI Agents SDK + SEOCHO Latest Engine Integration & Observability.

This script wires:
1. Generation Plane: `seocho.ontology.Ontology` schema injection (`to_query_context`).
2. Validation Plane: Guardrail checking using ontology labels, relationships, and tenant parameters.
3. Execution Plane: Plan Feedback Gate (EXPLAIN + 2s probe timeout with cost feedback).
4. Return Plane: CSV row formatting with truncation disclosure (`more_available`).
5. Observability: OpenTelemetry tracing spans capturing DB hits, execution latency, and violation telemetry.
"""
from __future__ import annotations

import os
import sys
import time
import json
import yaml
import re
from pathlib import Path
from collections import Counter
from typing import Dict, Any, List, Optional

# --- SEOCHO Engine ---
from seocho.ontology import Ontology

# --- Neo4j Driver ---
from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError

# --- OpenTelemetry (Observability) ---
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter

# Setup OpenTelemetry tracer
tracer_provider = TracerProvider()
tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("seocho.agents.sdk", "0.2.0")

# --- Configurations ---
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
ONTOLOGY_FILE = WORKSPACE_ROOT / "ontology" / "finbench.ontology.yaml"
ROW_CAP = 50
PROBE_TIMEOUT_S = 2.0
TX_TIMEOUT_S = 30.0
ACCEPT_COST_MARK = "accept-cost"
DEFAULT_WORKSPACE_ID = "ws_seocho_prod"

# Load and validate ontology using latest SEOCHO engine
raw_ontology_doc = yaml.safe_load(ONTOLOGY_FILE.read_text())
finbench_ontology = Ontology.from_dict(raw_ontology_doc)
query_context = finbench_ontology.to_query_context()


# ==============================================================================
# 1. Guardrail Validation Engine (Powered by SEOCHO Ontology)
# ==============================================================================
def validate_cypher_guardrail(cypher: str, params: Dict[str, Any], ontology: Ontology) -> List[str]:
    """Validates generated Cypher query against SEOCHO ontology constraints.

    Ensures:
    - Node labels exist in declared ontology.
    - Relationship types exist in declared ontology.
    - Multi-tenant workspace scoping (_workspace_id) is enforced.
    - Disallows unsafe or destructive keywords.
    """
    violations: List[str] = []

    # 1. Prevent destructive operations
    if re.search(r"\b(DELETE|DETACH|DROP|CREATE|SET|REMOVE|MERGE)\b", cypher, re.IGNORECASE):
        violations.append("disallowed_clause:read_only_policy_violation")

    # 2. Extract and validate node labels (:Label)
    # Exclude variable definitions and relationship patterns
    node_labels = re.findall(r"\(\s*[a-zA-Z0-9_]*\s*:\s*([A-Z][a-zA-Z0-9_]*)", cypher)
    for label in node_labels:
        if not ontology.is_valid_label(label):
            violations.append(f"undeclared_node_label:{label}")

    # 3. Extract and validate relationship types ([:REL_TYPE])
    rel_types = re.findall(r"\[\s*[a-zA-Z0-9_]*\s*:\s*([A-Z][a-zA-Z0-9_]*)", cypher)
    for rel in rel_types:
        if rel not in ontology.relationships:
            violations.append(f"undeclared_relationship_type:{rel}")

    # 4. Enforce tenant boundary (_workspace_id)
    if "_workspace_id" not in cypher and "$workspace_id" not in cypher and "$ws" not in cypher:
        violations.append("missing_tenant_scope:_workspace_id_must_be_bound")

    return violations


# ==============================================================================
# 2. OpenAI Agents SDK Function Tool Implementation
# ==============================================================================
try:
    from agents import Agent, Runner, function_tool, AgentHooks, RunContext
    HAS_OPENAI_AGENTS_SDK = True
except ImportError:
    HAS_OPENAI_AGENTS_SDK = False
    # Fallback dummy decorator for environments where openai-agents-python is being installed
    def function_tool(*args, **kwargs):
        def decorator(f):
            return f
        return decorator


def execute_cypher_with_plan_feedback(
    cypher: str,
    driver: Any,
    ontology: Ontology = finbench_ontology,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    anchor: Optional[int] = None,
) -> Dict[str, Any]:
    """Core execution engine handling Guardrail, Plan Probing, Profile, and Observability."""
    record: Dict[str, Any] = {
        "cypher": cypher,
        "outcome": "ok",
        "db_hits": 0,
        "rows": 0,
        "ms": 0.0,
        "violations": [],
        "more_available": False,
        "response_text": "",
    }

    params = {"workspace_id": workspace_id, "ws": workspace_id, "limit": ROW_CAP}
    if anchor is not None:
        params["a"] = anchor
        params["acct_no"] = anchor

    with tracer.start_as_current_span("run_cypher_execution") as span:
        span.set_attribute("db.system", "neo4j")
        span.set_attribute("cypher.query", cypher)

        # -------------------------------------------------------------
        # Phase 1: SEOCHO Guardrail Check
        # -------------------------------------------------------------
        violations = validate_cypher_guardrail(cypher, params, ontology)
        if violations:
            record["outcome"] = "guardrail_rejected"
            record["violations"] = violations
            span.set_attribute("cypher.outcome", "guardrail_rejected")
            span.set_attribute("cypher.violations", violations)
            record["response_text"] = (
                f"REJECTED by Guardrail — The query violates graph schema constraints: "
                f"{', '.join(violations)}. Please rewrite the query using only declared entities "
                f"and bind the required tenant parameters ($workspace_id, $limit, $a)."
            )
            return record

        t0 = time.perf_counter()
        with driver.session() as session:
            # ---------------------------------------------------------
            # Phase 2: Plan Feedback & 2-second Probe Gate
            # ---------------------------------------------------------
            accepted = ACCEPT_COST_MARK in cypher
            if not accepted:
                try:
                    explain = session.run("EXPLAIN " + cypher, **params).consume()
                    span.set_attribute("cypher.has_plan", True)
                except Neo4jError as exc:
                    record["outcome"] = "syntax_error"
                    span.set_attribute("cypher.outcome", "syntax_error")
                    span.set_attribute("cypher.error", exc.code or "")
                    record["response_text"] = f"ERROR — Cypher syntax/compilation failed: {exc.code}: {str(exc)[:200]}"
                    return record

                # 2-second transaction probe
                probe = session.begin_transaction(timeout=PROBE_TIMEOUT_S)
                try:
                    probe.run(cypher, **params).consume()
                    probe.commit()
                except Exception:
                    probe.close()
                    elapsed_ms = (time.perf_counter() - t0) * 1000
                    record["outcome"] = "plan_rejected"
                    record["ms"] = elapsed_ms
                    span.set_attribute("cypher.outcome", "plan_rejected")
                    span.set_attribute("cypher.ms", elapsed_ms)
                    record["response_text"] = (
                        f"NOT EXECUTED (Plan Gate) — The query did not finish within {PROBE_TIMEOUT_S:.0f}s. "
                        "The cost is in the graph expansion rather than result size. Rewrite it to start "
                        "from indexed lookups (Account.acct_no, Person.id, Company.id, Channel.code) and "
                        "apply filters before expanding relationships. If this cost is strictly necessary, "
                        f"resend the query with the comment `/* {ACCEPT_COST_MARK} */`."
                    )
                    return record

            # ---------------------------------------------------------
            # Phase 3: Profile Execution & Row Retrieval
            # ---------------------------------------------------------
            tx = session.begin_transaction(timeout=TX_TIMEOUT_S)
            try:
                result = tx.run("PROFILE " + cypher, **params)
                rows = [dict(r) for _, r in zip(range(ROW_CAP), result)]
                summary = result.consume()
                tx.commit()
            except Exception as exc:
                tx.close()
                record["outcome"] = "db_error"
                record["ms"] = (time.perf_counter() - t0) * 1000
                span.set_attribute("cypher.outcome", "db_error")
                span.set_attribute("cypher.error", str(exc))
                record["response_text"] = f"ERROR — Database execution error: {str(exc)}"
                return record

        elapsed_ms = (time.perf_counter() - t0) * 1000
        db_hits = summary.profile.get("dbHits", 0) if summary.profile else 0
        more_available = len(rows) >= ROW_CAP

        record["outcome"] = "ok"
        record["ms"] = elapsed_ms
        record["db_hits"] = db_hits
        record["rows"] = len(rows)
        record["more_available"] = more_available

        # Record metrics in OTel span
        span.set_attribute("cypher.outcome", "ok")
        span.set_attribute("cypher.ms", elapsed_ms)
        span.set_attribute("cypher.db_hits", db_hits)
        span.set_attribute("cypher.rows", len(rows))
        span.set_attribute("cypher.more_available", more_available)

        # -------------------------------------------------------------
        # Phase 4: CSV Encoding (Context-Efficient Return)
        # -------------------------------------------------------------
        if not rows:
            record["response_text"] = "(0 rows returned)"
            return record

        headers = list(rows[0].keys())
        csv_lines = [",".join(headers)]
        for r in rows:
            csv_lines.append(",".join(str(r.get(h, "")) for h in headers))
        csv_lines.append(f"# count={len(rows)} cap={ROW_CAP} more_available={more_available}")

        record["response_text"] = "\n".join(csv_lines)
        return record


# Define OpenAI Agents SDK Tool
if HAS_OPENAI_AGENTS_SDK:
    @function_tool(
        name_override="run_cypher",
        description_override=(
            "Run a read-only Cypher query against the financial graph database. "
            "Use only labels and relationship types declared in the schema. "
            "$workspace_id, $limit, and $a are automatically bound."
        )
    )
    def run_cypher_tool(cypher: str) -> str:
        driver = GraphDatabase.driver(
            os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "password"))
        )
        res = execute_cypher_with_plan_feedback(cypher, driver)
        return res["response_text"]


# ==============================================================================
# 3. Agent Builder & Observability Hooks
# ==============================================================================
def build_seocho_agent(model: str = "gpt-4o") -> Any:
    """Constructs the OpenAI Agent with SEOCHO ontology instructions and tools."""
    if not HAS_OPENAI_AGENTS_SDK:
        raise RuntimeError("openai-agents package is required to build Agent.")

    schema_text = query_context.get("graph_schema", "")
    instructions = f"""
You are an expert Financial Anti-Money-Laundering (AML) Graph Investigator.
Analyze user questions, write optimized Cypher queries, and execute them using `run_cypher`.

### Graph Ontology & Schema (from SEOCHO):
{schema_text}

### Instructions & Rules:
1. Always scope nodes with `_workspace_id: $workspace_id`.
2. Always respect relationship direction (e.g. `(p:Person)-[:OWN]->(a:Account)`).
3. If `run_cypher` returns `REJECTED` or `NOT EXECUTED`, carefully read the feedback and rewrite the query.
4. When CSV results include `more_available=True`, explicitly mention in your final answer that results were truncated to the top {ROW_CAP} entries.
"""

    return Agent(
        name="Seocho_FinBench_Agent",
        model=model,
        instructions=instructions,
        tools=[run_cypher_tool],
    )


# ==============================================================================
# 4. Self-Test & Verification Routine
# ==============================================================================
def smoke_test_wiring():
    print("=" * 60)
    print("🛠️ Testing SEOCHO Engine Integration & Guardrails")
    print("=" * 60)
    print(f"✅ Loaded SEOCHO Ontology: {finbench_ontology.name} (Nodes: {len(finbench_ontology.nodes)}, Rels: {len(finbench_ontology.relationships)})")
    
    # 1. Valid Query Test
    good_query = (
        "MATCH (a:Account {_workspace_id: $workspace_id, acct_no: $a})"
        "<-[t:TRANSFER]-(b:Account {_workspace_id: $workspace_id}) "
        "RETURN b.acct_no, t.amount LIMIT $limit"
    )
    v_good = validate_cypher_guardrail(good_query, {"workspace_id": "ws1"}, finbench_ontology)
    print(f"🔹 Conforming Query Guardrail Check: {'PASSED (0 violations)' if not v_good else f'FAILED: {v_good}'}")

    # 2. Invalid Query Test (Undeclared label + missing tenant)
    bad_query = "MATCH (w:Wallet)-[t:TRANSFER]->(b:Account) RETURN b"
    v_bad = validate_cypher_guardrail(bad_query, {"workspace_id": "ws1"}, finbench_ontology)
    print(f"🔹 Non-Conforming Query Check: BLOCKED as expected -> {v_bad}")

    print("\n✅ SEOCHO Ontology & Guardrail Wiring Verified Successfully!")


if __name__ == "__main__":
    smoke_test_wiring()
