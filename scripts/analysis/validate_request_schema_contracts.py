#!/usr/bin/env python3
"""Statically validate the versioned real-world request -> FinBench contract catalog.

This gate is intentionally prior to any paid model call.  It checks only declared contract
surfaces: business terms are never accepted as physical schema names, and every physical
label, relationship, property and directional endpoint must be defined by FinBench.
"""
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


def _parts(name: str) -> set[str]:
    return set(str(name).split("|"))


def validate(catalog_path: Path, physical_path: Path, projection_path: Path) -> dict[str, Any]:
    catalog = yaml.safe_load(catalog_path.read_text())
    physical = yaml.safe_load(physical_path.read_text())
    projection = yaml.safe_load(projection_path.read_text())
    nodes, rels = physical["nodes"], physical["relationships"]
    semantic_terms = set((projection.get("semantic_source", {}).get("terms") or {}).keys())
    errors: list[str] = []
    request_reports = []
    required = {"id", "domain", "difficulty", "real_world_case", "user_request", "parameters",
                "physical", "semantic_terms", "result", "verification", "plan_risk", "gold"}
    ids: set[str] = set()
    for request in catalog.get("requests", []):
        ident = request.get("id", "<missing>")
        missing = sorted(required - set(request))
        local: list[str] = [f"missing {x}" for x in missing]
        if ident in ids:
            local.append("duplicate id")
        ids.add(ident)
        surface = request.get("physical") or {}
        for label in surface.get("nodes", []):
            if label not in nodes:
                local.append(f"unknown physical node {label}")
        for relation in surface.get("relationships", []):
            if relation not in rels:
                local.append(f"unknown physical relationship {relation}")
        for prop in surface.get("properties", []):
            try:
                label, key = prop.split(".", 1)
                available = set((nodes[label].get("properties") or {}).keys()) if label in nodes else set()
                if label in rels:
                    available = set((rels[label].get("properties") or {}).keys())
                if key not in available:
                    local.append(f"unknown physical property {prop}")
            except ValueError:
                local.append(f"property must use Type.property: {prop}")
        for term in request.get("semantic_terms", []):
            if term not in semantic_terms:
                local.append(f"unknown semantic term {term}")
        for direction in surface.get("directions", []):
            try:
                left, relation, right = direction.replace("<-", "-").replace("->", "-").split("-")
                if relation not in rels:
                    local.append(f"direction references unknown relationship {relation}")
                else:
                    declared = rels[relation]
                    # Arrows mean physical source -> target.  Undirected spelling is forbidden.
                    if "<-" in direction:
                        source, target = right, left
                    elif "->" in direction:
                        source, target = left, right
                    else:
                        local.append(f"direction must include an arrow: {direction}")
                        continue
                    if not _parts(source) <= _parts(declared["source"]):
                        local.append(f"direction source {source} outside {relation} source {declared['source']}")
                    if not _parts(target) <= _parts(declared["target"]):
                        local.append(f"direction target {target} outside {relation} target {declared['target']}")
            except ValueError:
                local.append(f"invalid direction {direction}")
        result = request.get("result") or {}
        if not (result.get("columns") or {}):
            local.append("result contract has no columns")
        risk = request.get("plan_risk") or {}
        if not risk.get("class") or not risk.get("scope") or not risk.get("expansion_bound"):
            local.append("plan risk must declare class, scope and expansion_bound")
        request_reports.append({"id": ident, "valid": not local, "errors": local})
        errors.extend(f"{ident}: {error}" for error in local)
    return {"schema_version": "finbench.request-schema-contract-validation.v1",
            "manifest": runmeta.manifest(validation="request-to-FinBench contract"),
            "inputs": {"catalog": str(catalog_path), "physical": str(physical_path), "projection": str(projection_path)},
            "counts": {"requests": len(request_reports), "domains": len({r.get('domain') for r in catalog.get('requests', [])})},
            "requests": request_reports, "errors": errors, "valid": not errors}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=Path("configs/agentic_request_schema_contracts_v2.yaml"))
    parser.add_argument("--physical", type=Path, default=Path("ontology/finbench.ontology.yaml"))
    parser.add_argument("--projection", type=Path, default=Path("ontology/fibo_finbench.projection.yaml"))
    parser.add_argument("--out", type=Path, default=Path("results/analysis/request_schema_contracts_v2_validation.json"))
    args = parser.parse_args()
    report = validate(args.catalog, args.physical, args.projection)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"valid={report['valid']} requests={report['counts']['requests']} wrote {args.out}")
    for error in report["errors"]:
        print(f"ERROR {error}")
    raise SystemExit(0 if report["valid"] else 1)


if __name__ == "__main__":
    main()
