#!/usr/bin/env python3
"""Discover MARA models and run one controlled Agentic FinBench protocol on each.

Each model is a separate endpoint arm with its own topology report, raw samples, manifest,
conversations and trace receipt.  The matrix runner never combines measurements across
models; it only records which child artifacts belong to the same pre-registered protocol.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
TOPOLOGY_RUNNER = REPO_ROOT / "scripts" / "benchmarks" / "bench_agent_topology.py"

_spec = importlib.util.spec_from_file_location(
    "runmeta_model_matrix", REPO_ROOT / "scripts" / "analysis" / "runmeta.py")
runmeta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runmeta)  # type: ignore[union-attr]


def _discover(base_url: str, api_key: str, timeout_s: float) -> List[Dict[str, Any]]:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        payload = json.load(response)
    models = payload.get("data")
    if not isinstance(models, list) or not models:
        raise RuntimeError("MARA /models returned no accessible models")
    normalized = []
    for item in models:
        if isinstance(item, dict) and item.get("id"):
            normalized.append({"id": str(item["id"]),
                               "owned_by": item.get("owned_by")})
    if not normalized:
        raise RuntimeError("MARA /models returned no model IDs")
    return sorted(normalized, key=lambda item: item["id"].lower())


def _slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def _run_child(command: List[str], log_path: Path) -> int:
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, cwd=REPO_ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
            log.flush()
            os.fsync(log.fileno())
            print(line, end="", flush=True)
        return process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv(
        "MARA_BASE_URL", "https://api.cloud.mara.com/v1"))
    parser.add_argument("--models", nargs="+", default=None,
                        help="explicit model IDs; default discovers every ID from /v1/models")
    parser.add_argument("--uri", default="bolt://localhost:7688")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", required=True)
    parser.add_argument("--database", default="finbenchl1:1")
    parser.add_argument("--ontology", default="ontology/finbench.ontology.yaml")
    parser.add_argument("--questions", nargs="+", default=[
        "ext_med_2", "ext_hard_2", "int_med_1", "int_hard_2"])
    parser.add_argument("--arms", nargs="+", default=[
        "staged_single", "multi_full", "multi_typed"])
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--row-cap", type=int, default=50)
    parser.add_argument("--decision-tokens", type=int, default=500)
    parser.add_argument("--executor-tokens", type=int, default=1000)
    parser.add_argument("--request-timeout-s", type=float, default=180.0)
    parser.add_argument("--seed", type=int, default=31)
    parser.add_argument("--db-container", default="aisummit-simtest")
    parser.add_argument("--otlp-endpoint", default="http://127.0.0.1:4317")
    parser.add_argument("--tempo-url", default="http://127.0.0.1:3200")
    parser.add_argument("--system-metrics-interval-s", type=float, default=5.0)
    parser.add_argument("--episode-delay-s", type=float, default=5.0)
    parser.add_argument("--no-system-metrics", action="store_true")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output-dir", default="results/episodes/agent_model_matrix")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.run_id is None:
        args.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    api_key = os.getenv("MARA_API_KEY", "")
    if not api_key:
        raise SystemExit("MARA_API_KEY is required")

    out_dir = Path(args.output_dir) / args.run_id
    if out_dir.exists() and not args.resume:
        raise SystemExit(f"output exists; choose new --run-id or --resume: {out_dir}")
    # Capture source state before creating any result path.
    source_manifest = runmeta.manifest(experiment="mara-agent-model-matrix")
    discovered = _discover(args.base_url, api_key, args.request_timeout_s)
    if args.models:
        accessible = {item["id"]: item for item in discovered}
        missing = sorted(set(args.models) - set(accessible))
        if missing:
            raise SystemExit(f"requested models not returned by MARA /models: {missing}")
        models = [accessible[name] for name in args.models]
    else:
        models = discovered

    out_dir.mkdir(parents=True, exist_ok=True)
    models_root = out_dir / "models"
    models_root.mkdir(exist_ok=True)
    protocol = {
        "provider": "mara", "base_url": args.base_url,
        "models": models, "questions": args.questions, "arms": args.arms,
        "repeats": args.repeats, "database": args.database,
        "row_cap": args.row_cap, "decision_tokens": args.decision_tokens,
        "executor_tokens": args.executor_tokens, "request_timeout_s": args.request_timeout_s,
        "temperature": 0.0, "seed": args.seed, "verifier_mode": "advisory",
        "telemetry": {
            "experiment_boundary": "hosted_mara_model_plus_local_graph_database",
            "profile_operator_tree": True,
            "local_system_metrics": not args.no_system_metrics,
            "local_database_monitoring_required": not args.no_system_metrics,
            "system_metrics_interval_s": args.system_metrics_interval_s,
            "episode_delay_s": args.episode_delay_s,
            "hosted_model_server": "unavailable_by_endpoint",
        },
    }
    header = {
        "schema_version": "seocho.finbench.agent-model-matrix.v1",
        "run_id": args.run_id, "manifest": source_manifest,
        "discovery": {"endpoint": args.base_url.rstrip("/") + "/models",
                      "accessible_count": len(discovered), "models": discovered},
        "protocol": protocol,
        "config": {**{k: v for k, v in vars(args).items() if k != "password"},
                   "password": "<redacted>", "api_key_present": True},
    }
    (out_dir / "manifest.json").write_text(json.dumps(header, indent=2) + "\n")

    statuses: List[Dict[str, Any]] = []
    for model in models:
        model_id = model["id"]
        child_id = _slug(model_id)
        child_dir = models_root / child_id
        report_path = child_dir / "report.json"
        command = [
            sys.executable, str(TOPOLOGY_RUNNER), "--provider", "mara",
            "--model", model_id, "--base-url", args.base_url,
            "--request-timeout-s", str(args.request_timeout_s),
            "--uri", args.uri, "--user", args.user, "--password", args.password,
            "--databases", args.database, "--ontology", args.ontology,
            "--arms", *args.arms, "--only", *args.questions,
            "--repeats", str(args.repeats), "--row-cap", str(args.row_cap),
            "--decision-tokens", str(args.decision_tokens),
            "--executor-tokens", str(args.executor_tokens),
            "--verifier-mode", "advisory", "--seed", str(args.seed),
            "--db-container", args.db_container, "--otlp-endpoint", args.otlp_endpoint,
            "--tempo-url", args.tempo_url, "--run-id", child_id,
            "--output-dir", str(models_root),
            "--system-metrics-interval-s", str(args.system_metrics_interval_s),
            "--episode-delay-s", str(args.episode_delay_s),
        ]
        if args.no_system_metrics:
            command.append("--no-system-metrics")
        if args.resume:
            command.append("--resume")
        print(f"\n=== model {model_id} ({child_id}) ===", flush=True)
        if args.dry_run:
            statuses.append({"model": model_id, "child_run_id": child_id,
                             "status": "dry_run", "report": str(report_path)})
            continue
        returncode = _run_child(command, out_dir / f"{child_id}.log")
        status: Dict[str, Any] = {
            "model": model_id, "owned_by": model.get("owned_by"),
            "child_run_id": child_id, "returncode": returncode,
            "status": "completed" if returncode == 0 and report_path.exists() else "failed",
            "report": str(report_path),
        }
        if report_path.exists():
            child = json.loads(report_path.read_text())
            status["endpoint"] = child.get("endpoint")
            status["summary"] = child.get("summary")
            status["trace_receipt"] = child.get("trace_receipt")
        statuses.append(status)
        (out_dir / "progress.json").write_text(json.dumps(statuses, indent=2) + "\n")

    report = {**header, "models": statuses,
              "completed_models": sum(s["status"] == "completed" for s in statuses),
              "failed_models": sum(s["status"] == "failed" for s in statuses)}
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"wrote {out_dir / 'report.json'}", flush=True)
    if not args.dry_run and report["failed_models"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
