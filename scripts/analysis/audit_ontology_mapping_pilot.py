#!/usr/bin/env python3
"""Audit immutable pilot receipts and replay only previously admitted queries; no model calls."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.benchmarks import run_ontology_mapping_pilot as pilot


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seocho-src", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise ValueError("Audit output already exists; choose a new directory")
    folder = args.run_dir
    report = json.loads((folder / "report.json").read_text())
    config = report["config"]
    samples = pilot.read_lines(folder / "samples.jsonl")
    conversations = pilot.read_lines(folder / "conversations.jsonl")
    attempts = pilot.read_lines(folder / "attempts.jsonl")
    require(samples == report["samples"], "report differs from raw samples")
    ids = [s["episode_id"] for s in samples]
    require(len(ids) == len(set(ids)), "duplicate episodes")
    require(ids == [a["episode_id"] for a in attempts] == [c["episode_id"] for c in conversations], "attempt/conversation ledger mismatch")
    require(report["summary"] == pilot.summarize(samples, config["arms"], config["decision"]["thresholds"]), "summary cannot be reproduced")
    for path, checksum in report["fingerprint"]["code"].items():
        require(pilot.sha(ROOT / path) == checksum, "measurement code differs: " + path)
    for key, checksum in report["fingerprint"]["sources"].items():
        require(pilot.sha(ROOT / config[key]) == checksum, "source differs: " + key)
    require(pilot.sha(folder / "protocol.yaml") == report["fingerprint"]["protocol"], "protocol changed")
    require(pilot.sha(folder / "snapshot_manifest.json") == report["fingerprint"]["snapshot"], "snapshot changed")
    require(pilot.sha(folder / "seocho_source.tar.gz") == report["fingerprint"]["dependency_archive"], "archive changed")
    require(pilot.verify_dependency(args.seocho_src, folder / "seocho_source.tar.gz") == report["fingerprint"]["dependency_tree"], "dependency changed")
    sys.path.insert(0, str(args.seocho_src))
    from neo4j import GraphDatabase
    from seocho.query.workload_compiler import Text2CypherFallbackPolicy
    from seocho.query.text2cypher import validate_text2cypher_fallback as validator
    ontology = yaml.safe_load((ROOT / config["physical"]).read_text())
    mapping = yaml.safe_load((ROOT / config["mapping"]).read_text())
    properties = {"_workspace_id", "limit"} | {p for section in ("nodes", "relationships") for n in ontology[section].values() for p in n.get("properties", {})}
    policy = Text2CypherFallbackPolicy(tuple(ontology["nodes"]), tuple(ontology["relationships"]),
        tuple(sorted(properties)), "_workspace_id", max_graph_hops=config["graph"]["max_graph_hops"], max_repair_attempts=0)
    original = json.loads((folder / "preflight.json").read_text())
    cases = {c["id"]: c for c in original["cases"]}
    for sample, attempt, conv in zip(samples, attempts, conversations):
        case = cases[sample["question_id"]]
        expected = pilot.messages(case, ontology, mapping, sample["arm"], config["graph"]["max_graph_hops"])
        require(attempt["messages"] == conv["messages"] == expected, "prompt not reproducible")
        require(conv["bound_params"] == case["params"], "bindings changed")
        require(sample["valid"] and len(sample["server_receipts"]) == 1, "invalid/missing receipt")
        receipt = sample["server_receipts"][0]
        require(receipt["model"].casefold() == report["endpoint"]["model_name"].casefold(), "endpoint mismatch")
        require(bool(receipt["headers"].get("inference-id")), "inference id missing")
        require(receipt["usage"]["prompt_tokens"] == sample["prompt_tokens"] and receipt["usage"]["completion_tokens"] == sample["completion_tokens"], "token mismatch")
        if sample["executions"]:
            execution = sample["executions"][0]
            require(conv["observed_output"]["rows"] == execution["rows"], "actual output differs")
            require(sample["correct"] == pilot.score_rows(execution["rows"], case["gold_receipt"]["rows"], execution["columns"], case["result"]), "score mismatch")
    args.output_dir.mkdir(parents=True)
    pilot.write(args.output_dir / "manifest.json", {
        "manifest": pilot.runmeta.manifest(db_container=config["graph"]["container"], experiment="ontology-mapping-db-replay-audit"),
        "source_run": folder.name, "source_hashes": {name: pilot.sha(folder / name) for name in ("manifest.json", "report.json", "samples.jsonl", "conversations.jsonl", "attempts.jsonl", "preflight.json")},
        "audit_script_sha256": pilot.sha(Path(__file__)), "model_calls": 0,
        "scope": "Replay each unique admitted query with its exact parameters once. Preserve rejected queries without executing them. Timing is a replay receipt, not a pooled latency sample."})
    driver = GraphDatabase.driver(config["graph"]["uri"], auth=pilot.db_password(config["graph"]["container"]), connection_timeout=10)
    replays = []
    try:
        prepared = pilot.prepare(config, driver, validator, policy, json.loads((folder / "snapshot_manifest.json").read_text()))
        signature = lambda p: [(c["id"], c["params"], c["gold_receipt"]["rows"]) for c in p["cases"]]
        require(signature(prepared) == signature(original), "DB/Gold/bindings changed")
        pilot.write(args.output_dir / "preflight.json", prepared)
        groups = {}
        for sample in samples:
            if sample["executions"]:
                e = sample["executions"][0]
                groups.setdefault(pilot.stable([e["cypher"], e["params"]]), []).append(sample)
        for group in groups.values():
            source = group[0]["executions"][0]
            params, cypher = source["params"], source["cypher"]
            anchor_key = "company_id" if "company_id" in params else "acct_no"
            case_policy = replace(policy, required_parameters=("workspace_id", anchor_key), max_result_rows=params["limit"])
            require(not (list(validator(cypher, params=params, policy=case_policy)) + pilot.scope_gate(cypher, params)), "replay guardrail failed")
            explained = pilot.execute(driver, config["graph"], cypher, params, False)
            require(not pilot.plan_gate(explained, config["graph"]["max_leaf_estimated_rows"]), "replay plan failed")
            actual = pilot.execute(driver, config["graph"], cypher, params)
            comparisons = [{"episode_id": s["episode_id"],
                "same_rows": actual["rows"] == s["executions"][0]["rows"],
                "same_columns": actual["columns"] == s["executions"][0]["columns"],
                "same_db_hits": actual["db_hits"] == s["db_hits"]} for s in group]
            replay = {"execution": actual, "comparisons": comparisons}
            pilot.append(args.output_dir / "replay_samples.jsonl", replay)
            replays.append(replay)
        stable = all(c["same_rows"] and c["same_columns"] and c["same_db_hits"] for r in replays for c in r["comparisons"])
        result = {"valid": stable, "ledger_samples_checked": len(samples), "unique_queries_replayed": len(replays),
            "execution_samples_covered": sum(len(r["comparisons"]) for r in replays),
            "rejected_samples_not_executed": sum(not s["executions"] for s in samples),
            "summary_reproduced": True, "prompts_reproduced": True, "db_gates_passed": True, "model_calls": 0,
            "scope": "Output and DB-hit reproducibility only. This does not establish semantic generalization, statistical significance or a latency effect."}
        pilot.write(args.output_dir / "audit.json", result)
        print(pilot.stable(result))
        require(stable, "replay disagrees with original receipts; inspect raw comparisons")
    except Exception as exc:
        pilot.write(args.output_dir / "failure.json", {"error": str(exc)})
        raise
    finally:
        driver.close()


if __name__ == "__main__":
    main()
