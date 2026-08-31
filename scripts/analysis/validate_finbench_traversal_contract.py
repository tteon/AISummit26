#!/usr/bin/env python3
"""Static gate for FinBench hub-truncation Agent API contracts."""
from __future__ import annotations
import argparse, importlib.util, json
from pathlib import Path
from typing import Any
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("runmeta", ROOT / "scripts/analysis/runmeta.py")
runmeta = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(runmeta)
ORDERS = {"TIMESTAMP_ASCENDING", "TIMESTAMP_DESCENDING", "AMOUNT_ASCENDING", "AMOUNT_DESCENDING"}
WORKLOADS = {"transaction_simple_read", "transaction_complex_read", "transaction_write", "transaction_read_write", "analytics"}

def validate(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text()); errors=[]; rows=[]
    for name, policy in (data.get("policies") or {}).items():
        local=[]; traversal=policy.get("traversal_policy") or {}; envelope=set(policy.get("required_result_envelope") or [])
        if policy.get("workload_class") not in WORKLOADS: local.append("unknown workload_class")
        if policy.get("completeness") == "bounded_deterministic_sample":
            if not isinstance(traversal.get("truncation_limit"), int) or traversal["truncation_limit"] <= 0: local.append("bounded policy needs positive truncation_limit")
            if traversal.get("truncation_order") not in ORDERS: local.append("bounded policy needs a permitted deterministic order")
            missing={"applied_limit", "applied_order", "has_more", "execution_receipt"}-envelope
            if missing: local.append(f"bounded policy missing envelope fields {sorted(missing)}")
        if policy.get("completeness") == "complete_required" and traversal.get("truncation_limit") is not None: local.append("complete-required policy cannot truncate")
        if policy.get("workload_class") == "transaction_write":
            if not {"write_receipt", "idempotency_key", "transaction_outcome"} <= envelope: local.append("write policy lacks mutation receipt")
        if policy.get("on_budget_exhaustion") not in {"return_partial", "escalate_async", "refuse"}: local.append("invalid budget action")
        rows.append({"policy":name,"valid":not local,"errors":local}); errors += [f"{name}: {x}" for x in local]
    return {"schema_version":"finbench.agent-traversal-contract-validation.v1", "manifest":runmeta.manifest(validation="FinBench traversal contract"), "policies":rows,"errors":errors,"valid":not errors}

def main() -> None:
    p=argparse.ArgumentParser(); p.add_argument("--contract",type=Path,default=Path("configs/finbench_agent_traversal_contract_v1.yaml")); p.add_argument("--out",type=Path,default=Path("results/analysis/finbench_agent_traversal_contract_v1_validation.json")); a=p.parse_args(); r=validate(a.contract); a.out.parent.mkdir(parents=True,exist_ok=True); a.out.write_text(json.dumps(r,indent=2)+"\n"); print(f"valid={r['valid']} policies={len(r['policies'])} wrote {a.out}"); raise SystemExit(0 if r['valid'] else 1)
if __name__ == "__main__": main()
