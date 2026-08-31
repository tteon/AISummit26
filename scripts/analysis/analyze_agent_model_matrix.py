#!/usr/bin/env python3
"""Compute within-model paired context/topology effects from a MARA model matrix."""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "runmeta_model_matrix_analysis", REPO_ROOT / "scripts" / "analysis" / "runmeta.py")
runmeta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runmeta)  # type: ignore[union-attr]


def _pct(value: float, baseline: float) -> float | None:
    return round((value / baseline - 1) * 100, 2) if baseline else None


def _paired(rows: List[Dict[str, Any]], left: str, right: str) -> List[Dict[str, Any]]:
    index = {(str(r["question_id"]), int(r.get("repeat", 0)), str(r["arm"])): r
             for r in rows}
    cells = sorted({(str(r["question_id"]), int(r.get("repeat", 0))) for r in rows})
    pairs = []
    for question_id, repeat in cells:
        a = index[(question_id, repeat, left)]
        b = index[(question_id, repeat, right)]
        pairs.append({
            "question_id": question_id, "repeat": repeat,
            "left_correct": bool(a.get("correct")), "right_correct": bool(b.get("correct")),
            "same_correct": bool(a.get("correct")) == bool(b.get("correct")),
            "same_initial_cypher": (a.get("decisions", {}).get("initial_cypher") ==
                                    b.get("decisions", {}).get("initial_cypher")),
            "left_prompt_tokens": int(a.get("prompt_tokens", 0)),
            "right_prompt_tokens": int(b.get("prompt_tokens", 0)),
            "left_handoff_chars": int(a.get("handoff_chars", 0)),
            "right_handoff_chars": int(b.get("handoff_chars", 0)),
            "left_db_hits": int(a.get("db_hits", 0)),
            "right_db_hits": int(b.get("db_hits", 0)),
            "both_executed": bool(a.get("graph_trips") and b.get("graph_trips")),
            "left_error": a.get("error"), "right_error": b.get("error"),
        })
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    matrix = json.loads(args.matrix.read_text())
    per_model: Dict[str, Any] = {}
    for status in matrix["models"]:
        model = status["model"]
        if status["status"] != "completed":
            per_model[model] = {"status": status["status"],
                                "returncode": status.get("returncode")}
            continue
        report_path = Path(status["report"])
        if not report_path.is_absolute():
            report_path = REPO_ROOT / report_path
        report = json.loads(report_path.read_text())
        rows = report["samples"]
        topology = _paired(rows, "staged_single", "multi_full")
        context = _paired(rows, "multi_full", "multi_typed")
        executed = [p for p in context if p["both_executed"] and p["left_db_hits"]]
        per_model[model] = {
            "status": "completed", "endpoint": report["endpoint"],
            "trace_receipt": {k: report["trace_receipt"][k]
                              for k in ("local_complete", "tempo_complete")},
            "arms": report["summary"],
            "staged_single_vs_multi_full": {
                "n": len(topology),
                "correctness_agreement_rate": round(
                    sum(p["same_correct"] for p in topology) / len(topology), 4),
                "initial_cypher_agreement_rate": round(
                    sum(p["same_initial_cypher"] for p in topology) / len(topology), 4),
                "prompt_tokens_equal_rate": round(sum(
                    p["left_prompt_tokens"] == p["right_prompt_tokens"] for p in topology)
                    / len(topology), 4),
                "paired": topology,
            },
            "multi_full_vs_typed": {
                "n": len(context),
                "prompt_token_change_pct": _pct(
                    sum(p["right_prompt_tokens"] for p in context),
                    sum(p["left_prompt_tokens"] for p in context)),
                "handoff_char_change_pct": _pct(
                    sum(p["right_handoff_chars"] for p in context),
                    sum(p["left_handoff_chars"] for p in context)),
                "correct_change": (sum(p["right_correct"] for p in context) -
                                   sum(p["left_correct"] for p in context)),
                "db_hits_both_executed_n": len(executed),
                "db_hits_paired_median_change_pct": (round(statistics.median(
                    (p["right_db_hits"] / p["left_db_hits"] - 1) * 100 for p in executed), 2)
                    if executed else None),
                "paired": context,
            },
        }

    completed = [value for value in per_model.values() if value["status"] == "completed"]
    output = {
        "schema_version": "seocho.agent-model-matrix-analysis.v1",
        "manifest": runmeta.manifest(analysis="MARA cross-model agent-interface reproducibility"),
        "source": str(args.matrix), "protocol": matrix["protocol"],
        "per_model": per_model,
        "cross_model": {
            "accessible_models": len(matrix["discovery"]["models"]),
            "completed_models": len(completed),
            "all_trace_complete": all(
                value["trace_receipt"]["local_complete"] and
                value["trace_receipt"]["tempo_complete"] for value in completed),
            "models_with_typed_prompt_reduction": sum(
                value["multi_full_vs_typed"]["prompt_token_change_pct"] < 0
                for value in completed),
            "models_with_no_typed_correctness_loss": sum(
                value["multi_full_vs_typed"]["correct_change"] >= 0
                for value in completed),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2, default=str) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
