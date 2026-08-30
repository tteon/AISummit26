#!/usr/bin/env python3
"""Derive paired agent-interface findings from immutable raw pilot reports."""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
from pathlib import Path
from typing import Any, Dict, Iterable, List

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "runmeta_readiness", REPO_ROOT / "scripts" / "analysis" / "runmeta.py")
runmeta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runmeta)  # type: ignore[union-attr]


def _load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text())


def _pct(candidate: float, baseline: float) -> float | None:
    return round((candidate / baseline - 1) * 100, 2) if baseline else None


def _index(rows: Iterable[Dict[str, Any]], arm_key: str = "arm") -> Dict[tuple[str, str], Dict[str, Any]]:
    return {(str(r["question_id"]), str(r[arm_key])): r for r in rows}


def analyze(topology: Dict[str, Any], framework: Dict[str, Any],
            fibo: Dict[str, Any], sources: Dict[str, str]) -> Dict[str, Any]:
    trows = topology["samples"]
    tidx = _index(trows)
    qids = sorted({r["question_id"] for r in trows})
    full_typed = []
    staged_multi = []
    typed_envelope = []
    for qid in qids:
        full = tidx[(qid, "multi_full")]
        typed = tidx[(qid, "multi_typed")]
        staged = tidx[(qid, "staged_single")]
        envelope = tidx[(qid, "multi_envelope")]
        full_typed.append({
            "question_id": qid, "full_correct": full["correct"],
            "typed_correct": typed["correct"],
            "full_prompt_tokens": full["prompt_tokens"],
            "typed_prompt_tokens": typed["prompt_tokens"],
            "full_handoff_chars": full["handoff_chars"],
            "typed_handoff_chars": typed["handoff_chars"],
            "both_executed": bool(full.get("graph_trips") and typed.get("graph_trips")),
            "full_db_hits": full.get("db_hits"), "typed_db_hits": typed.get("db_hits"),
        })
        staged_multi.append({
            "question_id": qid,
            "same_correct": staged["correct"] == full["correct"],
            "same_prompt_tokens": staged["prompt_tokens"] == full["prompt_tokens"],
            "same_handoff_chars": staged["handoff_chars"] == full["handoff_chars"],
            "same_cypher": (staged.get("decisions", {}).get("initial_cypher") ==
                            full.get("decisions", {}).get("initial_cypher")),
        })
        typed_envelope.append({
            "question_id": qid,
            "same_correct": typed["correct"] == envelope["correct"],
            "same_initial_cypher": (typed.get("decisions", {}).get("initial_cypher") ==
                                    envelope.get("decisions", {}).get("initial_cypher")),
            "typed_prompt_tokens": typed["prompt_tokens"],
            "envelope_prompt_tokens": envelope["prompt_tokens"],
        })

    both_db = [p for p in full_typed if p["both_executed"]]
    verifier_rows = [r for r in trows if r.get("verifier_pass") is not None]
    topology_analysis = {
        "arms": topology["summary"],
        "full_to_typed": {
            "paired": full_typed,
            "prompt_token_change_pct": _pct(
                sum(p["typed_prompt_tokens"] for p in full_typed),
                sum(p["full_prompt_tokens"] for p in full_typed)),
            "handoff_char_change_pct": _pct(
                sum(p["typed_handoff_chars"] for p in full_typed),
                sum(p["full_handoff_chars"] for p in full_typed)),
            "correct_change": (sum(p["typed_correct"] for p in full_typed) -
                               sum(p["full_correct"] for p in full_typed)),
            "db_hits_both_executed_n": len(both_db),
            "db_hits_paired_median_change_pct": (statistics.median(
                ((p["typed_db_hits"] / p["full_db_hits"] - 1) * 100)
                for p in both_db if p["full_db_hits"])
                if both_db else None),
        },
        "staged_single_vs_multi_full_manipulation_check": {
            "paired": staged_multi,
            "all_visible_outputs_equal": all(
                all(p[k] for k in ("same_correct", "same_prompt_tokens",
                                    "same_handoff_chars", "same_cypher"))
                for p in staged_multi),
        },
        "typed_vs_result_envelope": {
            "paired": typed_envelope,
            "same_correct_all": all(p["same_correct"] for p in typed_envelope),
            "same_initial_cypher_all": all(p["same_initial_cypher"] for p in typed_envelope),
        },
        "verifier": {
            "evaluated": len(verifier_rows),
            "false_rejects_of_correct_results": sum(
                bool(r["correct"] and r.get("verifier_pass") is False) for r in verifier_rows),
            "false_accepts_of_incorrect_results": sum(
                bool(not r["correct"] and r.get("verifier_pass") is True) for r in verifier_rows),
            "authority": "advisory",
        },
    }

    frows = framework["samples"]
    fidx = {(r["scheduler"], r["context_policy"]): r for r in frows}
    scheduler_checks = []
    for policy in sorted({r["context_policy"] for r in frows}):
        procedural = fidx[("procedural", policy)]
        langgraph = fidx[("langgraph", policy)]
        scheduler_checks.append({
            "context_policy": policy,
            "same_correct": procedural["correct"] == langgraph["correct"],
            "same_cypher": procedural["cypher"] == langgraph["cypher"],
            "same_prompt_tokens": procedural["prompt_tokens"] == langgraph["prompt_tokens"],
            "same_completion_tokens": (procedural["completion_tokens"] ==
                                       langgraph["completion_tokens"]),
        })
    typed = fidx[("procedural", "typed_isolated")]
    full = fidx[("procedural", "full_transcript")]
    framework_analysis = {
        "scheduler_manipulation_check": scheduler_checks,
        "scheduler_outputs_equal": all(all(p[k] for k in p if k != "context_policy")
                                       for p in scheduler_checks),
        "typed_vs_full_single_question": {
            "full_correct": full["correct"], "typed_correct": typed["correct"],
            "prompt_token_change_pct": _pct(typed["prompt_tokens"], full["prompt_tokens"]),
            "handoff_char_change_pct": _pct(typed["handoff_chars"], full["handoff_chars"]),
        },
    }

    brows = fibo["samples"]
    bidx = _index(brows)
    bqids = sorted({r["question_id"] for r in brows})
    paired_fibo = []
    for qid in bqids:
        physical = bidx[(qid, "physical_only")]
        compiled = bidx[(qid, "compiled_fibo")]
        retrieved = bidx[(qid, "retrieved_fibo")]
        paired_fibo.append({
            "question_id": qid, "physical_correct": physical["correct"],
            "compiled_correct": compiled["correct"],
            "retrieved_correct": retrieved["correct"],
            "compiled_gain": bool(compiled["correct"] and not physical["correct"]),
            "compiled_loss": bool(physical["correct"] and not compiled["correct"]),
            "retrieved_gain": bool(retrieved["correct"] and not physical["correct"]),
            "retrieved_loss": bool(physical["correct"] and not retrieved["correct"]),
            "physical_db_hits": physical.get("db_hits", 0),
            "compiled_db_hits": compiled.get("db_hits", 0),
            "retrieved_db_hits": retrieved.get("db_hits", 0),
        })
    bs = fibo["summary"]
    fibo_analysis = {
        "arms": bs, "paired": paired_fibo,
        "compiled_gains": sum(p["compiled_gain"] for p in paired_fibo),
        "compiled_losses": sum(p["compiled_loss"] for p in paired_fibo),
        "retrieved_gains": sum(p["retrieved_gain"] for p in paired_fibo),
        "retrieved_losses": sum(p["retrieved_loss"] for p in paired_fibo),
        "compiled_prompt_change_vs_physical_pct": _pct(
            bs["compiled_fibo"]["prompt_tokens"], bs["physical_only"]["prompt_tokens"]),
        "retrieved_prompt_change_vs_compiled_pct": _pct(
            bs["retrieved_fibo"]["prompt_tokens"], bs["compiled_fibo"]["prompt_tokens"]),
        "retrieved_context_change_vs_compiled_pct": _pct(
            bs["retrieved_fibo"]["semantic_context_chars"],
            bs["compiled_fibo"]["semantic_context_chars"]),
    }

    trace_receipts = {
        name: {"trace_ids": len(report["trace_receipt"]["trace_ids"]),
               "local_complete": report["trace_receipt"]["local_complete"],
               "tempo_complete": report["trace_receipt"]["tempo_complete"]}
        for name, report in (("topology", topology), ("framework", framework), ("fibo", fibo))
    }
    return {
        "schema_version": "seocho.agent-interface-readiness-analysis.v1",
        "manifest": runmeta.manifest(analysis="paired agent-interface pilots"),
        "sources": sources, "trace_receipts": trace_receipts,
        "topology": topology_analysis, "framework": framework_analysis,
        "fibo": fibo_analysis,
        "validity": {
            "status": "exploratory_pilot",
            "notes": [
                "One repeat at SF1; accuracy intervals are intentionally not claimed.",
                "Hosted MARA is a distinct arm from self-hosted vLLM and is not merged with it.",
                "DB-hit paired comparisons include only questions executed in both arms.",
                "No private chain-of-thought was requested or retained.",
            ],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology", type=Path, required=True)
    parser.add_argument("--framework", type=Path, required=True)
    parser.add_argument("--fibo", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    sources = {"topology": str(args.topology), "framework": str(args.framework),
               "fibo": str(args.fibo)}
    report = analyze(_load(args.topology), _load(args.framework), _load(args.fibo), sources)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
