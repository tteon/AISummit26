#!/usr/bin/env python3
"""Agents SDK role agents under procedural vs LangGraph orchestration.

This is a framework manipulation check, not a replacement for the FinBench topology arms.
The same three OpenAI Agents SDK agents, prompts, typed state, Bolt execution function and
ResultEnvelope run under two outer schedulers.  The LangGraph state contains only serializable
handoff artifacts; the driver, model and policy remain closure-scoped runtime dependencies.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, TypedDict

import yaml
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


runmeta = _load("runmeta_framework", REPO_ROOT / "scripts/analysis/runmeta.py")
interaction = _load("interaction_framework", REPO_ROOT / "scripts/agents/agent_interaction.py")
topology = _load("topology_framework", REPO_ROOT / "scripts/benchmarks/bench_agent_topology.py")

from agents import Agent, ModelSettings, RunConfig, Runner  # noqa: E402
from harness.llm import add_provider_args, agents_model, model_config  # noqa: E402
from harness.seocho_bridge import _ensure_seocho_on_path, enable_observability  # noqa: E402
from harness.tracing import init_tracing, shutdown_tracing, span  # noqa: E402


class WorkflowState(TypedDict, total=False):
    question: str
    planner_raw: str
    intent: Dict[str, Any]
    executor_input: str
    cypher: str
    evidence: Dict[str, Any]
    rows: List[Dict[str, Any]]
    db_metrics: Dict[str, Any]
    verifier_input: str
    verifier_raw: str
    verifier: Dict[str, Any]
    stages: List[Dict[str, Any]]
    handoff_chars: int


PLANNER = topology.PLANNER_SYSTEM
VERIFIER = topology.VERIFIER_SYSTEM
EXECUTOR_PREFIX = """You are the Cypher executor stage of an AML graph workflow.
Return one JSON object only with key cypher. Use only the supplied physical schema.
Every matched node must include {_workspace_id: $workspace_id} inline. Use only named
parameters $a, $acct_no, $workspace_id, $ws and $limit; never inline the anchor. Return a
read-only query ending in LIMIT $limit. Preserve relationship direction and aggregation."""


def _usage(result: Any, stage: str, elapsed_ms: float) -> Dict[str, Any]:
    usage = result.context_wrapper.usage
    return {
        "stage": stage,
        "prompt_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "requests": int(getattr(usage, "requests", 0) or 0),
        "elapsed_ms": round(elapsed_ms, 1),
    }


async def _agent_call(agent: Agent[Any], prompt: str, stage: str,
                      usage_sink: List[Dict[str, Any]]) -> tuple[str, Dict[str, Any]]:
    started = time.perf_counter()
    event: Dict[str, Any] = {
        "stage": stage, "attempted": True, "completed": False,
        "prompt_tokens": 0, "completion_tokens": 0, "requests": 0,
    }
    usage_sink.append(event)
    try:
        with span(f"framework.stage.{stage}", stage=stage, runtime="openai_agents_sdk"):
            result = await Runner.run(
                agent, prompt, max_turns=1,
                run_config=RunConfig(
                    tracing_disabled=True, workflow_name="finbench-framework-parity"),
            )
    except Exception as exc:
        event["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        event["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        raise
    event.update(_usage(result, stage, (time.perf_counter() - started) * 1000))
    event["completed"] = True
    return str(result.final_output or ""), event


def _make_nodes(*, agents: Dict[str, Agent[Any]], context_policy: str,
                schema: Dict[str, Any], driver: Any, database: str,
                policy: Any, params: Dict[str, Any], row_cap: int,
                usage_sink: List[Dict[str, Any]]) -> Dict[str, Any]:
    from seocho.query.workload_compiler import validate_text2cypher_fallback

    async def planner_node(state: WorkflowState) -> Dict[str, Any]:
        raw, event = await _agent_call(
            agents["planner"], state["question"], "planner", usage_sink)
        return {"planner_raw": raw,
                "intent": topology._json_object(raw) or {"parse_error": True, "raw": raw},
                "stages": [*state.get("stages", []), event]}

    async def executor_node(state: WorkflowState) -> Dict[str, Any]:
        if context_policy == "full_transcript":
            handoff = (f"Full workflow transcript:\nUSER QUESTION:\n{state['question']}\n\n"
                       f"PLANNER RESPONSE:\n{state['planner_raw']}\n")
        else:
            handoff = topology._stable_json({"question": state["question"],
                                             "intent": state["intent"]})
        executor_input = topology._stable_json({
            "handoff": handoff, "physical_schema": schema,
            "available_parameters": sorted(params),
        })
        raw, event = await _agent_call(
            agents["executor"], executor_input, "executor", usage_sink)
        payload = topology._json_object(raw) or {}
        cypher = str(payload.get("cypher") or "").strip()
        violations = validate_text2cypher_fallback(cypher, params=params, policy=policy)
        if violations:
            raise ValueError("agents-sdk Cypher rejected: " + ",".join(violations))

        def explain() -> None:
            with driver.session(database=database) as session:
                session.run("EXPLAIN " + cypher, **params).consume()

        await asyncio.to_thread(explain)
        execution = await asyncio.to_thread(
            topology._execute_profile, driver, database=database, cypher=cypher,
            params=params, row_cap=row_cap, exact_completeness=True)
        return {"executor_input": executor_input, "cypher": cypher,
                "evidence": execution["evidence"], "rows": execution["rows"],
                "db_metrics": execution["metrics"],
                "handoff_chars": len(handoff),
                "stages": [*state.get("stages", []), event]}

    async def verifier_node(state: WorkflowState) -> Dict[str, Any]:
        if context_policy == "full_transcript":
            prompt = (f"FULL TRANSCRIPT\nQuestion: {state['question']}\n"
                      f"Planner: {state['planner_raw']}\nCypher: {state['cypher']}\n"
                      f"ResultEnvelope: {topology._stable_json(state['evidence'])}")
        else:
            prompt = topology._stable_json({
                "question": state["question"], "query_intent": state["intent"],
                "cypher": state["cypher"], "ResultEnvelope": state["evidence"],
            })
        raw, event = await _agent_call(
            agents["verifier"], prompt, "verifier", usage_sink)
        return {"verifier_input": prompt, "verifier_raw": raw,
                "verifier": topology._json_object(raw) or {"parse_error": True},
                "handoff_chars": state.get("handoff_chars", 0) + len(prompt),
                "stages": [*state.get("stages", []), event]}

    return {"planner": planner_node, "executor": executor_node, "verifier": verifier_node}


async def _orchestrate(nodes: Dict[str, Any], scheduler: str,
                       question: str) -> WorkflowState:
    state: WorkflowState = {"question": question, "stages": [], "handoff_chars": 0}
    if scheduler == "procedural":
        for name in ("planner", "executor", "verifier"):
            state.update(await nodes[name](state))
        return state

    from langgraph.graph import END, START, StateGraph
    graph = StateGraph(WorkflowState)
    for name in ("planner", "executor", "verifier"):
        graph.add_node(name, nodes[name])
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "executor")
    graph.add_edge("executor", "verifier")
    graph.add_edge("verifier", END)
    return await graph.compile().ainvoke(state)


async def main_async(args: Any) -> None:
    _ensure_seocho_on_path(args.seocho_src)
    from seocho.ontology import Ontology
    from seocho.query.hybrid_planner import policy_from_ontology, schema_for_prompt

    ontology = Ontology.from_dict(yaml.safe_load(Path(args.ontology).read_text()))
    policy = policy_from_ontology(ontology)
    schema = schema_for_prompt(ontology, policy)
    cfg = model_config(args, max_tokens=args.max_tokens, temperature=0.0)
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    question = next((q for q in interaction.QUESTIONS if q["id"] == args.question), None)
    if question is None:
        raise SystemExit(f"unknown question: {args.question}")
    anchor = topology._anchor(driver, args.database)
    gold = topology._gold(driver, database=args.database, question=question, anchor=anchor)
    if not gold:
        raise SystemExit("blind gold")
    print(f"[gold] {args.database} anchor={anchor} qid={args.question} rows={len(gold)}")
    if args.validate_only:
        driver.close()
        return

    out_dir = Path(args.output_dir) / args.run_id
    if out_dir.exists() and not args.resume:
        raise SystemExit(f"output exists; choose new --run-id or --resume: {out_dir}")
    # Must precede creation of the run directory so the run cannot dirty its own manifest.
    run_manifest = runmeta.manifest(experiment="framework-context-parity")
    out_dir.mkdir(parents=True, exist_ok=True)
    samples_path = out_dir / "samples.jsonl"
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = args.otlp_endpoint
    os.environ["TRACE_JSONL_PATH"] = str(out_dir / "spans.jsonl")
    init_tracing("aisummit26-framework-context")
    observability = enable_observability(
        backend="otlp", endpoint=args.otlp_endpoint,
        service_name="seocho-framework-context", source=args.seocho_src,
        enable_metrics=False)

    header = {
        "schema_version": "seocho.finbench.framework-context.v1",
        "run_id": args.run_id,
        "manifest": run_manifest,
        "endpoint": cfg.descriptor(), "observability": observability,
        "config": {k: v for k, v in vars(args).items() if k != "password"},
        "graph": {"database": args.database, "anchor": anchor},
        "frameworks": {"openai_agents_sdk": __import__("agents").__version__,
                       "langgraph": "0.6.11"},
    }
    (out_dir / "manifest.json").write_text(json.dumps(header, indent=2, default=str) + "\n")
    print(f"[endpoint] {cfg.provider} {cfg.model_name} @ {cfg.base_url}")

    model = agents_model(cfg)
    role_agents = {
        "planner": Agent(name="FinBench Planner", instructions=PLANNER, model=model,
                         model_settings=ModelSettings(temperature=0.0)),
        "executor": Agent(name="FinBench Cypher Executor",
                          instructions=EXECUTOR_PREFIX, model=model,
                          model_settings=ModelSettings(temperature=0.0)),
        "verifier": Agent(name="FinBench Evidence Verifier", instructions=VERIFIER,
                          model=model, model_settings=ModelSettings(temperature=0.0)),
    }
    params = {"a": anchor, "acct_no": anchor, "workspace_id": topology.WS,
              "ws": topology.WS, "limit": args.row_cap}
    completed: set[str] = set()
    rows: List[Dict[str, Any]] = []
    if args.resume and samples_path.exists():
        for line in samples_path.read_text().splitlines():
            rec = json.loads(line)
            rows.append(rec)
            completed.add(rec["episode_id"])

    with samples_path.open("a") as out:
        for scheduler in args.schedulers:
            for context_policy in args.context_policies:
                episode_id = f"{args.run_id}:{scheduler}:{context_policy}:{args.question}"
                if episode_id in completed:
                    continue
                started = time.perf_counter()
                usage_sink: List[Dict[str, Any]] = []
                root = None
                try:
                    with span("framework.context.episode", run_id=args.run_id,
                              episode_id=episode_id, scheduler=scheduler,
                              context_policy=context_policy) as root:
                        nodes = _make_nodes(
                            agents=role_agents, context_policy=context_policy,
                            schema=schema, driver=driver, database=args.database,
                            policy=policy, params=params, row_cap=args.row_cap,
                            usage_sink=usage_sink)
                        state = await _orchestrate(
                            nodes, scheduler, question["question"].format(a=anchor))
                        score = interaction.score(question, gold, state["rows"])
                        trace_id = (f"{root.get_span_context().trace_id:032x}"
                                    if root is not None else None)
                    rec = {
                        "episode_id": episode_id, "run_id": args.run_id,
                        "scheduler": scheduler, "context_policy": context_policy,
                        "agent_runtime": "openai_agents_sdk", "question_id": args.question,
                        "trace_id": trace_id, "correct": bool(score["correct"]),
                        "score": score, "wall_ms": round((time.perf_counter() - started) * 1000, 1),
                        "prompt_tokens": sum(x["prompt_tokens"] for x in state["stages"]),
                        "completion_tokens": sum(x["completion_tokens"] for x in state["stages"]),
                        "model_calls": sum(x["requests"] for x in state["stages"]),
                        "handoff_chars": state["handoff_chars"], "cypher": state["cypher"],
                        "db_hits": state["db_metrics"]["db_hits"],
                        "db_ms": state["db_metrics"]["elapsed_ms"],
                        "result_bytes": state["db_metrics"]["result_bytes"],
                        "verifier": state["verifier"], "stages": state["stages"],
                    }
                except Exception as exc:
                    trace_id = (f"{root.get_span_context().trace_id:032x}"
                                if root is not None else None)
                    rec = {"episode_id": episode_id, "run_id": args.run_id,
                           "scheduler": scheduler, "context_policy": context_policy,
                           "agent_runtime": "openai_agents_sdk", "question_id": args.question,
                           "trace_id": trace_id, "correct": False,
                           "model_calls": len(usage_sink),
                           "model_calls_completed": sum(
                               bool(x.get("completed")) for x in usage_sink),
                           "prompt_tokens": sum(
                               int(x.get("prompt_tokens", 0)) for x in usage_sink),
                           "completion_tokens": sum(
                               int(x.get("completion_tokens", 0)) for x in usage_sink),
                           "stages": usage_sink,
                           "error": f"{type(exc).__name__}: {str(exc)[:1000]}"}
                out.write(json.dumps(rec, default=str) + "\n")
                out.flush()
                os.fsync(out.fileno())
                rows.append(rec)
                print(f"  {episode_id} correct={rec.get('correct')} "
                      f"tokens={rec.get('prompt_tokens', 0)} error={rec.get('error', '')[:120]}")

    driver.close()
    try:
        from seocho.tracing import flush_tracing
        flush_tracing()
    except Exception:
        pass
    shutdown_tracing()
    receipt = topology._trace_receipt(rows, out_dir / "spans.jsonl", args.tempo_url)
    (out_dir / "trace_receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    report = {**header, "trace_receipt": receipt, "samples": rows}
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_provider_args(parser)
    parser.add_argument("--uri", default="bolt://localhost:7688")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", required=True)
    parser.add_argument("--database", default="finbenchl1")
    parser.add_argument("--ontology", default="ontology/finbench.ontology.yaml")
    parser.add_argument("--seocho-src", default=None)
    parser.add_argument("--question", default="ext_med_2")
    parser.add_argument("--schedulers", nargs="+", choices=("procedural", "langgraph"),
                        default=["procedural", "langgraph"])
    parser.add_argument("--context-policies", nargs="+",
                        choices=("full_transcript", "typed_isolated"),
                        default=["full_transcript", "typed_isolated"])
    parser.add_argument("--row-cap", type=int, default=50)
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output-dir", default="results/episodes/framework_context")
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
