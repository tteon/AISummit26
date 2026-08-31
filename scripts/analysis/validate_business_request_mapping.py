#!/usr/bin/env python3
"""Validate the business-vocabulary -> FIBO/local -> physical FinBench mapping."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("runmeta", ROOT / "scripts/analysis/runmeta.py")
runmeta = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(runmeta)


def _walk(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [x for item in value for x in _walk(item)]
    if isinstance(value, dict):
        return [x for item in value.values() for x in _walk(item)]
    return []


def validate(mapping_path: Path, physical_path: Path, projection_path: Path) -> dict[str, Any]:
    mapping = yaml.safe_load(mapping_path.read_text())
    physical = yaml.safe_load(physical_path.read_text())
    projection = yaml.safe_load(projection_path.read_text())
    nodes, rels = physical["nodes"], physical["relationships"]
    fibo_terms = set((projection.get("semantic_source", {}).get("terms") or {}))
    logical_relationships = set((projection.get("relationships") or {}))
    errors: list[str] = []
    reports = []
    for name, term in (mapping.get("terms") or {}).items():
        local: list[str] = []
        status = term.get("status")
        aliases = term.get("aliases") or []
        if not aliases:
            local.append("has no aliases")
        if status not in {"mapped", "derived", "mapped_with_limitation", "unsupported"}:
            local.append(f"unknown status {status!r}")
        if status == "unsupported":
            if not term.get("refusal_code") or not term.get("reason"):
                local.append("unsupported term needs refusal_code and reason")
        elif not term.get("physical"):
            local.append("executable term has no physical mapping")
        if status == "mapped_with_limitation" and not term.get("limitation"):
            local.append("limited mapping has no limitation")
        semantic = term.get("semantic") or {}
        fibo = semantic.get("fibo_term")
        relationship = semantic.get("relationship")
        if fibo and fibo not in fibo_terms:
            local.append(f"unknown FIBO term {fibo}")
        if relationship and relationship not in logical_relationships:
            local.append(f"unknown projected relationship {relationship}")
        for field, value in (term.get("physical") or {}).items():
            for text in _walk(value):
                # Direct canonical declarations are checked; free-form Cypher snippets remain
                # documented compiler templates and are covered by request-contract validation.
                if field == "node" and text not in nodes:
                    local.append(f"unknown physical node {text}")
                if field == "relationship" and text not in rels:
                    local.append(f"unknown physical relationship {text}")
                if field in {"properties", "dependencies"} and "." in text:
                    owner, prop = text.split(".", 1)
                    available = (nodes.get(owner, {}).get("properties") or {}) if owner in nodes else (rels.get(owner, {}).get("properties") or {})
                    if prop not in available:
                        local.append(f"unknown physical property {text}")
        reports.append({"term": name, "valid": not local, "errors": local})
        errors.extend(f"{name}: {item}" for item in local)
    return {"schema_version": "finbench.business-vocabulary-mapping-validation.v1",
            "manifest": runmeta.manifest(validation="business vocabulary to FinBench mapping"),
            "inputs": {"mapping": str(mapping_path), "physical": str(physical_path), "projection": str(projection_path)},
            "counts": {"terms": len(reports)}, "terms": reports, "errors": errors, "valid": not errors}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping", type=Path, default=Path("ontology/business_request_finbench.mapping.yaml"))
    parser.add_argument("--physical", type=Path, default=Path("ontology/finbench.ontology.yaml"))
    parser.add_argument("--projection", type=Path, default=Path("ontology/fibo_finbench.projection.yaml"))
    parser.add_argument("--out", type=Path, default=Path("results/analysis/business_request_finbench_mapping_validation.json"))
    args = parser.parse_args()
    report = validate(args.mapping, args.physical, args.projection)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"valid={report['valid']} terms={report['counts']['terms']} wrote {args.out}")
    for error in report["errors"]:
        print(f"ERROR {error}")
    raise SystemExit(0 if report["valid"] else 1)


if __name__ == "__main__":
    main()
