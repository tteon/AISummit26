#!/usr/bin/env python3
"""Validate the FIBO logical -> FinBench physical projection and suite coverage.

The gate is intentionally static and cheap: no MARA call should be spent until every logical
term is either mapped to an existing physical term or explicitly unsupported, and every
physical label/relationship used by the FIBO workload is exposed by the projection or a named
local extension.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, Set

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "runmeta", REPO_ROOT / "scripts" / "analysis" / "runmeta.py")
runmeta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runmeta)  # type: ignore[union-attr]


def _physical_endpoints(rel: Dict[str, Any]) -> Set[str]:
    return set(str(rel.get("source", "")).split("|")) | set(str(rel.get("target", "")).split("|"))


def _suite_terms(gold: Iterable[str]) -> tuple[Set[str], Set[str]]:
    labels: Set[str] = set()
    rels: Set[str] = set()
    for text in gold:
        for union in re.findall(r"\([^)]*?:([A-Z][A-Za-z0-9_]*(?:\|[A-Z][A-Za-z0-9_]*)*)", text):
            labels.update(union.split("|"))
        rels.update(re.findall(r"\[[^]]*?:([A-Z][A-Z0-9_]*)", text))
    return labels, rels


def _official_terms(projection: Dict[str, Any], fibo_root: Path | None,
                    errors: list[str], warnings: list[str]) -> Dict[str, Any]:
    source = projection.get("semantic_source") or {}
    terms = source.get("terms") or {}
    result: Dict[str, Any] = {"requested_release": source.get("release"),
                              "requested_commit": source.get("commit"), "terms": {}}
    if fibo_root is None:
        warnings.append("official FIBO checkout not supplied; pinned term definitions not rechecked")
        result["checkout"] = None
        return result
    try:
        actual_commit = subprocess.run(
            ["git", "-C", str(fibo_root), "rev-parse", "HEAD"], check=True,
            text=True, capture_output=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        errors.append(f"official FIBO checkout unreadable: {exc}")
        return result
    result["checkout"] = str(fibo_root)
    result["actual_commit"] = actual_commit
    if actual_commit != source.get("commit"):
        errors.append(f"official FIBO commit {actual_commit} != pinned {source.get('commit')}")
    for name, spec in terms.items():
        path = fibo_root / str(spec.get("file", ""))
        curie = str(spec.get("curie", ""))
        prefix, sep, local = curie.partition(":")
        found = False
        if path.is_file() and sep:
            text = path.read_text(errors="replace")
            pattern = rf'<owl:Class\s+rdf:about="&{re.escape(prefix)};{re.escape(local)}"'
            found = re.search(pattern, text) is not None
        result["terms"][name] = {**spec, "defined_as_owl_class": found}
        if not path.is_file():
            errors.append(f"official term {curie}: source file missing {spec.get('file')}")
        elif not found:
            errors.append(f"official term {curie}: not defined as owl:Class in {spec.get('file')}")
    return result


def validate(projection_path: Path, suite_path: Path,
             fibo_root: Path | None = None) -> Dict[str, Any]:
    projection = yaml.safe_load(projection_path.read_text())
    logical_path = REPO_ROOT / projection["logical_ontology"]
    physical_path = REPO_ROOT / projection["physical_ontology"]
    logical = yaml.safe_load(logical_path.read_text())
    physical = yaml.safe_load(physical_path.read_text())
    suite = yaml.safe_load(suite_path.read_text())

    errors: list[str] = []
    warnings: list[str] = []
    official = _official_terms(projection, fibo_root, errors, warnings)
    pnodes = physical.get("nodes", {})
    prels = physical.get("relationships", {})
    node_map = projection.get("nodes", {})
    rel_map = projection.get("relationships", {})

    for name, spec in logical.get("nodes", {}).items():
        mapping = node_map.get(name)
        if not mapping:
            errors.append(f"logical node {name}: no projection entry")
            continue
        physical_name = mapping.get("physical")
        if physical_name not in pnodes:
            errors.append(f"logical node {name}: physical node {physical_name!r} does not exist")
            continue
        declared_props = set((spec.get("properties") or {}).keys())
        mapped_props = set((mapping.get("properties") or {}).keys())
        for missing in sorted(declared_props - mapped_props):
            errors.append(f"logical node {name}.{missing}: mapping is not explicit")
        physical_props = set((pnodes[physical_name].get("properties") or {}).keys()) | {"_workspace_id"}
        for logical_prop, target in (mapping.get("properties") or {}).items():
            if isinstance(target, dict) and target.get("status") == "unavailable":
                warnings.append(f"logical node {name}.{logical_prop}: explicitly unavailable")
            elif target not in physical_props:
                errors.append(f"logical node {name}.{logical_prop}: physical property {target!r} missing")

    for name, spec in logical.get("relationships", {}).items():
        mapping = rel_map.get(name)
        if not mapping:
            errors.append(f"logical relationship {name}: no projection entry")
            continue
        if mapping.get("status") == "unsupported":
            if not mapping.get("reason"):
                errors.append(f"logical relationship {name}: unsupported without reason")
            else:
                warnings.append(f"logical relationship {name}: explicitly unsupported")
            continue
        physical_name = mapping.get("physical")
        if physical_name not in prels:
            errors.append(f"logical relationship {name}: physical type {physical_name!r} missing")
            continue
        for endpoint_key in ("source", "target"):
            logical_endpoint = mapping.get(endpoint_key)
            if logical_endpoint not in node_map:
                errors.append(f"logical relationship {name}: unknown {endpoint_key} {logical_endpoint!r}")
                continue
            mapped_endpoint = node_map[logical_endpoint].get("physical")
            physical_allowed = set(str(prels[physical_name].get(endpoint_key, "")).split("|"))
            if mapped_endpoint not in physical_allowed:
                errors.append(
                    f"logical relationship {name}: {endpoint_key} maps to {mapped_endpoint}, "
                    f"outside physical {physical_name} endpoint {sorted(physical_allowed)}")

    mapped_physical_nodes = {m.get("physical") for m in node_map.values() if m.get("physical")}
    mapped_physical_rels = {m.get("physical") for m in rel_map.values() if m.get("physical")}
    mapped_physical_rels |= set((projection.get("physical_extensions") or {}).keys())
    suite_labels, suite_rels = _suite_terms(q["gold"] for q in suite.get("questions", []))
    for label in sorted(suite_labels - mapped_physical_nodes):
        errors.append(f"suite physical label {label}: absent from projection")
    for rel in sorted(suite_rels - mapped_physical_rels):
        errors.append(f"suite physical relationship {rel}: absent from projection/extensions")

    return {
        "schema_version": "seocho.fibo.projection-validation.v1",
        "manifest": runmeta.manifest(validation="fibo logical-to-physical projection"),
        "inputs": {
            "projection": str(projection_path),
            "logical": str(logical_path.relative_to(REPO_ROOT)),
            "physical": str(physical_path.relative_to(REPO_ROOT)),
            "suite": str(suite_path),
        },
        "counts": {
            "logical_nodes": len(logical.get("nodes", {})),
            "logical_relationships": len(logical.get("relationships", {})),
            "physical_nodes": len(pnodes),
            "physical_relationships": len(prels),
            "suite_questions": len(suite.get("questions", [])),
        },
        "suite_terms": {"labels": sorted(suite_labels), "relationships": sorted(suite_rels)},
        "official_fibo": official,
        "errors": errors,
        "warnings": warnings,
        "valid": not errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projection", type=Path,
                        default=Path("ontology/fibo_finbench.projection.yaml"))
    parser.add_argument("--suite", type=Path,
                        default=Path("configs/fibo_text2cypher_suite.yaml"))
    parser.add_argument("--fibo-root", type=Path, default=None,
                        help="checkout of the pinned official edmcouncil/fibo release")
    parser.add_argument("--out", type=Path,
                        default=Path("results/analysis/fibo_projection_validation.json"))
    args = parser.parse_args()
    report = validate(args.projection, args.suite, args.fibo_root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    for warning in report["warnings"]:
        print(f"WARN {warning}")
    for error in report["errors"]:
        print(f"ERROR {error}")
    print(f"valid={report['valid']} wrote {args.out}")
    raise SystemExit(0 if report["valid"] else 1)


if __name__ == "__main__":
    main()
