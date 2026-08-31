#!/usr/bin/env python3
"""Build a model/context + local graph retrieval ledger from PROFILE-bearing episodes."""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "runmeta_retrieval_ledger", REPO_ROOT / "scripts" / "analysis" / "runmeta.py")
runmeta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runmeta)  # type: ignore[union-attr]


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _operator_name(node: Dict[str, Any]) -> str:
    return str(node.get("operator_type") or node.get("operatorType") or "").split("@")[0]


def _arguments(node: Dict[str, Any]) -> Dict[str, Any]:
    return node.get("arguments") or node.get("args") or {}


def _walk(node: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not node:
        return []
    rows = [node]
    for child in node.get("children") or []:
        rows.extend(_walk(child))
    return rows


def _plan_metrics(tree: Optional[Dict[str, Any]], sweep_rows: float) -> Dict[str, Any]:
    operators = _walk(tree)
    if not operators:
        return {"available": False}
    table = []
    expansion_ratios = []
    filter_discard_ratios = []
    for node in operators:
        args = _arguments(node)
        name = _operator_name(node)
        actual_rows = _number(args.get("Rows"))
        children = node.get("children") or []
        child_rows = sum(_number(_arguments(child).get("Rows")) for child in children)
        if children and child_rows > 0 and ("Expand" in name or "Traversal" in name):
            expansion_ratios.append(actual_rows / child_rows)
        if children and child_rows > 0 and name.startswith("Filter"):
            filter_discard_ratios.append(max(0.0, 1.0 - actual_rows / child_rows))
        table.append({
            "operator": name,
            "id": args.get("Id"),
            "details": args.get("Details"),
            "estimated_rows": _number(args.get("EstimatedRows")),
            "rows": actual_rows,
            "db_hits": int(_number(args.get("DbHits"))),
            "page_cache_hits": int(_number(args.get("PageCacheHits"))),
            "page_cache_misses": int(_number(args.get("PageCacheMisses"))),
            "memory_bytes": int(_number(args.get("Memory"))),
        })
    access = [row for row in table
              if "Seek" in row["operator"] or "Scan" in row["operator"]]
    if not access:
        leaf_ids = {id(node) for node in operators if not (node.get("children") or [])}
        access = [row for node, row in zip(operators, table) if id(node) in leaf_ids]
    actual_max = max((row["rows"] for row in access), default=0.0)
    estimated_max = max((row["estimated_rows"] for row in access), default=0.0)
    classification_rows = actual_max if actual_max else estimated_max
    access_class = ("sweep" if classification_rows >= sweep_rows
                    else "range" if classification_rows > 10
                    else "point")
    scan_name_present = any("Scan" in row["operator"] for row in access)
    seek_name_present = any("Seek" in row["operator"] for row in access)
    return {
        "available": True,
        "runtime": _arguments(tree).get("runtime"),
        "planner": _arguments(tree).get("planner"),
        "operator_count": len(table),
        "operators": table,
        "access_operators": access,
        "access_actual_rows_max": actual_max,
        "access_estimated_rows_max": estimated_max,
        "sweep_rows_threshold": sweep_rows,
        "access_class": access_class,
        "scan_operator_name_present": scan_name_present,
        "seek_operator_name_present": seek_name_present,
        "name_cost_disagreement": bool(seek_name_present and access_class == "sweep"),
        "operator_rows_total": int(sum(row["rows"] for row in table)),
        "max_intermediate_rows": int(max((row["rows"] for row in table), default=0)),
        "db_hits_total": sum(row["db_hits"] for row in table),
        "page_cache_hits_total": sum(row["page_cache_hits"] for row in table),
        "page_cache_misses_total": sum(row["page_cache_misses"] for row in table),
        "max_expansion_ratio": (round(max(expansion_ratios), 4)
                                if expansion_ratios else None),
        "max_filter_discard_ratio": (round(max(filter_discard_ratios), 4)
                                     if filter_discard_ratios else None),
    }


def _ratio(value: float, baseline: float) -> Optional[float]:
    return round(value / baseline, 4) if baseline else None


def _episode(row: Dict[str, Any], sweep_rows: float) -> Dict[str, Any]:
    executions = []
    for execution in row.get("executions") or []:
        plan = _plan_metrics(execution.get("profile_tree"), sweep_rows)
        returned = int(execution.get("rows", 0) or 0)
        executions.append({
            "query_fingerprint": execution.get("query_fingerprint"),
            "cypher": execution.get("cypher"),
            "params": execution.get("params"),
            "returned_rows": returned,
            "db_hits": int(execution.get("db_hits", 0) or 0),
            "db_hits_per_returned_row": _ratio(
                int(execution.get("db_hits", 0) or 0), returned),
            "result_bytes": int(execution.get("result_bytes", 0) or 0),
            "client_total_ms": execution.get("elapsed_ms"),
            "client_submit_ms": execution.get("submit_ms"),
            "client_hydrate_ms": execution.get("hydrate_ms"),
            "server_available_ms": execution.get("server_available_ms"),
            "server_consumed_ms": execution.get("server_consumed_ms"),
            "plan": plan,
        })
    access_classes = [item["plan"].get("access_class") for item in executions
                      if item["plan"].get("available")]
    return {
        "episode_id": row["episode_id"], "sf": row["sf"], "database": row["database"],
        "arm": row["arm"], "question_id": row["question_id"], "repeat": row["repeat"],
        "correct": row.get("correct"), "error": row.get("error"),
        "model_calls": row.get("model_calls"), "prompt_tokens": row.get("prompt_tokens"),
        "completion_tokens": row.get("completion_tokens"),
        "handoff_chars": row.get("handoff_chars"), "wall_ms": row.get("wall_ms"),
        "graph_trips": row.get("graph_trips"),
        "unique_graph_queries": row.get("unique_graph_queries"),
        "redundant_graph_queries": row.get("redundant_graph_queries"),
        "db_hits": int(row.get("db_hits", 0) or 0),
        "db_hits_per_graph_trip": _ratio(
            int(row.get("db_hits", 0) or 0), int(row.get("graph_trips", 0) or 0)),
        "db_ms": row.get("db_ms"), "rows_into_context": row.get("rows_into_context"),
        "result_bytes": row.get("result_bytes"),
        "access_classes": access_classes,
        "executions": executions,
    }


def _median(values: Iterable[float]) -> Optional[float]:
    usable = list(values)
    return round(statistics.median(usable), 4) if usable else None


def _aggregate(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    keys = sorted({(row["sf"], row["arm"]) for row in rows})
    output = []
    for sf, arm in keys:
        group = [row for row in rows if row["sf"] == sf and row["arm"] == arm]
        executed = [row for row in group if row["graph_trips"]]
        plans = [execution["plan"] for row in executed for execution in row["executions"]
                 if execution["plan"].get("available")]
        output.append({
            "sf": sf, "arm": arm, "n": len(group),
            "errors": sum(bool(row["error"]) for row in group),
            "correct": sum(bool(row["correct"]) for row in group),
            "executed": len(executed),
            "prompt_tokens": sum(int(row["prompt_tokens"] or 0) for row in group),
            "graph_trips": sum(int(row["graph_trips"] or 0) for row in group),
            "db_hits": sum(row["db_hits"] for row in group),
            "db_hits_median": _median(row["db_hits"] for row in executed),
            "db_ms_median": _median(float(row["db_ms"]) for row in executed),
            "sweep_plans": sum(plan["access_class"] == "sweep" for plan in plans),
            "point_plans": sum(plan["access_class"] == "point" for plan in plans),
            "range_plans": sum(plan["access_class"] == "range" for plan in plans),
            "name_cost_disagreements": sum(plan["name_cost_disagreement"] for plan in plans),
        })
    return output


def _paired(rows: List[Dict[str, Any]], left_arm: str, right_arm: str) -> List[Dict[str, Any]]:
    index = {(row["sf"], row["question_id"], row["repeat"], row["arm"]): row for row in rows}
    cells = sorted({(row["sf"], row["question_id"], row["repeat"]) for row in rows})
    output = []
    for sf, question, repeat in cells:
        left = index.get((sf, question, repeat, left_arm))
        right = index.get((sf, question, repeat, right_arm))
        if left is None or right is None:
            continue
        both = bool(left["graph_trips"] and right["graph_trips"])
        output.append({
            "sf": sf, "question_id": question, "repeat": repeat,
            "left_arm": left_arm, "right_arm": right_arm,
            "correct_change": int(bool(right["correct"])) - int(bool(left["correct"])),
            "prompt_token_ratio": _ratio(right["prompt_tokens"], left["prompt_tokens"]),
            "both_executed": both,
            "db_hits_ratio": _ratio(right["db_hits"], left["db_hits"]) if both else None,
            "db_ms_ratio": _ratio(float(right["db_ms"]), float(left["db_ms"])) if both else None,
            "same_initial_fingerprint": (
                bool(left["executions"] and right["executions"]) and
                left["executions"][0]["query_fingerprint"] ==
                right["executions"][0]["query_fingerprint"]),
            "left_access_classes": left["access_classes"],
            "right_access_classes": right["access_classes"],
        })
    return output


def _scale_pairs(rows: List[Dict[str, Any]], low_sf: int, high_sf: int) -> List[Dict[str, Any]]:
    index = {(row["sf"], row["question_id"], row["repeat"], row["arm"]): row for row in rows}
    cells = sorted({(row["question_id"], row["repeat"], row["arm"]) for row in rows})
    output = []
    for question, repeat, arm in cells:
        low = index.get((low_sf, question, repeat, arm))
        high = index.get((high_sf, question, repeat, arm))
        if low is None or high is None:
            continue
        both = bool(low["graph_trips"] and high["graph_trips"])
        output.append({
            "question_id": question, "repeat": repeat, "arm": arm,
            "low_sf": low_sf, "high_sf": high_sf, "both_executed": both,
            "db_hits_ratio": _ratio(high["db_hits"], low["db_hits"]) if both else None,
            "db_ms_ratio": _ratio(float(high["db_ms"]), float(low["db_ms"])) if both else None,
            "result_bytes_ratio": _ratio(high["result_bytes"], low["result_bytes"])
                if both else None,
            "low_access_classes": low["access_classes"],
            "high_access_classes": high["access_classes"],
            "same_initial_fingerprint": (
                bool(low["executions"] and high["executions"]) and
                low["executions"][0]["query_fingerprint"] ==
                high["executions"][0]["query_fingerprint"]),
        })
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sweep-rows", type=float, default=1000.0)
    parser.add_argument("--context-left", default="multi_full")
    parser.add_argument("--context-right", default="multi_typed")
    parser.add_argument("--low-sf", type=int, default=1)
    parser.add_argument("--high-sf", type=int, default=100)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    rows = [_episode(row, args.sweep_rows) for row in report["samples"]]
    if any(row["graph_trips"] and not row["executions"][0]["plan"].get("available")
           for row in rows):
        raise SystemExit("an executed episode lacks the required full PROFILE tree")
    context_pairs = _paired(rows, args.context_left, args.context_right)
    scale_pairs = _scale_pairs(rows, args.low_sf, args.high_sf)
    output = {
        "schema_version": "seocho.retrieval-ledger-analysis.v1",
        "manifest": runmeta.manifest(analysis="MARA context + local graph retrieval ledger"),
        "source": str(args.report), "endpoint": report["endpoint"],
        "method": {
            "access_class": (
                "maximum actual rows among Seek/Scan access operators; estimated rows are "
                "used only when PROFILE reports zero actual rows"
            ),
            "sweep_rows_threshold": args.sweep_rows,
            "operator_rows_total": "sum across pipeline operators; diagnostic, not unique rows",
            "container_metrics": "secondary; analyzed separately by monitoring-window join",
        },
        "episodes": rows,
        "aggregate": _aggregate(rows),
        "context_pairs": context_pairs,
        "scale_pairs": scale_pairs,
        "summary": {
            "episodes": len(rows),
            "executed_episodes": sum(bool(row["graph_trips"]) for row in rows),
            "profiled_queries": sum(len(row["executions"]) for row in rows),
            "context_pairs": len(context_pairs), "scale_pairs": len(scale_pairs),
            "name_cost_disagreements": sum(
                execution["plan"].get("name_cost_disagreement", False)
                for row in rows for execution in row["executions"]),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2, default=str) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
