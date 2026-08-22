#!/usr/bin/env python3
"""Does the declared ontology describe the graph that is actually loaded?

Everything text2cypher does rests on this. The ontology is what goes into the prompt, what
`policy_from_ontology` validates against, and what the grammar is generated from. If it has
drifted from the graph, all three are built on a wrong description, and the failure shows up
as a model that "hallucinates" a label which is in fact real, or as a validator that passes a
query the database then rejects.

Four checks, and the direction of each mismatch means something different:

* **declared but absent** — the prompt advertises a label, relationship or property that does
  not exist. The model uses it, the query returns nothing, and nothing errors.
* **present but undeclared** — the graph has something the ontology hides. The validator
  rejects a correct query, and the repair loop burns generations trying to guess a schema that
  cannot express the question.
* **identity keys** — the properties the ontology names as identities are the ones text2cypher
  anchors on. If they are not indexed, every anchored query is a sweep, which the runtime
  measurements showed costs 4.2x on an engine without the parallel runtime.
* **relationship endpoints** — a declared (source)-[type]->(target) triple that never occurs
  lets the grammar generate a pattern that can only ever match nothing.

Read-only. Nothing here needs an LLM.

    python3 scripts/analysis/ontology_conformance.py --password "$NEO4J_PASSWORD" \
        --uri bolt://localhost:7688 --database finbenchl1
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location("runmeta", REPO_ROOT / "scripts" / "analysis" / "runmeta.py")
runmeta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runmeta)  # type: ignore[union-attr]


def graph_facts(driver, database: str) -> Dict[str, Any]:
    """What the database actually contains, read from its own schema procedures and counts."""
    out: Dict[str, Any] = {}
    with driver.session(database=database) as s:
        out["labels"] = sorted(r["label"] for r in s.run("CALL db.labels() YIELD label RETURN label"))
        out["rel_types"] = sorted(r["relationshipType"] for r in s.run(
            "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType"))
        # Property keys per label, from the data rather than from db.propertyKeys(), which is
        # global and cannot say which label carries which key.
        props: Dict[str, List[str]] = {}
        counts: Dict[str, int] = {}
        for label in out["labels"]:
            rec = s.run(f"MATCH (n:`{label}`) RETURN count(n) AS c").single()
            counts[label] = rec["c"] if rec else 0
            keys = s.run(f"MATCH (n:`{label}`) WITH keys(n) AS k LIMIT 5000 "
                         "UNWIND k AS key RETURN DISTINCT key ORDER BY key")
            props[label] = [r["key"] for r in keys]
        out["node_properties"] = props
        out["node_counts"] = counts
        rprops: Dict[str, List[str]] = {}
        rcounts: Dict[str, int] = {}
        for rt in out["rel_types"]:
            rec = s.run(f"MATCH ()-[r:`{rt}`]->() RETURN count(r) AS c").single()
            rcounts[rt] = rec["c"] if rec else 0
            keys = s.run(f"MATCH ()-[r:`{rt}`]->() WITH keys(r) AS k LIMIT 5000 "
                         "UNWIND k AS key RETURN DISTINCT key ORDER BY key")
            rprops[rt] = [r["key"] for r in keys]
        out["rel_properties"] = rprops
        out["rel_counts"] = rcounts
        # Which (source label, type, target label) triples occur, so a declared endpoint pair
        # that never happens can be told apart from one that does.
        out["rel_endpoints"] = sorted(
            f'{r["src"]}-[{r["t"]}]->{r["dst"]}' for r in s.run(
                "MATCH (a)-[r]->(b) WITH labels(a) AS la, type(r) AS t, labels(b) AS lb "
                "UNWIND la AS src UNWIND lb AS dst "
                "RETURN DISTINCT src, t, dst ORDER BY src, t, dst"))
        out["indexes"] = [
            {"name": r.get("name"), "labels": r.get("labelsOrTypes"),
             "properties": r.get("properties"), "type": r.get("type"),
             "state": r.get("state")}
            for r in s.run("SHOW INDEXES YIELD name, labelsOrTypes, properties, type, state "
                           "RETURN name, labelsOrTypes, properties, type, state")]
    return out


def declared(onto: Any) -> Dict[str, Any]:
    """The ontology as the prompt and the policy see it."""
    nodes = getattr(onto, "nodes", None) or {}
    rels = getattr(onto, "relationships", None) or {}

    def props_of(spec: Any) -> List[str]:
        p = getattr(spec, "properties", None)
        if p is None and isinstance(spec, dict):
            p = spec.get("properties")
        return sorted((p or {}).keys())

    def ident_of(spec: Any) -> List[str]:
        k = getattr(spec, "identity_keys", None)
        if k is None and isinstance(spec, dict):
            k = spec.get("identity_keys")
        return list(k or [])

    def _labels(v: Any) -> List[str]:
        """Endpoint labels, with the ontology's union syntax expanded.

        `source: Person|Company` is one declaration covering two labels. Treating it as a
        literal label reported four real relationships as "declared but never occurring",
        which was a bug in this check rather than drift in the ontology."""
        if v is None:
            return []
        vals = v if isinstance(v, (list, tuple)) else [v]
        out: List[str] = []
        for item in vals:
            out.extend(part.strip() for part in str(item).split("|") if part.strip())
        return out

    def endpoints_of(name: str, spec: Any) -> List[str]:
        src = getattr(spec, "source", None) or (spec.get("source") if isinstance(spec, dict) else None)
        dst = getattr(spec, "target", None) or (spec.get("target") if isinstance(spec, dict) else None)
        return [f"{a}-[{name}]->{b}" for a in _labels(src) for b in _labels(dst)]

    eps: List[str] = []
    for name, spec in rels.items():
        eps.extend(endpoints_of(name, spec))
    return {
        "labels": sorted(nodes.keys()),
        "rel_types": sorted(rels.keys()),
        "node_properties": {k: props_of(v) for k, v in nodes.items()},
        "rel_properties": {k: props_of(v) for k, v in rels.items()},
        "identity_keys": {k: ident_of(v) for k, v in nodes.items()},
        "rel_endpoints": sorted(set(eps)),
    }


def diff(name: str, decl: Set[str], real: Set[str]) -> Dict[str, Any]:
    return {"scope": name,
            "declared_but_absent": sorted(decl - real),
            "present_but_undeclared": sorted(real - decl),
            "agreed": sorted(decl & real)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", default="bolt://localhost:7688")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", required=True)
    ap.add_argument("--database", default="finbenchl1")
    ap.add_argument("--ontology", default="ontology/finbench.ontology.yaml")
    ap.add_argument("--seocho-src", default=None)
    ap.add_argument("--db-container", default="aisummit-simtest")
    ap.add_argument("--out", default="results/analysis/ontology_conformance.json")
    args = ap.parse_args()

    import yaml
    from harness.seocho_bridge import _ensure_seocho_on_path
    _ensure_seocho_on_path(args.seocho_src)
    from seocho.ontology import Ontology
    from seocho.query.hybrid_planner import policy_from_ontology
    raw = yaml.safe_load(Path(args.ontology).read_text())
    onto = Ontology.from_dict(raw)
    policy = policy_from_ontology(onto)
    d = declared(onto)

    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    g = graph_facts(driver, args.database)
    driver.close()

    findings: List[Dict[str, Any]] = [
        diff("labels", set(d["labels"]), set(g["labels"])),
        diff("relationship_types", set(d["rel_types"]), set(g["rel_types"])),
    ]
    # Properties, per label, because a global property-key diff hides which label is wrong.
    prop_rows = []
    for label in sorted(set(d["labels"]) | set(g["labels"])):
        dp = set(d["node_properties"].get(label, []))
        rp = set(g["node_properties"].get(label, []))
        # `_workspace_id` and the harness's derived `_out_degree` are infrastructure, not
        # domain schema; the ontology is not expected to declare them.
        rp_domain = {p for p in rp if not p.startswith("_")}
        prop_rows.append({"label": label, "node_count": g["node_counts"].get(label),
                          **diff(f"properties:{label}", dp, rp_domain),
                          "infrastructure_properties": sorted(rp - rp_domain)})
    rel_prop_rows = []
    for rt in sorted(set(d["rel_types"]) | set(g["rel_types"])):
        dp = set(d["rel_properties"].get(rt, []))
        rp = {p for p in g["rel_properties"].get(rt, []) if not p.startswith("_")}
        rel_prop_rows.append({"rel_type": rt, "rel_count": g["rel_counts"].get(rt),
                              **diff(f"rel_properties:{rt}", dp, rp)})

    # Identity keys are what text2cypher anchors on. An unindexed one turns every anchored
    # query into a sweep, so this is a performance finding, not only a schema one.
    indexed: Set[Tuple[str, str]] = set()
    for ix in g["indexes"]:
        for lab in (ix.get("labels") or []):
            for pr in (ix.get("properties") or []):
                indexed.add((lab, pr))
    identity_rows = []
    for label, keys in sorted(d["identity_keys"].items()):
        for k in keys:
            identity_rows.append({"label": label, "identity_key": k,
                                  "exists_in_graph": k in set(g["node_properties"].get(label, [])),
                                  "indexed": (label, k) in indexed})

    endpoint_diff = diff("relationship_endpoints",
                         set(d["rel_endpoints"]), set(g["rel_endpoints"]))

    problems: List[str] = []
    for f in findings:
        if f["declared_but_absent"]:
            problems.append(f'{f["scope"]}: declared but absent {f["declared_but_absent"]}')
        if f["present_but_undeclared"]:
            problems.append(f'{f["scope"]}: present but undeclared {f["present_but_undeclared"]}')
    for row in prop_rows:
        if row["declared_but_absent"]:
            problems.append(f'{row["label"]}: declared properties absent {row["declared_but_absent"]}')
        if row["present_but_undeclared"]:
            problems.append(f'{row["label"]}: graph properties undeclared {row["present_but_undeclared"]}')
    for row in identity_rows:
        if not row["exists_in_graph"]:
            problems.append(f'{row["label"]}.{row["identity_key"]}: identity key not in graph')
        elif not row["indexed"]:
            problems.append(f'{row["label"]}.{row["identity_key"]}: identity key NOT INDEXED '
                            f'— anchored queries on it sweep')
    if endpoint_diff["declared_but_absent"]:
        problems.append(f'endpoints declared but never occurring: '
                        f'{endpoint_diff["declared_but_absent"]}')

    report = {
        "schema_version": "seocho.finbench.ontology-conformance.v1",
        "question": "does the declared ontology describe the graph that is loaded?",
        "manifest": runmeta.manifest(db_container=args.db_container),
        "config": {k: v for k, v in vars(args).items() if k != "password"},
        "policy": {
            "allowed_labels": sorted(getattr(policy, "allowed_labels", ()) or ()),
            "allowed_relationships": sorted(getattr(policy, "allowed_relationships", ()) or ()),
            "allowed_properties": sorted(getattr(policy, "allowed_properties", ()) or ()),
            "workspace_property": getattr(policy, "workspace_property", None),
            "max_graph_hops": getattr(policy, "max_graph_hops", None),
        },
        "graph": g,
        "declared": d,
        "label_and_type_diffs": findings,
        "node_property_diffs": prop_rows,
        "rel_property_diffs": rel_prop_rows,
        "identity_keys": identity_rows,
        "endpoint_diff": endpoint_diff,
        "problems": problems,
        "conformant": not problems,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")

    print(f"[ontology] {args.ontology} vs {args.database}")
    print(f"  labels          declared {len(d['labels']):>3}  in graph {len(g['labels']):>3}")
    print(f"  rel types       declared {len(d['rel_types']):>3}  in graph {len(g['rel_types']):>3}")
    print(f"  endpoints       declared {len(d['rel_endpoints']):>3}  occurring {len(g['rel_endpoints']):>3}")
    print(f"  policy props    {len(report['policy']['allowed_properties'])}")
    print(f"\n  conformant = {report['conformant']}")
    for p in problems:
        print(f"    ! {p}")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
