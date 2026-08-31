#!/usr/bin/env python3
"""Score agent routing/policy decisions against a declarative Contract-Adherence suite."""
from __future__ import annotations
import argparse, importlib.util, json
from pathlib import Path
from typing import Any
import yaml
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location("runmeta",ROOT/"scripts/analysis/runmeta.py"); runmeta=importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(runmeta)
FIELDS=("route","completeness","truncation_limit","truncation_order","max_hops","budget_action","refusal_code","repair_allowed","mutation_receipt","disclosure_required")
def evaluate(suite: dict[str,Any], predictions: list[dict[str,Any]]) -> dict[str,Any]:
    byid={x["id"]:x for x in predictions}; rows=[]
    for case in suite["cases"]:
        expected=case["gold"]; got=byid.get(case["id"],{}); mismatches={k:{"expected":v,"actual":got.get(k)} for k,v in expected.items() if got.get(k)!=v}
        rows.append({"id":case["id"],"route_correct":got.get("route")==expected.get("route"),"policy_conformant":not mismatches,"mismatches":mismatches,"prediction":got})
    n=len(rows) or 1
    return {"schema_version":"finbench.agent-contract-adherence.v1","manifest":runmeta.manifest(validation="agent policy/router adherence"),"metrics":{"cases":len(rows),"route_accuracy":sum(x["route_correct"] for x in rows)/n,"policy_conformance":sum(x["policy_conformant"] for x in rows)/n,"avoidable_repair_rate":sum(x["prediction"].get("repair_allowed") is True and x["id"]=="unsupported_owner_type" for x in rows)/n},"episodes":rows}
def main()->None:
 p=argparse.ArgumentParser(); p.add_argument("--suite",type=Path,default=Path("configs/agent_contract_adherence_v1.yaml"));p.add_argument("--predictions",type=Path,required=True);p.add_argument("--out",type=Path,required=True);a=p.parse_args(); r=evaluate(yaml.safe_load(a.suite.read_text()),[json.loads(x) for x in a.predictions.read_text().splitlines() if x]);a.out.parent.mkdir(parents=True,exist_ok=True);a.out.write_text(json.dumps(r,indent=2)+"\n");print(json.dumps(r["metrics"]));
if __name__=="__main__":main()
