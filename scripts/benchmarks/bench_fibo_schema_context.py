#!/usr/bin/env python3
"""Measure physical vs compiled vs retrieved FIBO context at the MARA->Bolt boundary.

The physical graph, validator, model, endpoint, parameters and questions are fixed.  Only
the semantic context attached to the question changes.  FIBO mappings are projections and
carry status (proxy/informative/unsupported); they are never silently rewritten as physical
truth.  Each episode is append-only and has local + Tempo trace receipts.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


runmeta = _load("runmeta_fibo_context", REPO_ROOT / "scripts/analysis/runmeta.py")
fibo_suite = _load("fibo_suite_context", REPO_ROOT / "scripts/benchmarks/run_fibo_suite.py")
topology = _load("topology_fibo_context", REPO_ROOT / "scripts/benchmarks/bench_agent_topology.py")

from harness.llm import add_provider_args, model_config  # noqa: E402
from harness.seocho_bridge import (  # noqa: E402
    _ensure_seocho_on_path, enable_observability, make_llm_backend,
    schema_map_from_ontology,
)
from harness.tracing import init_tracing, shutdown_tracing, span  # noqa: E402

ARMS = ("physical_only", "compiled_fibo", "retrieved_fibo")


def _cards(projection: Dict[str, Any]) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    for logical, spec in (projection.get("nodes") or {}).items():
        cards.append({"kind": "node", "logical": logical, "physical": spec.get("physical"),
                      "status": "proxy", "properties": spec.get("properties", {})})
    for logical, spec in (projection.get("relationships") or {}).items():
        cards.append({"kind": "relationship", "logical": logical,
                      "physical": spec.get("physical"),
                      "status": spec.get("status", "proxy"),
                      "source": spec.get("source"), "target": spec.get("target"),
                      "semantic_scope": spec.get("semantic_scope"),
                      "reason": spec.get("reason")})
    for physical, spec in (projection.get("physical_extensions") or {}).items():
        cards.append({"kind": "local_extension", "logical": spec.get("semantic_term"),
                      "physical": physical, "status": spec.get("status"),
                      "source": spec.get("source"), "target": spec.get("target")})
    return cards


def _tokens(value: str) -> set[str]:
    parts = re.findall(r"[A-Za-z][A-Za-z0-9_]+", value.lower().replace("_", " "))
    return {p for p in parts if len(p) > 2}


def _retrieve(cards: List[Dict[str, Any]], query: str, k: int) -> List[Dict[str, Any]]:
    query_tokens = _tokens(query)
    ranked = []
    for index, card in enumerate(cards):
        card_tokens = _tokens(topology._stable_json(card))
        overlap = len(query_tokens & card_tokens)
        # Stable tie-break: physical account vocabulary is useful but never magic oracle data.
        ranked.append((overlap, -index, card))
    ranked.sort(reverse=True, key=lambda x: (x[0], x[1]))
    return [card for score, _, card in ranked[:k] if score > 0]


def _semantic_context(arm: str, projection: Dict[str, Any], cards: List[Dict[str, Any]],
                      question: Dict[str, Any], k: int) -> str:
    if arm == "physical_only":
        return ""
    source = projection.get("semantic_source") or {}
    selected = cards if arm == "compiled_fibo" else _retrieve(
        cards, f"{question['question']} {question.get('fibo_anchor', '')}", k)
    return topology._stable_json({
        "contract": "Semantic terms are navigation hints only. Generate executable Cypher "
                    "with physical names. Refuse unsupported mappings; never invent an edge.",
        "fibo_release": source.get("release"), "fibo_commit": source.get("commit"),
        "cards": selected,
    })


def _aggregate(rows: Iterable[Dict[str, Any]], arm: str) -> Dict[str, Any]:
    selected = [r for r in rows if r.get("arm") == arm]
    if not selected:
        return {"n": 0}
    successful = [r for r in selected if "error" not in r]
    return {
        "n": len(selected), "errors": len(selected) - len(successful),
        "correct": sum(bool(r["correct"]) for r in selected),
        "correct_rate": round(sum(bool(r["correct"]) for r in selected) / len(selected), 4),
        "model_calls": sum(r.get("model_calls", 0) for r in selected),
        "model_calls_completed": sum(r.get("model_calls_completed", 0) for r in selected),
        "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in selected),
        "completion_tokens": sum(r.get("completion_tokens", 0) for r in selected),
        "semantic_context_chars": sum(r["semantic_context_chars"] for r in selected),
        "attempts": sum(r.get("attempts", 0) for r in selected),
        "db_hits": sum(r.get("db_hits", 0) for r in selected),
        "generate_ms_median": (round(statistics.median(r["generate_ms"] for r in successful), 2)
                               if successful else 0.0),
        "db_ms_median": (round(statistics.median(r["db_ms"] for r in successful), 2)
                         if successful else 0.0),
    }


async def main_async(args: Any) -> None:
    _ensure_seocho_on_path(args.seocho_src)
    from seocho.ontology import Ontology
    from seocho.query.hybrid_planner import policy_from_ontology
    from seocho.query.text2cypher import generate_validated_cypher

    suite = yaml.safe_load(Path(args.suite).read_text())
    projection = yaml.safe_load(Path(args.projection).read_text())
    ontology = Ontology.from_dict(yaml.safe_load(Path(args.ontology).read_text()))
    schema = schema_map_from_ontology(ontology)
    policy = policy_from_ontology(ontology)
    cards = _cards(projection)
    cfg = model_config(args, max_tokens=args.max_tokens, temperature=0.0)
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    anchor = topology._anchor(driver, args.database)
    ws = suite.get("workspace_id", "default")
    selected_ids = set(args.only or [q["id"] for q in suite["questions"]])
    questions = [q for q in suite["questions"] if q["id"] in selected_ids]
    missing = selected_ids - {q["id"] for q in questions}
    if missing:
        raise SystemExit(f"unknown FIBO questions: {sorted(missing)}")

    prepared: Dict[str, Any] = {}
    for question in questions:
        declared = dict(question.get("params") or {})
        bound = {k: (anchor if v == "anchor" else v) for k, v in declared.items()}
        bound["workspace_id"] = ws
        with driver.session(database=args.database) as session:
            result = session.run(" ".join(str(question["gold"]).split()), **bound)
            gold = [dict(row) for row in result]
            result.consume()
        if not gold:
            raise SystemExit(f"blind gold: {question['id']}")
        prepared[question["id"]] = {"params": bound,
                                     "gold": fibo_suite.norm_rows(gold)}
    print(f"[gold] {args.database} anchor={anchor} questions={len(questions)}")
    if args.validate_only:
        driver.close()
        return

    out_dir = Path(args.output_dir) / args.run_id
    if out_dir.exists() and not args.resume:
        raise SystemExit(f"output exists; choose a new --run-id or --resume: {out_dir}")
    # Must precede creation of the run directory so the run cannot dirty its own manifest.
    run_manifest = runmeta.manifest(experiment="fibo-schema-context",
                                    db_container=args.db_container)
    out_dir.mkdir(parents=True, exist_ok=True)
    samples_path = out_dir / "samples.jsonl"
    conversations_path = out_dir / "conversations.jsonl"
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = args.otlp_endpoint
    os.environ["TRACE_JSONL_PATH"] = str(out_dir / "spans.jsonl")
    init_tracing("aisummit26-fibo-schema-context")
    obs = enable_observability(
        backend="otlp", endpoint=args.otlp_endpoint,
        service_name="seocho-fibo-schema-context", source=args.seocho_src,
        enable_metrics=False)
    header = {
        "schema_version": "seocho.fibo.schema-context.v1", "run_id": args.run_id,
        "manifest": run_manifest,
        "endpoint": cfg.descriptor(), "observability": obs,
        "config": {k: v for k, v in vars(args).items() if k != "password"},
        "graph": {"database": args.database, "anchor": anchor},
        "semantic_source": projection.get("semantic_source"),
    }
    (out_dir / "manifest.json").write_text(json.dumps(header, indent=2, default=str) + "\n")
    print(f"[endpoint] {cfg.provider} {cfg.model_name} @ {cfg.base_url}")

    completed: set[str] = set()
    rows: List[Dict[str, Any]] = []
    if args.resume and samples_path.exists():
        for line in samples_path.read_text().splitlines():
            rec = json.loads(line)
            rows.append(rec)
            completed.add(rec["episode_id"])

    with samples_path.open("a") as sample_out, conversations_path.open("a") as conv_out:
        for question in questions:
            prep = prepared[question["id"]]
            text = question["question"].format(
                a=anchor, **{k: v for k, v in prep["params"].items()
                             if k != "workspace_id"})
            for repeat in range(args.repeats):
                for arm in args.arms:
                    episode_id = f"{args.run_id}:{question['id']}:r{repeat}:{arm}"
                    if episode_id in completed:
                        continue
                    semantic = _semantic_context(
                        arm, projection, cards, question, args.retrieval_k)
                    model_question = text if not semantic else (
                        text + "\n\nSEMANTIC PROJECTION CONTEXT:\n" + semantic)
                    usage: List[Dict[str, Any]] = []
                    backend = make_llm_backend(cfg)
                    original = backend.acomplete

                    async def tracked(*a: Any, **kw: Any) -> Any:
                        started_call = time.perf_counter()
                        event: Dict[str, Any] = {"attempted": True, "completed": False,
                                                 "prompt_tokens": 0,
                                                 "completion_tokens": 0,
                                                 "cached_tokens": 0}
                        usage.append(event)
                        try:
                            with span("fibo.stage.text2cypher_model", arm=arm,
                                      question_id=question["id"]):
                                response = await original(*a, **kw)
                        except Exception as exc:
                            event["elapsed_ms"] = round(
                                (time.perf_counter() - started_call) * 1000, 1)
                            event["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                            raise
                        event.update(dict(getattr(response, "usage", None) or {}))
                        event["completed"] = True
                        event["elapsed_ms"] = round(
                            (time.perf_counter() - started_call) * 1000, 1)
                        return response

                    backend.acomplete = tracked  # type: ignore[method-assign]

                    async def explain(cypher: str, bound: Dict[str, Any]) -> None:
                        def run() -> None:
                            with driver.session(database=args.database) as session:
                                session.run("EXPLAIN " + cypher, **bound).consume()
                        await asyncio.to_thread(run)

                    started = time.perf_counter()
                    root = None
                    try:
                        with span("fibo.schema-context.episode", run_id=args.run_id,
                                  episode_id=episode_id, arm=arm,
                                  question_id=question["id"], family=question["family"]) as root:
                            generated = await generate_validated_cypher(
                                question=model_question, schema=schema,
                                params=prep["params"], policy=policy, backend=backend,
                                model=cfg.model_name, explain=explain)
                            generated_ms = (time.perf_counter() - started) * 1000
                            execution = await asyncio.to_thread(
                                topology._execute_profile, driver, database=args.database,
                                cypher=generated.cypher, params=dict(generated.params),
                                row_cap=int(prep["params"].get("limit", args.row_cap)),
                                exact_completeness=True)
                            got = fibo_suite.norm_rows(execution["rows"])
                            trace_id = (f"{root.get_span_context().trace_id:032x}"
                                        if root is not None else None)
                        rec = {
                            "episode_id": episode_id, "run_id": args.run_id, "arm": arm,
                            "question_id": question["id"], "family": question["family"],
                            "difficulty": question["difficulty"], "repeat": repeat,
                            "trace_id": trace_id, "correct": got == prep["gold"],
                            "nonempty_overlap": bool(set(got) & set(prep["gold"])),
                            "cypher": generated.cypher, "attempts": generated.attempts,
                            "generate_ms": round(generated_ms, 1),
                            "model_calls": len(usage),
                            "model_calls_completed": sum(bool(u.get("completed")) for u in usage),
                            "prompt_tokens": sum(int(u.get("prompt_tokens", 0) or 0) for u in usage),
                            "completion_tokens": sum(int(u.get("completion_tokens", 0) or 0) for u in usage),
                            "cached_tokens": sum(int(u.get("cached_tokens", 0) or 0) for u in usage),
                            "semantic_context_chars": len(semantic),
                            "db_hits": execution["metrics"]["db_hits"],
                            "db_ms": execution["metrics"]["elapsed_ms"],
                            "result_bytes": execution["metrics"]["result_bytes"],
                            "rows": execution["metrics"]["rows"],
                        }
                        conversation = {"episode_id": episode_id, "trace_id": trace_id,
                                        "question": text, "semantic_context": semantic,
                                        "cypher": generated.cypher}
                    except Exception as exc:
                        trace_id = (f"{root.get_span_context().trace_id:032x}"
                                    if root is not None else None)
                        rec = {"episode_id": episode_id, "run_id": args.run_id, "arm": arm,
                               "question_id": question["id"], "family": question["family"],
                               "difficulty": question["difficulty"], "repeat": repeat,
                               "trace_id": trace_id, "correct": False,
                               "model_calls": len(usage),
                               "model_calls_completed": sum(bool(u.get("completed")) for u in usage),
                               "prompt_tokens": sum(int(u.get("prompt_tokens", 0) or 0)
                                                    for u in usage),
                               "completion_tokens": sum(int(u.get("completion_tokens", 0) or 0)
                                                        for u in usage),
                               "cached_tokens": sum(int(u.get("cached_tokens", 0) or 0)
                                                    for u in usage),
                               "semantic_context_chars": len(semantic),
                               "attempts": len(usage), "db_hits": 0, "db_ms": 0.0,
                               "result_bytes": 0, "rows": 0,
                               "generate_ms": round((time.perf_counter() - started) * 1000, 1),
                               "error": f"{type(exc).__name__}: {str(exc)[:1000]}"}
                        conversation = {"episode_id": episode_id, "trace_id": trace_id,
                                        "question": text,
                                        "semantic_context": semantic, "error": rec["error"]}
                    sample_out.write(json.dumps(rec, default=str) + "\n")
                    sample_out.flush(); os.fsync(sample_out.fileno())
                    conv_out.write(json.dumps(conversation, default=str) + "\n")
                    conv_out.flush(); os.fsync(conv_out.fileno())
                    rows.append(rec)
                    print(f"  {question['id']:24s} {arm:15s} correct={rec.get('correct')} "
                          f"prompt={rec.get('prompt_tokens', 0)} hits={rec.get('db_hits', 0)} "
                          f"{rec.get('error', '')[:100]}", flush=True)

    driver.close()
    try:
        from seocho.tracing import flush_tracing
        flush_tracing()
    except Exception:
        pass
    shutdown_tracing()
    receipt = topology._trace_receipt(rows, out_dir / "spans.jsonl", args.tempo_url)
    (out_dir / "trace_receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    report = {**header, "trace_receipt": receipt,
              "summary": {arm: _aggregate(rows, arm) for arm in args.arms},
              "samples": rows}
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    print("=== summary ===")
    for arm, summary in report["summary"].items():
        print(f"  {arm:15s} {summary}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_provider_args(parser)
    parser.add_argument("--uri", default="bolt://localhost:7688")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", required=True)
    parser.add_argument("--database", default="finbenchl1")
    parser.add_argument("--suite", default="configs/fibo_text2cypher_suite.yaml")
    parser.add_argument("--ontology", default="ontology/finbench.ontology.yaml")
    parser.add_argument("--projection", default="ontology/fibo_finbench.projection.yaml")
    parser.add_argument("--arms", nargs="+", choices=ARMS, default=list(ARMS))
    parser.add_argument("--only", nargs="+", default=None)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--retrieval-k", type=int, default=5)
    parser.add_argument("--row-cap", type=int, default=50)
    parser.add_argument("--max-tokens", type=int, default=2000)
    parser.add_argument("--seocho-src", default=None)
    parser.add_argument("--db-container", default="aisummit-simtest")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output-dir", default="results/episodes/fibo_schema_context")
    parser.add_argument("--otlp-endpoint", default="http://127.0.0.1:4317")
    parser.add_argument("--tempo-url", default="http://127.0.0.1:3200")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.run_id is None:
        args.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
