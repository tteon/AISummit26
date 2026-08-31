#!/usr/bin/env python3
"""Freeze a DB-only, topology-stratified request workload before a paid agent run."""
from __future__ import annotations

import argparse
import importlib.util
import json
import time
from pathlib import Path
from typing import Any

import yaml
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("runmeta", ROOT / "scripts/analysis/runmeta.py")
runmeta = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(runmeta)


def _params(request: dict[str, Any], anchor: int, row_cap: int) -> dict[str, Any]:
    values = {"workspace_id": "default", "acct_no": anchor, "limit": row_cap}
    for name, field in request.get("parameters", {}).items():
        if field.get("source") == "user":
            values[name] = field.get("minimum", field.get("exclusive_minimum", 1))
    return values


def build(catalog_path: Path, manifests: dict[int, Path], databases: dict[int, str],
          uri: str, password: str, row_cap: int) -> dict[str, Any]:
    catalog = yaml.safe_load(catalog_path.read_text())
    samples: list[dict[str, Any]] = []
    anchors: list[dict[str, Any]] = []
    with GraphDatabase.driver(uri, auth=("neo4j", password)) as driver:
        for sf, path in manifests.items():
            snapshot = json.loads(path.read_text())
            curated = snapshot["degree_profile"]["curated_anchors"]
            for item in curated:
                band, anchor = item["band"], int(item["account_id"])
                anchors.append({"sf": sf, "database": databases[sf], **item})
                with driver.session(database=databases[sf]) as session:
                    for request in catalog["requests"]:
                        # Company-id request has a distinct topology stratum and is deliberately
                        # excluded from an account-anchor comparison.
                        if "$acct_no" not in request["gold"]:
                            continue
                        params = _params(request, anchor, row_cap)
                        started = time.monotonic()
                        try:
                            result = session.run(request["gold"], params)
                            columns = list(result.keys())
                            rows = [record.data() for record in result]
                            samples.append({"sf": sf, "database": databases[sf], "anchor_band": band,
                                            "anchor": anchor, "anchor_transfer_degree": item["degree"],
                                            "request_id": request["id"], "params": params, "ok": True,
                                            "row_count": len(rows), "columns": columns,
                                            "db_only_elapsed_ms": round((time.monotonic()-started)*1000, 3)})
                        except Exception as exc:
                            samples.append({"sf": sf, "database": databases[sf], "anchor_band": band,
                                            "anchor": anchor, "anchor_transfer_degree": item["degree"],
                                            "request_id": request["id"], "params": params, "ok": False,
                                            "error": f"{type(exc).__name__}: {exc}",
                                            "db_only_elapsed_ms": round((time.monotonic()-started)*1000, 3)})
    return {"schema_version": "finbench.topology-anchor-workload.v1",
            "manifest": runmeta.manifest(validation="DB-only topology-stratified request workload"),
            "method": {"unit": "request × SF × curated anchor band", "bands": ["median", "p99", "p99.9", "hub"],
                       "excluded": "requests without an account anchor", "note": "This freezes executable Gold cells only; no model/API call is made."},
            "catalog": str(catalog_path), "anchors": anchors, "samples": samples,
            "valid": all(row["ok"] for row in samples)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=Path("configs/agentic_request_schema_contracts_v2.yaml"))
    parser.add_argument("--snapshot", action="append", required=True, help="SF:manifest path")
    parser.add_argument("--database", action="append", required=True, help="SF:database")
    parser.add_argument("--uri", default="bolt://localhost:7688")
    parser.add_argument("--password", required=True)
    parser.add_argument("--row-cap", type=int, default=50)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    manifests = {int(x.split(":", 1)[0]): Path(x.split(":", 1)[1]) for x in args.snapshot}
    databases = {int(x.split(":", 1)[0]): x.split(":", 1)[1] for x in args.database}
    output = build(args.catalog, manifests, databases, args.uri, args.password, args.row_cap)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2) + "\n")
    print(f"valid={output['valid']} anchors={len(output['anchors'])} samples={len(output['samples'])} wrote {args.out}")
    raise SystemExit(0 if output["valid"] else 1)


if __name__ == "__main__":
    main()
