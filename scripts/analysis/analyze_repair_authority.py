#!/usr/bin/env python3
"""Pair auto-repair and advisory-repair reports without mixing scale denominators."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "runmeta_repair_authority", REPO_ROOT / "scripts" / "analysis" / "runmeta.py")
runmeta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runmeta)  # type: ignore[union-attr]


def _index(rows: Iterable[Dict[str, Any]]) -> Dict[tuple[int, str, int], Dict[str, Any]]:
    return {(int(row["sf"]), str(row["question_id"]), int(row.get("repeat", 0))): row
            for row in rows}


def _delta(auto: Dict[str, Any], advisory: Dict[str, Any]) -> Dict[str, Any]:
    ar = auto.get("repair_loop") or {}
    return {
        "sf": int(auto["sf"]), "question_id": auto["question_id"],
        "repeat": int(auto.get("repeat", 0)),
        "auto_correct": bool(auto.get("correct")),
        "advisory_correct": bool(advisory.get("correct")),
        "correct_change_auto_minus_advisory": int(bool(auto.get("correct"))) - int(bool(advisory.get("correct"))),
        "model_calls_delta": int(auto.get("model_calls", 0)) - int(advisory.get("model_calls", 0)),
        "prompt_tokens_delta": int(auto.get("prompt_tokens", 0)) - int(advisory.get("prompt_tokens", 0)),
        "completion_tokens_delta": int(auto.get("completion_tokens", 0)) - int(advisory.get("completion_tokens", 0)),
        "graph_trips_delta": int(auto.get("graph_trips", 0)) - int(advisory.get("graph_trips", 0)),
        "db_hits_delta": int(auto.get("db_hits", 0)) - int(advisory.get("db_hits", 0)),
        "db_ms_delta": round(float(auto.get("db_ms", 0) or 0) - float(advisory.get("db_ms", 0) or 0), 3),
        "wall_ms_delta": round(float(auto.get("wall_ms", 0) or 0) - float(advisory.get("wall_ms", 0) or 0), 1),
        "auto_verifier_repair_requested": bool(ar.get("verifier_requested")),
        "auto_verifier_repair_applied": bool(ar.get("repair_applied")),
        "auto_repair_model_calls": int(ar.get("repair_model_calls", 0) or 0),
        "auto_repair_api_elapsed_ms": float(ar.get("repair_api_elapsed_ms", 0) or 0),
        "same_initial_cypher": (auto.get("decisions", {}).get("initial_cypher") ==
                                  advisory.get("decisions", {}).get("initial_cypher")),
    }


def _summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    keys = ("correct_change_auto_minus_advisory", "model_calls_delta", "prompt_tokens_delta",
            "completion_tokens_delta", "graph_trips_delta", "db_hits_delta", "db_ms_delta",
            "wall_ms_delta", "auto_repair_model_calls", "auto_repair_api_elapsed_ms")
    return {"n": len(rows), **{key: round(sum(float(row[key]) for row in rows), 3)
                                for key in keys},
            "repair_applied_n": sum(bool(row["auto_verifier_repair_applied"]) for row in rows),
            "initial_cypher_equal_n": sum(bool(row["same_initial_cypher"]) for row in rows)}


def analyze(auto: Dict[str, Any], advisory: Dict[str, Any]) -> Dict[str, Any]:
    if auto.get("endpoint") != advisory.get("endpoint"):
        raise ValueError("endpoint descriptors differ; authority comparison is invalid")
    auto_index, advisory_index = _index(auto["samples"]), _index(advisory["samples"])
    cells = sorted(set(auto_index) & set(advisory_index))
    if set(auto_index) != set(advisory_index):
        raise ValueError("auto/advisory cells differ")
    pairs = [_delta(auto_index[cell], advisory_index[cell]) for cell in cells]
    by_sf = [{"sf": sf, **_summary([row for row in pairs if row["sf"] == sf])}
             for sf in sorted({row["sf"] for row in pairs})]
    return {
        "schema_version": "seocho.repair-authority-analysis.v1",
        "manifest": runmeta.manifest(analysis="paired verifier authority repair cost"),
        "endpoint": auto["endpoint"],
        "method": {
            "comparison": "auto minus advisory, paired by SF/question/repeat",
            "scope": "per-scale deltas are primary; aggregate is a balanced-cell ledger, not a scale-normalized latency claim",
            "gpu_cost": "not observed or inferred for hosted MARA",
        },
        "pairs": pairs, "overall": _summary(pairs), "by_sf": by_sf,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auto", type=Path, required=True)
    parser.add_argument("--advisory", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    output = analyze(json.loads(args.auto.read_text()), json.loads(args.advisory.read_text()))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2, default=str) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
