#!/usr/bin/env python3
"""Single-agent vs role-agent context topology at the Agent API -> Bolt boundary.

This is deliberately a *new* experiment, not another arm in the published interaction
figure.  It holds endpoint, model, ontology, question, graph, scope, anchor, row budget and
validator fixed while changing who sees which context:

``direct_single``
    One generation and one graph execution (the product baseline).
``staged_single``
    One logical investigator performs plan -> execute -> verify.  Every stage receives the
    accumulated transcript.
``multi_full``
    Planner, query specialist and verifier are separate roles, but the full transcript is
    copied across every handoff.
``multi_typed``
    The same three roles and calls, with a typed QueryIntent/EvidencePacket handoff.
``multi_envelope``
    ``multi_typed`` plus an agent-native ResultEnvelope whose completeness is exact.  The
    harness fetches cap+1 rows; legacy arms cannot distinguish exactly-cap from truncated.

The experiment records observable decisions, generated Cypher, per-stage token usage,
handoff bytes, query fingerprints and PROFILE/Bolt costs.  It does not request or retain
private chain-of-thought.  Samples are appended before the aggregate report is written, and a
manifest is written before the first paid request.

Example (validate first):

    python3 scripts/benchmarks/bench_agent_topology.py --password "$NEO4J_PASSWORD" \
      --databases finbenchl1:1 --only ext_med_1 --validate-only

    python3 scripts/benchmarks/bench_agent_topology.py --password "$NEO4J_PASSWORD" \
      --provider mara --databases finbenchl1:1 --only ext_med_1 ext_hard_1 int_hard_1b
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import random
import re
import statistics
import sys
import time
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

_runmeta_spec = importlib.util.spec_from_file_location(
    "runmeta", REPO_ROOT / "scripts" / "analysis" / "runmeta.py")
runmeta = importlib.util.module_from_spec(_runmeta_spec)
_runmeta_spec.loader.exec_module(runmeta)  # type: ignore[union-attr]

_interaction_spec = importlib.util.spec_from_file_location(
    "agent_interaction", REPO_ROOT / "scripts" / "agents" / "agent_interaction.py")
interaction = importlib.util.module_from_spec(_interaction_spec)
_interaction_spec.loader.exec_module(interaction)  # type: ignore[union-attr]

from harness.llm import add_provider_args, model_config  # noqa: E402
from harness.seocho_bridge import (  # noqa: E402
    _ensure_seocho_on_path, enable_observability, make_llm_backend,
)
from harness.system_monitor import SystemMetricsSampler  # noqa: E402
from harness.tracing import init_tracing, shutdown_tracing, span  # noqa: E402


ARMS = ("direct_single", "staged_single", "multi_full", "multi_typed",
        "multi_envelope")
DEFAULT_DIAGNOSTIC = ("ext_med_2", "ext_hard_1", "ext_hard_2", "int_med_1",
                      "int_hard_1", "int_hard_1b", "int_hard_2")
WS = "default"

PLANNER_SYSTEM = """You are the planning stage of an AML investigation workflow.
Create a concise, observable decision record for a graph query. Do not provide hidden
chain-of-thought. Return one JSON object only with keys: entities, relationships,
predicates, aggregation, ordering, expected_shape, assumptions, uncertainties,
acceptance_tests. Relationship entries must state direction when the question does."""

VERIFIER_SYSTEM = """You are the verification stage of an AML investigation workflow.
Check only observable artifacts: the user question, QueryIntent, Cypher and returned
evidence. Return one JSON object only: {\"pass\": boolean, \"reason_codes\": [strings],
\"revision\": string}. revision must be an actionable correction when pass is false.
Do not provide hidden chain-of-thought."""

ARM_FACTORS = {
    "direct_single": {"topology": "single_call", "context_policy": "question_only",
                      "result_contract": "legacy_rows"},
    "staged_single": {"topology": "single_logical_agent", "context_policy": "full_transcript",
                      "result_contract": "legacy_rows"},
    "multi_full": {"topology": "role_agents", "context_policy": "full_transcript",
                   "result_contract": "legacy_rows"},
    "multi_typed": {"topology": "role_agents", "context_policy": "typed_isolated",
                    "result_contract": "legacy_rows"},
    "multi_envelope": {"topology": "role_agents", "context_policy": "typed_isolated",
                       "result_contract": "result_envelope_v1"},
}


class CaseRunError(RuntimeError):
    """A failed episode with the paid work performed before the failure attached."""

    def __init__(self, cause: BaseException, *, stages: List[Dict[str, Any]],
                 conversation: Dict[str, Any], decisions: Dict[str, Any],
                 handoff_chars: int = 0) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.stages = list(stages)
        self.conversation = conversation
        self.decisions = decisions
        self.handoff_chars = handoff_chars


class StageCallError(RuntimeError):
    def __init__(self, cause: BaseException, event: Dict[str, Any]) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.event = event


def _json_object(text: str) -> Optional[Dict[str, Any]]:
    """Parse the first balanced JSON object from a model response."""
    raw = (text or "").strip()
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(raw[start:i + 1])
                    return value if isinstance(value, dict) else None
                except ValueError:
                    return None
    return None


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _query_fingerprint(cypher: str, params: Dict[str, Any]) -> str:
    normalized = re.sub(r"\s+", " ", cypher).strip().lower()
    return _hash(normalized + "\n" + _stable_json(params))


def _request_params(question: Dict[str, Any], *, anchor: int, row_cap: int) -> Dict[str, Any]:
    """Bind a declarative request's user inputs while keeping scope/cap harness-owned."""
    supplied = dict(question.get("params") or {})
    bound = {key: (anchor if value == "anchor" else value)
             for key, value in supplied.items()}
    # These names are service policy, never model-selected request fields.
    bound.update({"a": anchor, "acct_no": anchor, "ws": WS,
                  "workspace_id": WS, "limit": row_cap})
    return bound


def _request_metadata(question: Dict[str, Any]) -> Dict[str, Any]:
    """Stable workload labels for grouping user-visible request cost after a sweep."""
    return {
        "request_type": question.get("request_type", question.get("audience", "unspecified")),
        "real_world_case": question.get("real_world_case"),
        "schema_facets": list(question.get("schema_facets") or []),
        "repair_risks": list(question.get("repair_risks") or []),
        "parameter_contract": sorted((question.get("params") or {}).keys()),
    }


def _repair_ledger(*, stages: List[Dict[str, Any]], executions: List[Dict[str, Any]],
                   initial_correct: bool, final_correct: bool,
                   verifier_requested: bool, verifier_mode: str,
                   repair_applied: bool, repair_elapsed_ms: float) -> Dict[str, Any]:
    """Charge the incremental repair path without treating hosted API time as GPU cost."""
    initial_executor = [s for s in stages if s.get("stage") == "executor"]
    repair_executor = [s for s in stages if s.get("stage") == "repair_executor"]
    validator_retry = initial_executor[1:]
    repair_model_events = validator_retry + repair_executor
    repair_executions = executions[1:]
    return {
        "schema_version": "seocho.repair-loop-ledger.v1",
        "verifier_requested": verifier_requested,
        "verifier_mode": verifier_mode,
        "repair_applied": repair_applied,
        "validator_retry_generations": len(validator_retry),
        "verifier_repair_generations": len(repair_executor),
        "repair_model_calls": len(repair_model_events),
        "repair_api_elapsed_ms": round(sum(float(s.get("elapsed_ms", 0) or 0)
                                           for s in repair_model_events), 1),
        "repair_prompt_tokens": sum(int(s.get("prompt_tokens", 0) or 0)
                                    for s in repair_model_events),
        "repair_completion_tokens": sum(int(s.get("completion_tokens", 0) or 0)
                                        for s in repair_model_events),
        "repair_graph_trips": len(repair_executions),
        "repair_db_hits": sum(int(e.get("db_hits", 0) or 0) for e in repair_executions),
        "repair_db_ms": round(sum(float(e.get("elapsed_ms", 0) or 0)
                                  for e in repair_executions), 3),
        "repair_loop_wall_ms": round(repair_elapsed_ms, 1),
        "initial_correct": bool(initial_correct),
        "final_correct": bool(final_correct),
        "converged_to_correct": bool(not initial_correct and final_correct),
        "regressed_from_correct": bool(initial_correct and not final_correct),
    }


def _profile_db_hits(plan: Any) -> int:
    if plan is None:
        return 0
    args = ((plan.get("args") or plan.get("arguments") or {}) if isinstance(plan, dict)
            else getattr(plan, "arguments", None) or {})
    hits = int(args.get("DbHits", 0) or 0)
    children = (plan.get("children", []) if isinstance(plan, dict)
                else getattr(plan, "children", None) or [])
    for child in children:
        hits += _profile_db_hits(child)
    return hits


def _profile_tree(plan: Any) -> Optional[Dict[str, Any]]:
    """Serialize the complete Bolt PROFILE tree without changing its cost denominator."""
    if plan is None:
        return None
    if isinstance(plan, dict):
        operator = plan.get("operatorType") or plan.get("operator_type")
        args = plan.get("args", {})
        identifiers = plan.get("identifiers", [])
        children = plan.get("children", [])
    else:
        operator = getattr(plan, "operator_type", None)
        args = getattr(plan, "arguments", None) or {}
        identifiers = getattr(plan, "identifiers", None) or []
        children = getattr(plan, "children", None) or []
    return {
        "operator_type": operator,
        "identifiers": list(identifiers),
        "arguments": dict(args),
        "children": [_profile_tree(child) for child in children],
    }


def _usage_dict(response: Any, *, stage: str, elapsed_ms: float) -> Dict[str, Any]:
    usage = dict(getattr(response, "usage", None) or {})
    return {
        "stage": stage,
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        "cached_tokens": int(usage.get("cached_tokens", 0) or 0),
        "elapsed_ms": round(elapsed_ms, 1),
    }


async def _decision_call(backend: Any, *, stage: str, system: str, user: str,
                         max_tokens: int) -> tuple[str, Dict[str, Any]]:
    t0 = time.perf_counter()
    event = {"stage": stage, "attempted": True, "completed": False,
             "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
    try:
        with span(f"agent.stage.{stage}", stage=stage):
            response = await backend.acomplete(
                system=system, user=user, temperature=0.0, max_tokens=max_tokens,
                response_format={"type": "json_object"}, task_hint=stage,
            )
    except Exception as exc:
        event["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        event["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        raise StageCallError(exc, event) from exc
    elapsed_ms = (time.perf_counter() - t0) * 1000
    event.update(_usage_dict(response, stage=stage, elapsed_ms=elapsed_ms))
    event["completed"] = True
    return str(response.text or ""), event


def _execute_profile(driver: Any, *, database: str, cypher: str,
                     params: Dict[str, Any], row_cap: int,
                     exact_completeness: bool) -> Dict[str, Any]:
    """Execute one read and return both agent evidence and side-channel Bolt metrics."""
    effective_params = dict(params)
    effective_params["limit"] = row_cap + 1 if exact_completeness else row_cap
    take = row_cap + 1 if exact_completeness else row_cap
    t0 = time.perf_counter()
    with span("agent.stage.bolt", database=database,
              query_fingerprint=_query_fingerprint(cypher, effective_params)):
        with driver.session(database=database) as session:
            t_submit = time.perf_counter()
            result = session.run("PROFILE " + cypher, **effective_params)
            t_available = time.perf_counter()
            fetched = [dict(row) for _, row in zip(range(take), result)]
            t_hydrated = time.perf_counter()
            summary = result.consume()
            profile_tree = _profile_tree(getattr(summary, "profile", None))
    elapsed_ms = (time.perf_counter() - t0) * 1000
    has_more: Optional[bool]
    if exact_completeness:
        has_more = len(fetched) > row_cap
        rows = fetched[:row_cap]
        completeness = "partial" if has_more else "complete"
    else:
        rows = fetched[:row_cap]
        # Exactly row_cap rows is ambiguous without cap+1.  This is the legacy contract defect.
        has_more = None
        completeness = "complete" if len(rows) < row_cap else "unknown"
    fingerprint = _query_fingerprint(cypher, effective_params)
    evidence = {
        "rows": rows,
        "returned_count": len(rows),
        "row_cap": row_cap,
    }
    if exact_completeness:
        evidence.update({
            "columns": list(rows[0]) if rows else [],
            "completeness": completeness,
            "has_more": has_more,
            "next_cursor": (f"{fingerprint}:{row_cap}" if has_more else None),
            "evidence_id": f"ev-{fingerprint}",
            "query_fingerprint": fingerprint,
            "scope_applied": True,
            "anchor_bound": ("a" in effective_params),
            "outcome": "ok",
        })
    metrics = {
        "query_fingerprint": fingerprint,
        "params": effective_params,
        "rows": len(rows),
        "db_hits": _profile_db_hits(getattr(summary, "profile", None)),
        "profile_tree": profile_tree,
        "elapsed_ms": round(elapsed_ms, 3),
        "submit_ms": round((t_available - t_submit) * 1000, 3),
        "hydrate_ms": round((t_hydrated - t_available) * 1000, 3),
        "server_available_ms": float(summary.result_available_after or 0),
        "server_consumed_ms": float(summary.result_consumed_after or 0),
        "result_bytes": len(_stable_json(evidence)),
        "completeness": completeness,
    }
    return {"evidence": evidence, "metrics": metrics,
            "params": effective_params, "rows": rows}


def _gold(driver: Any, *, database: str, question: Dict[str, Any],
          params: Dict[str, Any]) -> List[Dict[str, Any]]:
    with driver.session(database=database) as session:
        result = session.run(question["ref"], **params)
        rows = [dict(row) for row in result]
        result.consume()
    return rows


def _anchor(driver: Any, database: str) -> int:
    with driver.session(database=database) as session:
        p99 = session.run(
            "MATCH (a:Account) RETURN percentileDisc(a._out_degree,0.99) AS p"
        ).single()["p"]
        return int(session.run(
            "MATCH (a:Account) WHERE a._out_degree >= $p "
            "RETURN min(a.acct_no) AS a", p=p99).single()["a"])


def _handoff_payload(arm: str, *, question_text: str, planner_raw: str,
                     intent: Dict[str, Any]) -> str:
    if arm in ("staged_single", "multi_full"):
        # Byte-identical visible treatment. On a stateless endpoint, agent object count is
        # not a causal intervention when prompt/context are otherwise the same.
        return (f"Full workflow transcript:\nUSER QUESTION:\n{question_text}\n\n"
                f"PLANNER RESPONSE:\n{planner_raw}\n")
    return ("Typed QueryIntent handoff:\n" + _stable_json({
        "question": question_text,
        "intent": intent,
    }))


def _verifier_payload(arm: str, *, question_text: str, planner_raw: str,
                      intent: Dict[str, Any], cypher: str,
                      evidence: Dict[str, Any]) -> str:
    if arm in ("staged_single", "multi_full"):
        return (f"FULL TRANSCRIPT\nQuestion: {question_text}\n"
                f"Planner: {planner_raw}\nCypher: {cypher}\n"
                f"Tool result: {_stable_json(evidence)}")
    packet_name = "ResultEnvelope" if arm == "multi_envelope" else "EvidencePacket"
    return _stable_json({
        "question": question_text,
        "query_intent": intent,
        "cypher": cypher,
        packet_name: evidence,
    })


async def _run_case(*, arm: str, question: Dict[str, Any], database: str, sf: int,
                    run_id: str, repeat: int, anchor: int, gold_rows: List[Dict[str, Any]],
                    driver: Any, schema: Dict[str, Any], policy: Any,
                    planner_backend: Any, executor_backend: Any, verifier_backend: Any,
                    model_name: str, row_cap: int, decision_tokens: int,
                    executor_tokens: int,
                    verifier_mode: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    params = _request_params(question, anchor=anchor, row_cap=row_cap)
    question_text = question["question"].format(**params)
    episode_id = f"{run_id}:sf{sf}:{database}:{question['id']}:r{repeat}:{arm}"
    t_episode = time.perf_counter()
    stages: List[Dict[str, Any]] = []
    executions: List[Dict[str, Any]] = []
    decisions: Dict[str, Any] = {}
    conversation: Dict[str, Any] = {"episode_id": episode_id, "stages": []}

    planner_raw = ""
    intent: Dict[str, Any] = {}
    if arm != "direct_single":
        try:
            planner_raw, event = await _decision_call(
                planner_backend, stage="planner", system=PLANNER_SYSTEM,
                user=question_text, max_tokens=decision_tokens)
        except StageCallError as exc:
            stages.append(exc.event)
            conversation["stages"].append({"role": "planner", "system": PLANNER_SYSTEM,
                                            "user": question_text,
                                            "error": exc.event["error"]})
            raise CaseRunError(exc.cause, stages=stages, conversation=conversation,
                               decisions=decisions) from exc
        stages.append(event)
        intent = _json_object(planner_raw) or {"parse_error": True, "raw": planner_raw}
        decisions["query_intent"] = intent
        conversation["stages"].append({"role": "planner", "system": PLANNER_SYSTEM,
                                        "user": question_text, "response": planner_raw})

    executor_question = question_text
    if arm != "direct_single":
        executor_question += "\n\n" + _handoff_payload(
            arm, question_text=question_text, planner_raw=planner_raw, intent=intent)
    executor_usage: List[Dict[str, Any]] = []
    original_acomplete = executor_backend.acomplete

    async def tracked_executor(**kwargs: Any) -> Any:
        t0 = time.perf_counter()
        event = {"stage": "executor", "attempted": True, "completed": False,
                 "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
        executor_usage.append(event)
        try:
            with span("agent.stage.executor_model", stage="executor"):
                response = await original_acomplete(**kwargs)
        except Exception as exc:
            event["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            event["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            raise
        event.update(_usage_dict(
            response, stage="executor", elapsed_ms=(time.perf_counter() - t0) * 1000))
        event["completed"] = True
        return response

    executor_backend.acomplete = tracked_executor  # type: ignore[method-assign]

    async def explain(cypher: str, bound: Dict[str, Any]) -> None:
        def run() -> None:
            with driver.session(database=database) as session:
                session.run("EXPLAIN " + cypher, **bound).consume()
        await asyncio.to_thread(run)

    from seocho.query.text2cypher import generate_validated_cypher
    try:
        generated = await generate_validated_cypher(
            question=executor_question, schema=schema, params=params, policy=policy,
            backend=executor_backend, model=model_name, explain=explain,
        )
    except Exception as exc:
        stages.extend(executor_usage)
        conversation["stages"].append({"role": "executor", "user": executor_question,
                                        "error": f"{type(exc).__name__}: {str(exc)[:500]}"})
        decisions["executor_failed"] = True
        raise CaseRunError(exc, stages=stages, conversation=conversation,
                           decisions=decisions,
                           handoff_chars=max(len(executor_question) - len(question_text), 0)) from exc
    finally:
        executor_backend.acomplete = original_acomplete  # type: ignore[method-assign]
    stages.extend(executor_usage)
    cypher = generated.cypher
    conversation["stages"].append({"role": "executor", "user": executor_question,
                                    "response": cypher, "attempts": generated.attempts})
    decisions["initial_cypher"] = cypher
    decisions["executor_attempts"] = generated.attempts
    exact = arm == "multi_envelope"
    execution = await asyncio.to_thread(
        _execute_profile, driver, database=database, cypher=cypher,
        params=dict(generated.params), row_cap=row_cap, exact_completeness=exact)
    executions.append({**execution["metrics"], "cypher": cypher, "phase": "initial"})
    final_execution = execution
    initial_scored = interaction.score(question, gold_rows, execution["rows"])

    verifier_raw = ""
    verifier: Dict[str, Any] = {"pass": True, "reason_codes": [], "revision": ""}
    repair_elapsed_ms = 0.0
    if arm != "direct_single":
        verifier_user = _verifier_payload(
            arm, question_text=question_text, planner_raw=planner_raw, intent=intent,
            cypher=cypher, evidence=execution["evidence"])
        try:
            verifier_raw, event = await _decision_call(
                verifier_backend, stage="verifier", system=VERIFIER_SYSTEM,
                user=verifier_user, max_tokens=decision_tokens)
            stages.append(event)
            verifier = _json_object(verifier_raw) or {
                "pass": False, "reason_codes": ["unparseable_verifier"], "revision": ""
            }
        except StageCallError as exc:
            stages.append(exc.event)
            verifier = {"pass": None, "reason_codes": ["verifier_call_failed"],
                        "revision": "", "error": exc.event["error"]}
        decisions["verify_decision"] = verifier
        conversation["stages"].append({"role": "verifier", "system": VERIFIER_SYSTEM,
                                        "user": verifier_user, "response": verifier_raw})

        revision = str(verifier.get("revision") or "").strip()
        decisions["repair_authority"] = verifier_mode
        decisions["repair_requested"] = bool(verifier.get("pass") is False and revision)
        if verifier.get("pass") is False and revision and verifier_mode == "auto":
            repair_started = time.perf_counter()
            repair_question = (executor_question + "\n\nVerifier correction (apply it exactly):\n"
                               + revision)
            repair_usage: List[Dict[str, Any]] = []

            async def tracked_repair(**kwargs: Any) -> Any:
                t0 = time.perf_counter()
                event = {"stage": "repair_executor", "attempted": True,
                         "completed": False, "prompt_tokens": 0,
                         "completion_tokens": 0, "cached_tokens": 0}
                repair_usage.append(event)
                try:
                    with span("agent.stage.repair_model", stage="repair_executor"):
                        response = await original_acomplete(**kwargs)
                except Exception as exc:
                    event["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                    event["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                    raise
                event.update(_usage_dict(
                    response, stage="repair_executor",
                    elapsed_ms=(time.perf_counter() - t0) * 1000))
                event["completed"] = True
                return response

            executor_backend.acomplete = tracked_repair  # type: ignore[method-assign]
            try:
                with span("agent.repair_loop", question_id=question["id"], arm=arm,
                          verifier_mode=verifier_mode):
                    try:
                        repaired = await generate_validated_cypher(
                            question=repair_question, schema=schema, params=params, policy=policy,
                            backend=executor_backend, model=model_name, explain=explain,
                        )
                    except Exception as exc:
                        decisions["repair_applied"] = False
                        decisions["repair_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                        conversation["stages"].append({
                            "role": "repair_executor", "user": repair_question,
                            "response": "", "error": decisions["repair_error"],
                        })
                        repaired = None
            finally:
                executor_backend.acomplete = original_acomplete  # type: ignore[method-assign]
            stages.extend(repair_usage)
            if repaired is not None:
                repaired_execution = await asyncio.to_thread(
                    _execute_profile, driver, database=database, cypher=repaired.cypher,
                    params=dict(repaired.params), row_cap=row_cap,
                    exact_completeness=exact)
                executions.append({**repaired_execution["metrics"], "cypher": repaired.cypher,
                                   "phase": "verifier_repair"})
                final_execution = repaired_execution
                decisions["repaired_cypher"] = repaired.cypher
                decisions["repair_attempts"] = repaired.attempts
                conversation["stages"].append({"role": "repair_executor",
                                                "user": repair_question,
                                                "response": repaired.cypher,
                                                "attempts": repaired.attempts})
                decisions["repair_applied"] = True
            repair_elapsed_ms = (time.perf_counter() - repair_started) * 1000
        elif verifier.get("pass") is False and revision:
            # An LLM critique is evidence, not authority. The paid gate found a correct query
            # that the verifier tried to replace with an invalid literal-bound variant.
            decisions["repair_applied"] = False
            decisions["repair_blocked_reason"] = "verifier_advisory_only"

    scored = interaction.score(question, gold_rows, final_execution["rows"])
    unique_queries = len({e["query_fingerprint"] for e in executions})
    planner_to_executor = executor_question[len(question_text):]
    executor_to_verifier = verifier_user if arm != "direct_single" else ""
    rec = {
        "episode_id": episode_id,
        "run_id": run_id,
        "arm": arm,
        "factors": {**ARM_FACTORS[arm], "orchestrator": "procedural",
                    "verifier_authority": verifier_mode,
                    "llm_runtime": "seocho_backend"},
        "sf": sf,
        "database": database,
        "question_id": question["id"],
        "audience": question["audience"],
        "difficulty": question["difficulty"],
        "request": _request_metadata(question),
        "repeat": repeat,
        "anchor": anchor,
        "correct": bool(scored["correct"]),
        "score": scored,
        "wall_ms": round((time.perf_counter() - t_episode) * 1000, 1),
        "model_calls": len(stages),
        "model_calls_completed": sum(bool(s.get("completed", True)) for s in stages),
        "prompt_tokens": sum(s["prompt_tokens"] for s in stages),
        "completion_tokens": sum(s["completion_tokens"] for s in stages),
        "cached_tokens": sum(s["cached_tokens"] for s in stages),
        "handoff_chars": len(planner_to_executor) + len(executor_to_verifier),
        "handoff_chars_by_edge": {"planner_to_executor": len(planner_to_executor),
                                  "executor_to_verifier": len(executor_to_verifier)},
        "graph_trips": len(executions),
        "unique_graph_queries": unique_queries,
        "redundant_graph_queries": len(executions) - unique_queries,
        "db_hits": sum(e["db_hits"] for e in executions),
        "db_ms": round(sum(e["elapsed_ms"] for e in executions), 3),
        "rows_into_context": sum(e["rows"] for e in executions),
        "result_bytes": sum(e["result_bytes"] for e in executions),
        "verifier_pass": verifier.get("pass") if arm != "direct_single" else None,
        "verifier_reason_codes": verifier.get("reason_codes", []),
        "repair_loop": _repair_ledger(
            stages=stages, executions=executions,
            initial_correct=bool(initial_scored["correct"]),
            final_correct=bool(scored["correct"]),
            verifier_requested=bool(decisions.get("repair_requested")),
            verifier_mode=verifier_mode,
            repair_applied=bool(decisions.get("repair_applied")),
            repair_elapsed_ms=repair_elapsed_ms),
        "decisions": decisions,
        "stages": stages,
        "executions": executions,
    }
    return rec, conversation


def _aggregate(rows: Iterable[Dict[str, Any]], arm: str) -> Dict[str, Any]:
    selected = [r for r in rows if r["arm"] == arm]
    if not selected:
        return {"n": 0}

    def med(key: str) -> float:
        values = [r[key] for r in selected if key in r]
        return round(float(statistics.median(values)), 2) if values else 0.0

    return {
        "n": len(selected),
        "errors": sum(1 for r in selected if r.get("error")),
        "correct": sum(1 for r in selected if r["correct"]),
        "correct_rate": round(sum(1 for r in selected if r["correct"]) / len(selected), 4),
        "model_calls": sum(r.get("model_calls", 0) for r in selected),
        "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in selected),
        "completion_tokens": sum(r.get("completion_tokens", 0) for r in selected),
        "handoff_chars": sum(r.get("handoff_chars", 0) for r in selected),
        "graph_trips": sum(r.get("graph_trips", 0) for r in selected),
        "db_hits": sum(r.get("db_hits", 0) for r in selected),
        "wall_ms_median": med("wall_ms"),
        "db_ms_median": med("db_ms"),
    }


def _trace_receipt(rows: List[Dict[str, Any]], spans_path: Path,
                   tempo_url: str) -> Dict[str, Any]:
    """Prove both the local append-only artifact and Tempo can resolve each trace."""
    local_counts: Dict[str, int] = {}
    if spans_path.exists():
        for line in spans_path.read_text().splitlines():
            if line.strip():
                trace_id = str(json.loads(line).get("trace_id") or "")
                local_counts[trace_id] = local_counts.get(trace_id, 0) + 1
    trace_ids = sorted({str(r.get("trace_id")) for r in rows if r.get("trace_id")})
    remote: Dict[str, Any] = {}
    for trace_id in trace_ids:
        url = f"{tempo_url.rstrip('/')}/api/traces/{trace_id}"
        found = False
        detail = ""
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=10) as response:
                    payload = json.loads(response.read())
                batches = payload.get("batches") or []
                found = bool(batches)
                detail = f"batches={len(batches)}"
                if found:
                    break
            except (OSError, ValueError, urllib.error.URLError) as exc:
                detail = f"{type(exc).__name__}: {str(exc)[:200]}"
            if attempt < 2:
                time.sleep(2)
        remote[trace_id] = {"found": found, "detail": detail}
    return {
        "schema_version": "seocho.trace-receipt.v1",
        "trace_ids": trace_ids,
        "local_jsonl": str(spans_path),
        "local_span_counts": local_counts,
        "local_complete": bool(trace_ids) and all(local_counts.get(t, 0) > 0 for t in trace_ids),
        "tempo_url": tempo_url,
        "tempo": remote,
        "tempo_complete": bool(trace_ids) and all(v["found"] for v in remote.values()),
    }


def _load_questions(question_suite: Optional[str], selected_ids: set[str]) -> List[Dict[str, Any]]:
    """Load either the legacy diagnostic set or declarative user-request workload."""
    if question_suite is None:
        available = interaction.QUESTIONS
    else:
        payload = yaml.safe_load(Path(question_suite).read_text()) or {}
        available = payload.get("questions") or []
        if not isinstance(available, list):
            raise SystemExit("question suite must contain a questions list")
    questions = [dict(q) for q in available if q.get("id") in selected_ids]
    ids = {str(q.get("id")) for q in questions}
    missing = selected_ids - ids
    if missing:
        raise SystemExit(f"unknown question ids: {sorted(missing)}")
    for question in questions:
        required = ("id", "audience", "difficulty", "question", "shape", "ref")
        absent = [key for key in required if key not in question]
        if absent:
            raise SystemExit(f"question {question.get('id')} missing {absent}")
        if question["shape"] == "scalar" and not question.get("keys"):
            raise SystemExit(f"scalar question {question['id']} needs keys")
        if question["shape"] == "list" and not question.get("column"):
            raise SystemExit(f"list question {question['id']} needs column")
    return questions


async def main_async(args: Any) -> None:
    _ensure_seocho_on_path(args.seocho_src)
    from seocho.ontology import Ontology
    from seocho.query.hybrid_planner import policy_from_ontology, schema_for_prompt

    ontology = Ontology.from_dict(yaml.safe_load(Path(args.ontology).read_text()))
    policy = policy_from_ontology(ontology)
    schema = schema_for_prompt(ontology, policy)
    cfg = model_config(args, max_tokens=args.executor_tokens)
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))

    if args.question_suite and not args.only:
        suite_payload = yaml.safe_load(Path(args.question_suite).read_text()) or {}
        selected_ids = {str(q.get("id")) for q in suite_payload.get("questions") or []}
    else:
        selected_ids = set(args.only or DEFAULT_DIAGNOSTIC)
    questions = _load_questions(args.question_suite, selected_ids)
    targets = []
    for value in args.databases:
        database, _, sf = value.partition(":")
        targets.append((database, int(sf or 1)))

    prepared: Dict[tuple[str, str], Dict[str, Any]] = {}
    for database, sf in targets:
        anchor = _anchor(driver, database)
        for question in questions:
            gold_rows = _gold(driver, database=database, question=question,
                              params=_request_params(question, anchor=anchor,
                                                     row_cap=args.row_cap))
            if not gold_rows:
                raise SystemExit(f"blind gold: {database}/{question['id']} returned no rows")
            prepared[(database, question["id"])] = {
                "sf": sf, "anchor": anchor, "gold_rows": gold_rows,
            }
        print(f"[gold] {database} SF{sf} anchor={anchor} questions={len(questions)}",
              flush=True)

    if args.validate_only:
        driver.close()
        print(f"validation OK: {len(prepared)} cells")
        return

    # Credential resolution is a pre-paid gate.  Do it before any run artifact or monitor is
    # opened so a missing hosted key cannot leave a manifest that resembles an interrupted run.
    cfg.client_kwargs()

    arms = list(args.arms)
    unknown = set(arms) - set(ARMS)
    if unknown:
        raise SystemExit(f"unknown arms: {sorted(unknown)}")
    output_dir = Path(args.output_dir) / args.run_id
    sample_path = Path(args.samples) if args.samples else output_dir / "samples.jsonl"
    conversation_path = (Path(args.conversations) if args.conversations
                         else output_dir / "conversations.jsonl")
    out_path = Path(args.out) if args.out else output_dir / "report.json"
    manifest_path = (Path(args.manifest_out) if args.manifest_out
                     else output_dir / "manifest.json")
    occupied = [p for p in (sample_path, conversation_path, out_path, manifest_path)
                if p.exists()]
    if occupied and not args.resume:
        raise SystemExit("run outputs already exist; choose a new --run-id or pass --resume: "
                         + ", ".join(str(p) for p in occupied))

    # Capture repository state before creating any run artifact; otherwise the run's own
    # output directory makes every otherwise-clean manifest report git_dirty=true.
    manifest = runmeta.manifest(
        db_container=args.db_container,
        experiment="agent-context-topology",
        schema_version="seocho.finbench.agent-topology.v1",
    )
    for path in (sample_path, conversation_path, out_path, manifest_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", args.otlp_endpoint)
    os.environ["TRACE_JSONL_PATH"] = str(output_dir / "spans.jsonl")
    init_tracing("aisummit26-agent-topology")
    observability = enable_observability(
        backend="otlp", endpoint=args.otlp_endpoint,
        service_name="seocho-agent-topology", source=args.seocho_src,
        enable_metrics=False)
    system_metrics_path = output_dir / "system_metrics.jsonl"
    system_sampler = None
    system_monitor_preflight = None
    if not args.no_system_metrics:
        system_sampler = SystemMetricsSampler(
            system_metrics_path, interval_s=args.system_metrics_interval_s,
            db_container=args.db_container)
        system_monitor_preflight = system_sampler.probe()

    run_header = {
        "schema_version": "seocho.finbench.agent-topology.v1",
        "run_id": args.run_id,
        "manifest": manifest,
        "endpoint": cfg.descriptor(),
        "observability": observability,
        "system_monitor": {
            "enabled": system_sampler is not None,
            "required": system_sampler is not None,
            "path": str(system_metrics_path) if system_sampler is not None else None,
            "interval_s": args.system_metrics_interval_s,
            "scope": "local client host and database container; hosted model server excluded",
            "preflight": system_monitor_preflight,
        },
        "config": {k: v for k, v in vars(args).items() if k != "password"},
        "graph": {f"{db}:sf{sf}": {"anchor": prepared[(db, questions[0]['id'])]["anchor"]}
                  for db, sf in targets},
    }
    if not manifest_path.exists():
        manifest_path.write_text(json.dumps(run_header, indent=2, default=str) + "\n")
    print(f"[endpoint] {cfg.provider} {cfg.model_name} @ {cfg.base_url}", flush=True)
    print(f"[manifest] {manifest_path}", flush=True)
    if system_sampler is not None and not system_monitor_preflight.get("available", False):
        driver.close()
        shutdown_tracing()
        raise SystemExit(
            "database monitoring preflight failed before model calls: "
            + str(system_monitor_preflight.get("reason", "container unavailable")))
    if system_sampler is not None:
        system_sampler.start()

    completed: set[str] = set()
    rows_out: List[Dict[str, Any]] = []
    if args.resume and sample_path.exists():
        for line in sample_path.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            rows_out.append(rec)
            completed.add(rec["episode_id"])
        print(f"[resume] {len(completed)} episodes", flush=True)

    planner_backend = make_llm_backend(cfg)
    executor_backend = make_llm_backend(cfg)
    verifier_backend = make_llm_backend(cfg)
    rng = random.Random(args.seed)
    abort_requested = False
    abort_reason = None
    last_episode_ended_mono = None

    with sample_path.open("a") as samples, conversation_path.open("a") as conversations:
        for database, sf in targets:
            if abort_requested:
                break
            for question in questions:
                if abort_requested:
                    break
                prep = prepared[(database, question["id"])]
                for repeat in range(args.repeats):
                    if abort_requested:
                        break
                    block_arms = list(arms)
                    rng.shuffle(block_arms)
                    for arm in block_arms:
                        if abort_requested:
                            break
                        episode_id = (f"{args.run_id}:sf{sf}:{database}:"
                                      f"{question['id']}:r{repeat}:{arm}")
                        if episode_id in completed:
                            continue
                        if last_episode_ended_mono is not None and args.episode_delay_s > 0:
                            remaining = (args.episode_delay_s -
                                         (time.monotonic() - last_episode_ended_mono))
                            if remaining > 0:
                                print(f"  [pace] waiting {remaining:.1f}s before {episode_id}",
                                      flush=True)
                                await asyncio.sleep(remaining)
                        episode_started_wall = time.time()
                        episode_started_mono = time.monotonic()
                        episode_span = None
                        try:
                            with span("agent.topology.episode", run_id=args.run_id,
                                      episode_id=episode_id, arm=arm,
                                      question_id=question["id"], sf=sf) as episode_span:
                                rec, conversation = await _run_case(
                                    arm=arm, question=question, database=database, sf=sf,
                                    run_id=args.run_id, repeat=repeat,
                                    anchor=prep["anchor"], gold_rows=prep["gold_rows"],
                                    driver=driver, schema=schema, policy=policy,
                                    planner_backend=planner_backend,
                                    executor_backend=executor_backend,
                                    verifier_backend=verifier_backend,
                                    model_name=cfg.model_name, row_cap=args.row_cap,
                                    decision_tokens=args.decision_tokens,
                                    executor_tokens=args.executor_tokens,
                                    verifier_mode=args.verifier_mode,
                                )
                        except Exception as exc:
                            partial_stages = (exc.stages if isinstance(exc, CaseRunError) else [])
                            original_exc = (exc.cause if isinstance(exc, CaseRunError) else exc)
                            rec = {
                                "episode_id": episode_id, "run_id": args.run_id,
                                "arm": arm,
                                "factors": {**ARM_FACTORS[arm],
                                            "orchestrator": "procedural",
                                            "verifier_authority": args.verifier_mode,
                                            "llm_runtime": "seocho_backend"},
                                "sf": sf,
                                "database": database, "question_id": question["id"],
                                "audience": question["audience"],
                                "difficulty": question["difficulty"], "repeat": repeat,
                                "anchor": prep["anchor"], "correct": False,
                                "model_calls": len(partial_stages),
                                "model_calls_completed": sum(
                                    bool(s.get("completed", True)) for s in partial_stages),
                                "prompt_tokens": sum(s.get("prompt_tokens", 0)
                                                     for s in partial_stages),
                                "completion_tokens": sum(s.get("completion_tokens", 0)
                                                         for s in partial_stages),
                                "cached_tokens": sum(s.get("cached_tokens", 0)
                                                     for s in partial_stages),
                                "handoff_chars": (exc.handoff_chars
                                                  if isinstance(exc, CaseRunError) else 0),
                                "graph_trips": 0,
                                "unique_graph_queries": 0, "redundant_graph_queries": 0,
                                "db_hits": 0, "db_ms": 0.0, "rows_into_context": 0,
                                "result_bytes": 0,
                                "decisions": (exc.decisions
                                              if isinstance(exc, CaseRunError) else {}),
                                "stages": partial_stages, "executions": [],
                                "error": (f"{type(original_exc).__name__}: "
                                          f"{str(original_exc)[:500]}"),
                            }
                            conversation = (exc.conversation if isinstance(exc, CaseRunError)
                                            else {"episode_id": episode_id})
                            conversation["error"] = rec["error"]
                        if episode_span is not None:
                            context = episode_span.get_span_context()
                            trace_id = f"{context.trace_id:032x}"
                            span_id = f"{context.span_id:016x}"
                            rec["trace_id"] = trace_id
                            rec["root_span_id"] = span_id
                            conversation["trace_id"] = trace_id
                        rec["monitor_window"] = {
                            "started_wall": episode_started_wall,
                            "ended_wall": time.time(),
                            "started_mono": episode_started_mono,
                            "ended_mono": time.monotonic(),
                        }
                        samples.write(json.dumps(rec, default=str) + "\n")
                        samples.flush()
                        os.fsync(samples.fileno())
                        conversations.write(json.dumps(conversation, default=str) + "\n")
                        conversations.flush()
                        os.fsync(conversations.fileno())
                        rows_out.append(rec)
                        last_episode_ended_mono = time.monotonic()
                        print(f"  {episode_id:48s} correct={rec.get('correct')} "
                              f"calls={rec.get('model_calls', 0)} "
                              f"prompt={rec.get('prompt_tokens', 0)} "
                              f"trips={rec.get('graph_trips', 0)} "
                              f"{rec.get('error', '')[:80]}", flush=True)
                        if (not args.continue_after_rate_limit and
                                str(rec.get("error") or "").startswith("RateLimitError")):
                            abort_requested = True
                            abort_reason = "MARA rate limit; stopped after durable sample"

    system_monitor_receipt = (system_sampler.stop() if system_sampler is not None else {
        "schema_version": "seocho.system-monitor-receipt.v2", "complete": False,
        "db_container_complete": False, "valid": False, "reason": "disabled"})
    driver.close()
    try:
        from seocho.tracing import flush_tracing
        flush_tracing()
    except Exception:
        pass
    shutdown_tracing()
    trace_receipt = _trace_receipt(rows_out, output_dir / "spans.jsonl", args.tempo_url)
    (output_dir / "trace_receipt.json").write_text(
        json.dumps(trace_receipt, indent=2, default=str) + "\n")
    usable = [r for r in rows_out if "model_calls" in r]
    report = {
        **run_header,
        "run_status": "aborted_rate_limit" if abort_requested else "completed",
        "abort_reason": abort_reason,
        "trace_receipt": trace_receipt,
        "system_monitor_receipt": system_monitor_receipt,
        "summary": {arm: _aggregate(usable, arm) for arm in arms},
        "samples": rows_out,
    }
    out_path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print("\n=== summary ===")
    for arm, summary in report["summary"].items():
        print(f"  {arm:16s} {summary}")
    print(f"wrote {out_path}")
    if system_sampler is not None and not system_monitor_receipt["valid"]:
        raise SystemExit(
            "database monitoring gate failed after preserving the report: "
            + json.dumps(system_monitor_receipt, default=str))
    if abort_requested:
        raise SystemExit(abort_reason)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    add_provider_args(parser)
    parser.add_argument("--uri", default="bolt://localhost:7688")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default=os.getenv("NEO4J_PASSWORD"))
    parser.add_argument("--databases", nargs="+", default=["finbenchl1:1"])
    parser.add_argument("--ontology", default="ontology/finbench.ontology.yaml")
    parser.add_argument("--seocho-src", default=None)
    parser.add_argument("--db-container", default="aisummit-simtest")
    parser.add_argument("--arms", nargs="+", default=list(ARMS), choices=ARMS)
    parser.add_argument("--question-suite", default=None,
                        help="declarative user-request YAML; defaults to the diagnostic set")
    parser.add_argument("--only", nargs="+", default=None)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--row-cap", type=int, default=50)
    parser.add_argument("--decision-tokens", type=int, default=500)
    parser.add_argument("--executor-tokens", type=int, default=1000)
    parser.add_argument("--verifier-mode", choices=("advisory", "auto"),
                        default="advisory",
                        help="whether an LLM verifier may trigger a second graph query")
    parser.add_argument("--seed", type=int, default=29)
    parser.add_argument("--run-id", default=None,
                        help="unique immutable run id (default: UTC timestamp)")
    parser.add_argument("--output-dir", default="results/episodes/agent_topology")
    parser.add_argument("--otlp-endpoint", default="http://127.0.0.1:4317")
    parser.add_argument("--tempo-url", default="http://127.0.0.1:3200")
    parser.add_argument("--system-metrics-interval-s", type=float, default=5.0)
    parser.add_argument("--no-system-metrics", action="store_true")
    parser.add_argument(
        "--episode-delay-s", type=float, default=0.0,
        help="minimum idle time between paid episodes; outside the measured episode window")
    parser.add_argument(
        "--continue-after-rate-limit", action="store_true",
        help="keep attempting later cells after a 429 (default aborts after durable receipt)")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--samples", default=None)
    parser.add_argument("--conversations", default=None)
    parser.add_argument("--manifest-out", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    if not args.password:
        parser.error("--password or NEO4J_PASSWORD is required")
    if args.run_id is None:
        args.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
