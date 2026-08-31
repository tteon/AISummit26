#!/usr/bin/env python3
"""Execute every request-contract gold query against a named FinBench database.

This is a database-only pre-flight: it makes no model call and records raw per-request rows so
the later paid arm cannot mistake a blind or syntactically invalid reference query for a model
failure.
"""
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


def validate(catalog_path: Path, uri: str, password: str, databases: dict[str, int],
             anchor: int, row_cap: int) -> dict[str, Any]:
    catalog = yaml.safe_load(catalog_path.read_text())
    rows: list[dict[str, Any]] = []
    with GraphDatabase.driver(uri, auth=("neo4j", password)) as driver:
        for database, sf in databases.items():
            for request in catalog["requests"]:
                params = {"workspace_id": catalog.get("workspace_id", "default"), "acct_no": anchor,
                          "limit": row_cap}
                params.update({name: spec.get("minimum", 1)
                               for name, spec in request.get("parameters", {}).items()
                               if spec.get("source") == "user"})
                # A deterministic, valid-shaped company identifier; a zero-row list is still a
                # valid query and is intentionally retained rather than excluded as "blind".
                if "company_id" in params:
                    params["company_id"] = "C0"
                started = time.monotonic()
                try:
                    with driver.session(database=database) as session:
                        if request.get("validation_seed"):
                            seed = session.run(request["validation_seed"], params).single()
                            if seed is None:
                                raise ValueError("validation_seed returned no scenario")
                            params.update(seed.data())
                        result = session.run(request["gold"], params)
                        columns = list(result.keys())
                        data = [record.data() for record in result]
                    rows.append({"database": database, "sf": sf, "request_id": request["id"],
                                 "params": params, "ok": True, "row_count": len(data),
                                 "columns": list(data[0]) if data else columns,
                                 "elapsed_ms": round((time.monotonic() - started) * 1000, 3)})
                except Exception as exc:  # preserve every sample rather than aborting a batch
                    rows.append({"database": database, "sf": sf, "request_id": request["id"],
                                 "params": params, "ok": False, "error": f"{type(exc).__name__}: {exc}",
                                 "elapsed_ms": round((time.monotonic() - started) * 1000, 3)})
    return {"schema_version": "finbench.request-contract-gold-validation.v1",
            "manifest": runmeta.manifest(validation="database-only request contract gold"),
            "catalog": str(catalog_path), "uri": uri, "anchor": anchor, "row_cap": row_cap,
            "samples": rows, "valid": all(row["ok"] for row in rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=Path("configs/agentic_request_schema_contracts_v2.yaml"))
    parser.add_argument("--uri", default="bolt://localhost:7688")
    parser.add_argument("--password", required=True)
    parser.add_argument("--databases", nargs="+", required=True, help="name:SF pairs")
    parser.add_argument("--anchor", type=int, default=108)
    parser.add_argument("--row-cap", type=int, default=50)
    parser.add_argument("--out", type=Path, default=Path("results/analysis/request_schema_contracts_v2_gold.json"))
    args = parser.parse_args()
    databases = {item.rsplit(":", 1)[0]: int(item.rsplit(":", 1)[1]) for item in args.databases}
    report = validate(args.catalog, args.uri, args.password, databases, args.anchor, args.row_cap)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"valid={report['valid']} samples={len(report['samples'])} wrote {args.out}")
    raise SystemExit(0 if report["valid"] else 1)


if __name__ == "__main__":
    main()
