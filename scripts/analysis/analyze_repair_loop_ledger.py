#!/usr/bin/env python3
"""Derive per-user-request repair-loop cost from a traced topology report.

This deliberately reports observable hosted-API and local-graph work.  It does not infer GPU
seconds or dollars from a third-party endpoint.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "runmeta_repair_loop", REPO_ROOT / "scripts" / "analysis" / "runmeta.py")
runmeta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runmeta)  # type: ignore[union-attr]


def _median(rows: Iterable[float]) -> float | None:
    values = list(rows)
    return round(float(statistics.median(values)), 3) if values else None


def _episode(row: Dict[str, Any]) -> Dict[str, Any]:
    repair = dict(row.get("repair_loop") or {})
    stages = list(row.get("stages") or [])
    request = dict(row.get("request") or {})
    return {
        "episode_id": row.get("episode_id"), "question_id": row.get("question_id"),
        "repeat": row.get("repeat"), "sf": row.get("sf"), "arm": row.get("arm"),
        "correct": bool(row.get("correct")), "error": row.get("error"),
        "request_type": request.get("request_type", row.get("audience")),
        "real_world_case": request.get("real_world_case"),
        "schema_facets": request.get("schema_facets") or [],
        "repair_risks": request.get("repair_risks") or [],
        "parameter_contract": request.get("parameter_contract") or [],
        "request_wall_ms": row.get("wall_ms"),
        "request_api_elapsed_ms": round(sum(float(s.get("elapsed_ms", 0) or 0)
                                            for s in stages), 1),
        "request_model_calls": len(stages),
        "request_prompt_tokens": int(row.get("prompt_tokens", 0) or 0),
        "request_completion_tokens": int(row.get("completion_tokens", 0) or 0),
        "request_graph_trips": int(row.get("graph_trips", 0) or 0),
        "request_db_hits": int(row.get("db_hits", 0) or 0),
        "request_db_ms": float(row.get("db_ms", 0) or 0),
        "repair": repair,
    }


def _aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    repaired = [row for row in rows if row["repair"].get("repair_model_calls", 0)]
    verifier_repairs = [row for row in rows if row["repair"].get("repair_applied")]
    return {
        "n": len(rows), "correct": sum(row["correct"] for row in rows),
        "errors": sum(bool(row["error"]) for row in rows),
        "repair_model_work_n": len(repaired), "verifier_repair_n": len(verifier_repairs),
        "converged_to_correct_n": sum(bool(row["repair"].get("converged_to_correct"))
                                      for row in rows),
        "regressed_from_correct_n": sum(bool(row["repair"].get("regressed_from_correct"))
                                       for row in rows),
        "request_wall_ms_median": _median(float(row["request_wall_ms"])
                                           for row in rows if row["request_wall_ms"] is not None),
        "request_api_elapsed_ms_median": _median(float(row["request_api_elapsed_ms"])
                                                  for row in rows),
        "request_prompt_tokens_total": sum(row["request_prompt_tokens"] for row in rows),
        "request_completion_tokens_total": sum(row["request_completion_tokens"] for row in rows),
        "request_graph_trips_total": sum(row["request_graph_trips"] for row in rows),
        "request_db_hits_total": sum(row["request_db_hits"] for row in rows),
        "repair_api_elapsed_ms_total": round(sum(float(row["repair"].get(
            "repair_api_elapsed_ms", 0) or 0) for row in rows), 1),
        "repair_loop_wall_ms_total": round(sum(float(row["repair"].get(
            "repair_loop_wall_ms", 0) or 0) for row in rows), 1),
        "repair_prompt_tokens_total": sum(int(row["repair"].get(
            "repair_prompt_tokens", 0) or 0) for row in rows),
        "repair_completion_tokens_total": sum(int(row["repair"].get(
            "repair_completion_tokens", 0) or 0) for row in rows),
        "repair_graph_trips_total": sum(int(row["repair"].get(
            "repair_graph_trips", 0) or 0) for row in rows),
        "repair_db_hits_total": sum(int(row["repair"].get(
            "repair_db_hits", 0) or 0) for row in rows),
        "repair_db_ms_total": round(sum(float(row["repair"].get(
            "repair_db_ms", 0) or 0) for row in rows), 3),
    }


def analyze(report: Dict[str, Any]) -> Dict[str, Any]:
    rows = [_episode(row) for row in report.get("samples") or []]
    groups: Dict[tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    facets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["arm"]), str(row["request_type"]))].append(row)
        for facet in row["schema_facets"]:
            facets[str(facet)].append(row)
    return {
        "schema_version": "seocho.repair-loop-analysis.v1",
        "manifest": runmeta.manifest(analysis="per-request hosted API repair-loop ledger"),
        "source": report.get("run_id"), "endpoint": report.get("endpoint"),
        "method": {
            "request_cost": "observed API elapsed/tokens plus local PROFILE/Bolt work",
            "repair_cost": "validator retries beyond first executor generation plus all verifier-directed repair work",
            "gpu_cost": "not observed or inferred for hosted MARA",
        },
        "episodes": rows,
        "overall": _aggregate(rows),
        "by_arm_and_request_type": [
            {"arm": arm, "request_type": request_type, **_aggregate(group)}
            for (arm, request_type), group in sorted(groups.items())
        ],
        "by_schema_facet": [
            {"schema_facet": facet, **_aggregate(group)}
            for facet, group in sorted(facets.items())
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    output = analyze(report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2, default=str) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
