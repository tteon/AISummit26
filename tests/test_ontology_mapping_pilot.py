import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ontology_pilot", ROOT / "scripts/benchmarks/run_ontology_mapping_pilot.py")
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


def test_only_semantic_mapping_changes_and_gold_never_enters_prompt():
    case = {"question": "Count inbound payments", "params": {"workspace_id": "w", "acct_no": 7, "limit": 50},
            "result": {"columns": {"count": "integer"}}, "gold_receipt": {"rows": [{"count": 987654}]}}
    ontology = {"nodes": {"Account": {"properties": {"acct_no": {"type": "INTEGER"}}}}, "relationships": {}}
    mapping = {"version": "1", "terms": {"inbound": {"physical": "TRANSFER"}}}
    before = pilot.messages(case, ontology, mapping, "physical_schema", 4)
    after = pilot.messages(case, ontology, mapping, "business_mapping", 4)
    assert before[0] == after[0]
    body = json.loads(after[1]["content"])
    assert body.pop("business_semantics")["terms"] == mapping["terms"]
    assert body == json.loads(before[1]["content"])
    assert "987654" not in pilot.stable(after)


def test_exact_columns_integer_types_and_order_are_part_of_the_answer():
    contract = {"columns": {"account": "integer"}}
    gold = [{"account": 1}, {"account": 2}]
    assert pilot.score_rows(gold, gold, ["account"], contract)
    assert not pilot.score_rows(list(reversed(gold)), gold, ["account"], contract)
    assert not pilot.score_rows([{"account": True}, {"account": 2}], gold, ["account"], contract)
    assert not pilot.score_rows([{"account": "1"}, {"account": 2}], gold, ["account"], contract)
    assert not pilot.score_rows(gold, gold, ["wrong_alias"], contract)


def test_anchor_scope_and_row_cap_cannot_be_reassigned_by_model():
    params = {"workspace_id": "w", "acct_no": 7, "limit": 10}
    good = "MATCH (a:Account {_workspace_id:$workspace_id,acct_no:$acct_no})-[:TRANSFER]->(b:Account {_workspace_id:$workspace_id}) RETURN b.acct_no AS account LIMIT $limit"
    assert pilot.scope_gate(good, params) == []
    assert "harness_anchor_required" in pilot.scope_gate(good.replace("acct_no:$acct_no", "acct_no:7"), params)
    assert "unscoped_node" in pilot.scope_gate(good.replace("(b:Account {_workspace_id:$workspace_id})", "(b:Account)"), params)
    assert "harness_limit_required" in pilot.scope_gate(good.replace("LIMIT $limit", "LIMIT 999"), params)
    assert "unsupported_query_surface" in pilot.scope_gate(good + "; CREATE (:Account)", params)


def test_scoped_variable_reuse_is_allowed():
    query = "MATCH (p:Person {_workspace_id:$workspace_id})-[:OWN]->(a:Account {_workspace_id:$workspace_id,acct_no:$acct_no}) MATCH (p)-[:APPLY]->(l:Loan {_workspace_id:$workspace_id}) RETURN l.id AS loan LIMIT $limit"
    assert pilot.scope_gate(query, {"workspace_id": "w", "acct_no": 1, "limit": 50}) == []


def test_binding_validation_rejects_missing_values_and_old_identifier_encoding():
    specs = {"ratio": {"type": "number", "exclusive_minimum": 0, "maximum": 1},
             "company_id": {"type": "string", "pattern": "^Company:[0-9]+$"}}
    assert pilot.validate_parameters(specs, {"ratio": .75, "company_id": "Company:4"}) == []
    assert pilot.validate_parameters(specs, {"ratio": 0, "company_id": "C4"})
    assert pilot.validate_parameters(specs, {"ratio": float("nan")})


def test_plan_gate_uses_leaf_cost_even_when_operator_says_seek():
    receipt = {"query_type": "r", "plan": {"operatorType": "NodeIndexSeek", "args": {"EstimatedRows": 200470}, "children": []}}
    assert pilot.plan_gate(receipt, 5000) == ["leaf_estimate_over_budget"]
    receipt["plan"]["args"]["EstimatedRows"] = 1
    assert not pilot.plan_gate(receipt, 2)


def test_partial_or_invalid_pair_cannot_support_a_positive_decision():
    rows = [{"question_id": "q", "repeat": 0, "arm": "a", "correct": False, "valid": True,
             "prompt_tokens": 10, "completion_tokens": 5},
            {"question_id": "q", "repeat": 0, "arm": "b", "correct": True, "valid": False,
             "prompt_tokens": 10, "completion_tokens": 5}]
    report = pilot.summarize(rows, ["a", "b"])
    assert report["complete_valid_pairs"] == 0
    assert report["conclusion"] == "inconclusive"
