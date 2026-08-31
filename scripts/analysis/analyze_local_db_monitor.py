#!/usr/bin/env python3
"""Join Agentic FinBench episodes to durable local database-container samples."""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "runmeta_local_db_monitor", REPO_ROOT / "scripts" / "analysis" / "runmeta.py")
runmeta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runmeta)  # type: ignore[union-attr]

CUMULATIVE_COUNTERS = (
    "network_rx_bytes", "network_tx_bytes", "block_read_bytes", "block_write_bytes")


def _overlaps(sample: Dict[str, Any], window: Dict[str, float]) -> bool:
    sample_start = float(sample.get("sample_started_mono", sample["t_mono"]))
    sample_end = float(sample["t_mono"])
    return sample_start <= float(window["ended_mono"]) and sample_end >= float(
        window["started_mono"])


def _counter_delta(samples: List[Dict[str, Any]], key: str) -> Optional[int]:
    values = [sample["db_container"]["counters"].get(key) for sample in samples]
    usable = [int(value) for value in values if value is not None]
    if len(usable) < 2:
        return None
    # Docker's network and block-I/O fields are cumulative since container start. A reset
    # means the container restarted, so a negative value must not be reported as workload IO.
    return usable[-1] - usable[0] if usable[-1] >= usable[0] else None


def _episode_summary(episode: Dict[str, Any], samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    window = episode.get("monitor_window")
    if not window:
        raise ValueError(f"episode has no monitor_window: {episode.get('episode_id')}")
    selected = [sample for sample in samples if _overlaps(sample, window)]
    available = [sample for sample in selected
                 if sample.get("db_container", {}).get("available")]
    counters = [sample["db_container"]["counters"] for sample in available]

    def gauge(key: str) -> List[float]:
        return [float(counter[key]) for counter in counters if counter.get(key) is not None]

    cpu = gauge("cpu_percent")
    memory = gauge("memory_used_bytes")
    pids = gauge("pids")
    bracketed = bool(available) and (
        float(available[0].get("sample_started_mono", available[0]["t_mono"]))
        <= float(window["started_mono"]) and
        float(available[-1]["t_mono"]) >= float(window["ended_mono"]))
    return {
        "episode_id": episode["episode_id"],
        "arm": episode["arm"], "question_id": episode["question_id"],
        "correct": episode.get("correct"),
        "wall_ms": episode.get("wall_ms"), "db_ms": episode.get("db_ms"),
        "db_hits": episode.get("db_hits"), "graph_trips": episode.get("graph_trips"),
        "monitor_window": window,
        "overlapping_samples": len(selected),
        "available_samples": len(available),
        "window_bracketed": bracketed,
        "db_container_gauges": {
            "cpu_percent_median": round(statistics.median(cpu), 3) if cpu else None,
            "cpu_percent_max": round(max(cpu), 3) if cpu else None,
            "memory_used_bytes_max": int(max(memory)) if memory else None,
            "pids_max": int(max(pids)) if pids else None,
        },
        "db_container_cumulative_counter_delta": {
            key: _counter_delta(available, key) for key in CUMULATIVE_COUNTERS
        },
        "attribution": (
            "bracketing sample-window change; includes concurrent/background DB activity "
            "and is not an exact per-query counter"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    report = json.loads(args.report.read_text())
    receipt = report.get("system_monitor_receipt", {})
    if not receipt.get("valid"):
        raise SystemExit("source run has no valid local database monitoring receipt")
    metrics_path = args.metrics or (REPO_ROOT / receipt["path"])
    samples = [json.loads(line) for line in metrics_path.read_text().splitlines()
               if line.strip()]
    if not samples:
        raise SystemExit("system metrics JSONL is empty")
    episodes = [_episode_summary(episode, samples) for episode in report["samples"]]
    output = {
        "schema_version": "seocho.local-db-monitor-analysis.v1",
        "manifest": runmeta.manifest(analysis="MARA + local DozerDB monitoring join"),
        "source": {"report": str(args.report), "metrics": str(metrics_path)},
        "endpoint": report["endpoint"],
        "system_monitor_receipt": receipt,
        "episodes": episodes,
        "summary": {
            "episodes": len(episodes),
            "all_windows_bracketed": all(row["window_bracketed"] for row in episodes),
            "all_have_two_or_more_samples": all(
                row["available_samples"] >= 2 for row in episodes),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2, default=str) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
