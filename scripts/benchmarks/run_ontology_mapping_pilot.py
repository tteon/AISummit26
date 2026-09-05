#!/usr/bin/env python3
"""Preregistered bounded ontology A/B with DB-only gates and complete request/return receipts."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from harness.llm import model_config, sync_client


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runmeta = load_module("ontology_pilot_runmeta", ROOT / "scripts/analysis/runmeta.py")
SYSTEM = """Return one JSON object with exactly the key cypher. Generate one read-only Cypher query.
Use the provided physical schema and named parameters. The harness alone supplies all parameter values.
Every new matched node must include {_workspace_id:$workspace_id}; a previously scoped variable may be reused.
Match the anchor inline: Account {acct_no:$acct_no} or Company {id:$company_id}, plus its workspace scope.
Never inline user values, invent properties/relationships, change parameters, use CALL/UNION/LOAD CSV,
or write data. End with LIMIT $limit. Use exactly the requested output column names, units, ordering,
and uniqueness. Follow the bounded hop limit. Produce only the JSON object, without explanations.
"""


def stable(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_dependency(source, archive):
    expected = {}
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            if member.isfile() and member.name.endswith(".py"):
                relative = Path(member.name).relative_to("src")
                expected[str(relative)] = hashlib.sha256(tar.extractfile(member).read()).hexdigest()
    actual = {str(p.relative_to(source)): sha(p) for p in (source / "seocho").rglob("*.py")}
    if not expected or actual != expected:
        raise ValueError("SEOCHO runtime source differs from its archived receipt")
    return hashlib.sha256(stable(actual).encode()).hexdigest()


def write(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w") as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def append(path, value):
    with path.open("a") as stream:
        stream.write(stable(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_lines(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def physical_context(ontology):
    # Descriptions and FIBO mapping appear only in the treatment; physical endpoints are shared.
    return {
        "nodes": {name: {**{k: v["type"] for k, v in spec.get("properties", {}).items()},
                          "_workspace_id": "STRING"} for name, spec in ontology["nodes"].items()},
        "relationships": {name: {"source": spec["source"], "target": spec["target"],
                                  "properties": {k: v["type"] for k, v in spec.get("properties", {}).items()}}
                          for name, spec in ontology["relationships"].items()},
    }


def messages(case, ontology, mapping, arm, hops):
    user = {"question": case["question"], "physical_schema": physical_context(ontology),
            "parameters": case["params"], "result_contract": case["result"], "max_hops": hops}
    if arm == "business_mapping":
        user["business_semantics"] = {"terms": mapping["terms"], "version": mapping["version"],
            "scope": "Explicit partial FinBench mappings and local/derived concepts; not legal proof or complete FIBO conformance."}
    elif arm != "physical_schema":
        raise ValueError("unknown arm")
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": stable(user)}]


def validate_parameters(specs, values):
    errors = []
    for name, spec in specs.items():
        value = values.get(name)
        kind = spec["type"]
        valid_type = ((kind == "integer" and type(value) is int) or
                      (kind == "number" and type(value) in (int, float) and math.isfinite(value)) or
                      (kind == "string" and isinstance(value, str)))
        if not valid_type:
            errors.append(f"{name}: invalid/missing {kind}")
            continue
        for key, invalid in (("minimum", lambda a, b: a < b), ("maximum", lambda a, b: a > b),
                             ("exclusive_minimum", lambda a, b: a <= b)):
            if key in spec and invalid(value, spec[key]):
                errors.append(f"{name}: {key} violated")
        if "pattern" in spec and not re.fullmatch(spec["pattern"], value):
            errors.append(f"{name}: pattern violated")
    return errors


def scope_gate(cypher, params):
    errors = []
    if re.search(r"\b(CALL|UNION|LOAD|CREATE|MERGE|DELETE|SET|REMOVE|DROP|FOREACH)\b|;|//|/\*", cypher, re.I):
        errors.append("unsupported_query_surface")
    if not re.search(r"\bLIMIT\s+\$limit\s*$", cypher, re.I):
        errors.append("harness_limit_required")
    anchor_key = "company_id" if "company_id" in params else "acct_no"
    property_name = "id" if anchor_key == "company_id" else "acct_no"
    if not re.search(rf"\b{property_name}\s*:\s*\${anchor_key}\b", cypher):
        errors.append("harness_anchor_required")
    if set(re.findall(r"\$([A-Za-z_]\w*)", cypher)) - set(params):
        errors.append("unbound_parameters")
    scoped = set()
    matches = list(re.finditer(r"\bMATCH\b(.*?)(?=\b(?:WHERE|WITH|RETURN|MATCH|OPTIONAL|UNWIND|ORDER|LIMIT)\b|$)", cypher, re.I | re.S))
    if not matches:
        errors.append("matched_scope_required")
    for clause in matches:
        for node in re.findall(r"\(([^()]*)\)", clause.group(1)):
            var = re.match(r"\s*([A-Za-z_]\w*)", node)
            name = var.group(1) if var else None
            if re.search(r"\b_workspace_id\s*:\s*\$workspace_id\b", node):
                if name:
                    scoped.add(name)
            elif not name or name not in scoped:
                errors.append("unscoped_node")
    return sorted(set(errors))


def score_rows(rows, gold, columns, spec):
    if set(columns) != set(spec["columns"]) or len(rows) != len(gold):
        return False
    for row, expected in zip(rows, gold):
        if set(row) != set(expected):
            return False
        for key, want in expected.items():
            got = row[key]
            if want is None:
                if got is not None:
                    return False
            elif spec["columns"][key] == "integer":
                if type(got) is not int or got != want:
                    return False
            elif isinstance(want, (int, float)) and type(got) in (int, float):
                if not math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-6):
                    return False
            elif type(got) is not type(want) or got != want:
                return False
    return True


def plan_dict(plan):
    if plan is None:
        return None
    if isinstance(plan, dict):
        return {k: v for k, v in plan.items() if k != "children"} | {
            "children": [plan_dict(p) for p in plan.get("children", [])]}
    return {"operator_type": plan.operator_type, "arguments": plan.arguments,
            "db_hits": getattr(plan, "db_hits", None),
            "children": [plan_dict(p) for p in plan.children]}


def flatten(plan):
    if not plan:
        return []
    return [plan] + [p for child in plan.get("children", []) for p in flatten(child)]


def execute(driver, graph, query, params, profile=True):
    from neo4j import Query, READ_ACCESS
    started = time.perf_counter()
    with driver.session(database=graph["database"], default_access_mode=READ_ACCESS) as session:
        result = session.run(Query(("PROFILE " if profile else "EXPLAIN ") + query,
                                   timeout=graph["query_timeout_s"]), params)
        columns = list(result.keys())
        rows = [r.data() for r in result]
        summary = result.consume()
        plan = plan_dict(summary.profile if profile else summary.plan)
        operators = flatten(plan)
        hits = sum(int(p.get("dbHits", p.get("db_hits", 0)) or p.get("args", p.get("arguments", {})).get("DbHits", 0)) for p in operators) if profile else None
        return {"cypher": query, "params": params, "columns": columns, "rows": rows,
                "row_count": len(rows), "db_hits": hits, "elapsed_ms": round((time.perf_counter()-started)*1000, 3),
                "query_type": summary.query_type, "plan": plan,
                "server_available_ms": summary.result_available_after,
                "server_consumed_ms": summary.result_consumed_after}


def plan_gate(receipt, maximum):
    if receipt["query_type"] != "r":
        return ["non_read_plan"]
    leaves = [p for p in flatten(receipt["plan"]) if not p.get("children")]
    if not leaves:
        return ["plan_missing"]
    estimates = [p.get("args", p.get("arguments", {})).get("EstimatedRows") for p in leaves]
    if any(x is None for x in estimates):
        return ["leaf_estimate_missing"]
    if any(float(x) > maximum for x in estimates):
        return ["leaf_estimate_over_budget"]
    return []


def db_password(container):
    if os.getenv("NEO4J_PASSWORD"):
        return os.getenv("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]
    # Read the configured local database credential in memory, never in argv or receipts.
    doc = json.loads(subprocess.check_output(["docker", "inspect", container], text=True))[0]
    auth = next((v.split("=", 1)[1] for v in doc["Config"]["Env"] if v.startswith("NEO4J_AUTH=")), None)
    if not auth or "/" not in auth:
        raise ValueError("Set NEO4J_USER and NEO4J_PASSWORD for the selected local DB")
    return tuple(auth.split("/", 1))


def prepare(protocol, driver, validator, policy, snapshot):
    graph = protocol["graph"]
    ontology = yaml.safe_load((ROOT / protocol["physical"]).read_text())
    catalog = yaml.safe_load((ROOT / protocol["catalog"]).read_text())
    mapping = yaml.safe_load((ROOT / protocol["mapping"]).read_text())
    mapping_check = load_module("mapping_check", ROOT / "scripts/analysis/validate_business_request_mapping.py")
    validation = mapping_check.validate(ROOT / protocol["mapping"], ROOT / protocol["physical"], ROOT / protocol["projection"])
    if not validation["valid"]:
        raise ValueError("Business mapping failed static validation: " + stable(validation))
    expected = snapshot["counts"]
    counts = {}
    with driver.session(database=graph["database"]) as session:
        for label in ontology["nodes"]:
            counts[label.lower()] = session.run(f"MATCH (n:{label} {{_workspace_id:$ws}}) RETURN count(n) AS n", ws=graph["workspace_id"]).single()["n"]
        for kind in ontology["relationships"]:
            counts[kind.lower()] = session.run(f"MATCH (a {{_workspace_id:$ws}})-[r:{kind}]->(b {{_workspace_id:$ws}}) RETURN count(r) AS n", ws=graph["workspace_id"]).single()["n"]
        if counts != expected:
            raise ValueError("Snapshot counts differ: " + stable({"expected": expected, "actual": counts}))
        anchor_row = session.run("MATCH (a:Account {_workspace_id:$ws,acct_no:$acct_no}) RETURN a._out_degree AS degree", ws=graph["workspace_id"], acct_no=graph["anchor"]).single()
        if not anchor_row or anchor_row["degree"] is None:
            raise ValueError("Missing annotated anchor")
    prepared = []
    for selected in protocol["cases"]:
        request = next(q for q in catalog["requests"] if q["id"] == selected["id"])
        specs = {**request["parameters"], **selected.get("parameter_overrides", {})}
        params = {"workspace_id": graph["workspace_id"], "acct_no": graph["anchor"],
                  "limit": selected.get("row_cap", graph["row_cap"]), **selected["params"]}
        seed_receipt = None
        if selected.get("seed"):
            seed_receipt = execute(driver, graph, selected["seed"], params)
            if len(seed_receipt["rows"]) != 1:
                raise ValueError(f"{request['id']}: deterministic selector has no scenario")
            params.update(seed_receipt["rows"][0])
        errors = validate_parameters(specs, params)
        if errors:
            raise ValueError(f"{request['id']}: {errors}")
        if "company_id" in params:
            params.pop("acct_no")
        anchor_key = "company_id" if "company_id" in params else "acct_no"
        case_policy = replace(policy, required_parameters=("workspace_id", anchor_key), max_result_rows=params["limit"])
        query = request["gold"]
        violations = list(validator(query, params=params, policy=case_policy)) + scope_gate(query, params)
        if violations:
            raise ValueError(f"{request['id']}: Gold rejected by shared guardrail: {violations}")
        label, prop = ("Company", "id") if anchor_key == "company_id" else ("Account", "acct_no")
        anchor_query = f"MATCH (a:{label} {{_workspace_id:$workspace_id,{prop}:${anchor_key}}}) RETURN a.{prop} AS anchor LIMIT $limit"
        probe = execute(driver, graph, anchor_query, params, False)
        operators = flatten(probe["plan"])
        if not any("NodeIndexSeek" in str(p.get("operatorType", p.get("operator_type", ""))) for p in operators):
            raise ValueError(f"{request['id']}: no index seek for anchor")
        if plan_gate(probe, 2):
            raise ValueError(f"{request['id']}: anchor is not an estimated point lookup")
        explanation = execute(driver, graph, query, params, False)
        if plan_gate(explanation, graph["max_leaf_estimated_rows"]):
            raise ValueError(f"{request['id']}: Gold exceeds plan gate")
        gold = execute(driver, graph, query, params)
        if not gold["rows"] or not any(any(v not in (None, 0, False, "") for v in row.values()) for row in gold["rows"]):
            raise ValueError(f"{request['id']}: blind Gold")
        if not score_rows(gold["rows"], gold["rows"], gold["columns"], request["result"]):
            raise ValueError(f"{request['id']}: Gold/result contract mismatch")
        case = {"id": request["id"], "question": request["user_request"].format(**params),
                "params": params, "result": request["result"], "verification": request["verification"],
                "plan_risk": request["plan_risk"], "semantic_terms": request["semantic_terms"],
                "business_case": request["real_world_case"], "domain": request["domain"],
                "seed_receipt": seed_receipt, "index_receipt": probe, "gold_receipt": gold,
                "gold_explain": explanation, "parameter_specs": specs}
        for arm in protocol["arms"]:
            size = len(stable(messages(case, ontology, mapping, arm, graph["max_graph_hops"])).encode())
            if size > protocol["budget"]["max_prompt_bytes_per_call"]:
                raise ValueError(f"{request['id']}/{arm}: prompt exceeds byte budget ({size})")
        prepared.append(case)
    return {"valid": True, "counts": counts, "anchor": graph["anchor"], "anchor_out_degree": anchor_row["degree"],
            "mapping_validation": validation, "cases": prepared}


def summarize(samples, arms, thresholds):
    result = {}
    for arm in arms:
        rows = [s for s in samples if s["arm"] == arm]
        result[arm] = {"n": len(rows), "correct": sum(s.get("correct") is True for s in rows),
            "invalid": sum(s.get("valid") is not True for s in rows),
            "prompt_tokens": sum(s.get("prompt_tokens") or 0 for s in rows),
            "completion_tokens": sum(s.get("completion_tokens") or 0 for s in rows),
            "db_hits": sum(s.get("db_hits") or 0 for s in rows),
            "usage_missing": sum(s.get("prompt_tokens") is None for s in rows)}
    paired = {}
    for s in samples:
        paired.setdefault((s["question_id"], s["repeat"]), {})[s["arm"]] = s
    pairs = [p for p in paired.values() if set(p) == set(arms) and all(s.get("valid") for s in p.values())]
    wins = sum(not p[arms[0]]["correct"] and p[arms[1]]["correct"] for p in pairs)
    losses = sum(p[arms[0]]["correct"] and not p[arms[1]]["correct"] for p in pairs)
    tokens = [sum((p[a].get("prompt_tokens") or 0)+(p[a].get("completion_tokens") or 0) for p in pairs) for a in arms]
    ratio = tokens[1]/tokens[0] if tokens[0] else None
    conclusion = "inconclusive"
    if len(pairs) == thresholds["paired_outcomes"] and ratio is not None:
        if wins >= thresholds["min_wins"] and losses <= thresholds["max_losses"] and ratio <= thresholds["max_token_ratio"]:
            conclusion = "advance_to_confirmation"
        elif losses > wins or (ratio > thresholds["max_token_ratio"] and wins <= losses):
            conclusion = "reject_configuration"
    return {"arms": result, "complete_valid_pairs": len(pairs), "wins": wins, "losses": losses,
            "token_ratio": ratio, "conclusion": conclusion,
            "scope": "Preregistered exploratory pilot; no significance or general ontology claim."}


def run(args):
    from dotenv import load_dotenv
    from neo4j import GraphDatabase
    load_dotenv(args.env_file, override=False)
    dependency_tree = verify_dependency(args.seocho_src, args.seocho_archive)
    sys.path.insert(0, str(args.seocho_src))
    from seocho.query.workload_compiler import Text2CypherFallbackPolicy
    from seocho.query.text2cypher import validate_text2cypher_fallback as validator
    protocol = yaml.safe_load(args.protocol.read_text())
    ontology = yaml.safe_load((ROOT / protocol["physical"]).read_text())
    mapping = yaml.safe_load((ROOT / protocol["mapping"]).read_text())
    properties = {"_workspace_id", "limit"} | {p for section in ("nodes", "relationships") for n in ontology[section].values() for p in n.get("properties", {})}
    policy = Text2CypherFallbackPolicy(tuple(ontology["nodes"]), tuple(ontology["relationships"]),
        tuple(sorted(properties)), "_workspace_id", max_graph_hops=protocol["graph"]["max_graph_hops"], max_repair_attempts=0)
    cfg = model_config(provider=protocol["model"]["provider"], model_name=protocol["model"]["name"],
        temperature=protocol["model"]["temperature"], max_tokens=protocol["model"]["max_tokens"],
        request_timeout_s=protocol["model"]["request_timeout_s"])
    endpoint = cfg.descriptor() | {"reasoning_effort": protocol["model"]["reasoning_effort"], "sdk_max_retries": 0}
    output = args.output_dir
    sources = {name: sha(ROOT / protocol[name]) for name in ("catalog", "physical", "mapping", "projection")}
    code_files = ["scripts/benchmarks/run_ontology_mapping_pilot.py", "harness/llm.py", "harness/config.py",
                  "harness/environment.py", "scripts/analysis/runmeta.py", "scripts/analysis/validate_business_request_mapping.py"]
    fingerprint = {"protocol": sha(args.protocol), "sources": sources, "endpoint": endpoint,
                   "snapshot": sha(args.snapshot_manifest), "dependency_archive": sha(args.seocho_archive),
                   "dependency_tree": dependency_tree, "code": {p: sha(ROOT / p) for p in code_files}}
    previous = None
    if output.exists():
        if not args.resume:
            raise ValueError("Choose a new output directory or use --resume; existing results are never overwritten")
        previous = json.loads((output / "manifest.json").read_text())
        if previous["fingerprint"] != fingerprint:
            raise ValueError("Resume refused: protocol, dependency, data or endpoint changed")
    manifest = previous or {"schema_version": "finance.ontology-mapping-pilot.v1", "run_id": output.name,
        "manifest": runmeta.manifest(db_container=protocol["graph"]["container"], experiment="ontology-mapping-pilot"),
        "endpoint": endpoint, "fingerprint": fingerprint, "config": protocol,
        "graph": protocol["graph"], "semantic_source": yaml.safe_load((ROOT / protocol["projection"]).read_text())["semantic_source"]}
    if args.execute and manifest["manifest"]["git_dirty"]:
        raise ValueError("Paid run requires a clean source commit")
    if args.execute and subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"], text=True).strip():
        raise ValueError("Current source checkout must be clean for execution or resume")
    output.mkdir(parents=True, exist_ok=True)
    if not previous:
        write(output / "manifest.json", manifest)
        write(output / "endpoint.json", endpoint)
        shutil.copy2(args.protocol, output / "protocol.yaml")
        shutil.copy2(args.snapshot_manifest, output / "snapshot_manifest.json")
        shutil.copy2(args.seocho_archive, output / "seocho_source.tar.gz")
    driver = GraphDatabase.driver(protocol["graph"]["uri"], auth=db_password(protocol["graph"]["container"]), connection_timeout=10)
    client = None
    try:
        preflight = prepare(protocol, driver, validator, policy, json.loads(args.snapshot_manifest.read_text()))
        if previous and (output / "preflight.json").exists():
            original = json.loads((output / "preflight.json").read_text())
            signature = lambda p: [(c["id"], c["params"], c["gold_receipt"]["rows"]) for c in p["cases"]]
            if signature(original) != signature(preflight):
                raise ValueError("Resume refused: bound inputs or Gold changed")
        else:
            write(output / "preflight.json", preflight)
        print(f"[gold] {protocol['graph']['database']} anchor={preflight['anchor']} degree={preflight['anchor_out_degree']} valid_cases={len(preflight['cases'])}", flush=True)
        for c in preflight["cases"]:
            print(f"[case] {c['id']} params={stable(c['params'])} expected_rows={len(c['gold_receipt']['rows'])}", flush=True)
        print(f"[endpoint] {endpoint['provider']} {endpoint['model_name']} @ {endpoint['base_url']} effort={endpoint['reasoning_effort']}", flush=True)
        if not args.execute:
            print("[prepared] DB-only gates passed; no model calls", flush=True)
            return
        samples_path = output / "samples.jsonl"
        samples = read_lines(samples_path)
        completed = {s["episode_id"] for s in samples}
        attempts = read_lines(output / "attempts.jsonl")
        accounted = {s["episode_id"] for s in samples}
        if any(a["episode_id"] not in accounted for a in attempts):
            raise ValueError("An interrupted attempt has no sample; audit it before resuming to avoid duplicate billing")
        expected_episodes = len(preflight["cases"])*len(protocol["arms"])*protocol["repeats"]
        if len(completed) == expected_episodes:
            print(f"[resume] existing={len(completed)} new=0", flush=True)
            return
        client = sync_client(cfg).with_options(max_retries=0)
        available = [m.id for m in client.models.list().data]
        if cfg.model_name not in available:
            raise ValueError("Requested model absent from endpoint model list")
        write(output / "endpoint_models.json", {"checked_at": datetime.now(timezone.utc).isoformat(), "models": available})
        start = time.monotonic()
        prior_seconds = sum(s.get("wall_ms", 0) for s in samples)/1000 + len(samples)*protocol["budget"]["delay_seconds"]
        run_status = "completed"
        stop_reason = None
        budget = protocol["budget"]
        for repeat in range(protocol["repeats"]):
            for case in preflight["cases"]:
                # Reverse A/B order in the second repeat to expose warm-up/order effects.
                order = protocol["arms"] if repeat % 2 == 0 else list(reversed(protocol["arms"]))
                for arm in order:
                    eid = f"{output.name}:{case['id']}:r{repeat}:{arm}"
                    if eid in completed:
                        continue
                    payload = messages(case, ontology, mapping, arm, protocol["graph"]["max_graph_hops"])
                    prompt_bytes = len(stable(payload).encode())
                    if (len(attempts) >= budget["max_model_calls"] or
                        sum(a["prompt_bytes"] for a in attempts)+prompt_bytes > budget["max_total_prompt_bytes"] or
                        sum(s.get("completion_tokens") or 0 for s in samples)+cfg.max_tokens > budget["max_completion_tokens"] or
                        prior_seconds+time.monotonic()-start+cfg.request_timeout_s > budget["max_wall_seconds"]):
                        run_status, stop_reason = "budget_exhausted", "pre-call budget gate"
                        break
                    attempt = {"episode_id": eid, "attempt_index": len(attempts), "prompt_bytes": prompt_bytes,
                               "started_at": datetime.now(timezone.utc).isoformat(), "messages": payload}
                    append(output / "attempts.jsonl", attempt)
                    attempts.append(attempt)
                    sample = {"episode_id": eid, "run_id": output.name, "arm": arm, "question_id": case["id"],
                        "repeat": repeat, "sf": protocol["graph"]["sf"], "database": protocol["graph"]["database"],
                        "anchor": case["params"].get("acct_no", case["params"].get("company_id")),
                        "trace_id": uuid.uuid4().hex, "correct": False, "valid": True, "model_calls": 1,
                        "prompt_tokens": None, "completion_tokens": None, "db_hits": 0, "db_ms": 0,
                        "score": {"gold": case["gold_receipt"]["rows"]}, "executions": []}
                    conv = {"episode_id": eid, "question": case["question"], "messages": payload,
                        "semantic_context": stable(mapping["terms"]) if arm == "business_mapping" else "",
                        "bound_params": case["params"], "expected_output": case["result"]}
                    started = time.perf_counter()
                    try:
                        raw = client.chat.completions.with_raw_response.create(model=cfg.model_name,
                            messages=payload, temperature=cfg.temperature, max_tokens=cfg.max_tokens,
                            response_format={"type": "json_object"})
                        response = raw.parse()
                        usage = response.usage.model_dump() if response.usage else None
                        choice = response.choices[0]
                        receipt = {"usage": usage, "headers": {k: v for k, v in raw.headers.items() if k.lower() in ("inference-id", "x-request-id")},
                            "finish_reason": choice.finish_reason, "model": response.model,
                            "client_elapsed_ms": round((time.perf_counter()-started)*1000, 3),
                            "reasoning_effort": None, "max_tokens": cfg.max_tokens}
                        sample["server_receipts"] = [receipt]
                        if response.model.casefold() != cfg.model_name.casefold():
                            sample["valid"] = False
                            raise ValueError("endpoint_response_model_mismatch")
                        sample["prompt_tokens"] = usage.get("prompt_tokens") if usage else None
                        sample["completion_tokens"] = usage.get("completion_tokens") if usage else None
                        sample["reasoning_tokens"] = ((usage or {}).get("completion_tokens_details") or {}).get("reasoning_tokens")
                        latency = (usage or {}).get("total_latency")
                        sample["server_total_latency_ms"] = latency*1000 if isinstance(latency, (int, float)) else None
                        conv["response"] = choice.message.content
                        if not usage or not isinstance(latency, (int, float)) or not receipt["headers"].get("inference-id"):
                            sample["valid"] = False
                            raise ValueError("missing_server_receipt")
                        if choice.finish_reason == "length":
                            sample["length_stops"] = 1
                            raise ValueError("output_budget_exhausted")
                        generated = json.loads(choice.message.content or "")
                        if set(generated) != {"cypher"} or not isinstance(generated["cypher"], str):
                            raise ValueError("invalid_output_contract")
                        cypher = generated["cypher"].strip()
                        sample["cypher"] = conv["cypher"] = cypher
                        anchor_key = "company_id" if "company_id" in case["params"] else "acct_no"
                        case_policy = replace(policy, required_parameters=("workspace_id", anchor_key), max_result_rows=case["params"]["limit"])
                        violations = list(validator(cypher, params=case["params"], policy=case_policy)) + scope_gate(cypher, case["params"])
                        sample["guardrail_violations"] = violations
                        if violations:
                            raise ValueError("guardrail_rejected:"+stable(violations))
                        explanation = execute(driver, protocol["graph"], cypher, case["params"], False)
                        sample["explain_receipt"] = explanation
                        if plan_gate(explanation, protocol["graph"]["max_leaf_estimated_rows"]):
                            raise ValueError("plan_gate_rejected")
                        execution = execute(driver, protocol["graph"], cypher, case["params"])
                        sample["executions"] = [execution]
                        sample["db_hits"], sample["db_ms"] = execution["db_hits"], execution["elapsed_ms"]
                        sample["rows"] = execution["row_count"]
                        sample["correct"] = score_rows(execution["rows"], case["gold_receipt"]["rows"], execution["columns"], case["result"])
                        sample["score"]["correct"] = sample["correct"]
                        conv["observed_output"] = {"rows": execution["rows"], "columns": execution["columns"],
                            "row_cap": case["params"]["limit"], "completeness": "bounded_contract",
                            "source": "PROFILE/Bolt execution receipt"}
                    except Exception as exc:
                        status = getattr(exc, "status_code", None)
                        body = getattr(exc, "body", None)
                        sample["error"] = f"{type(exc).__name__}: {str(exc)[:1600]}"
                        if status or sample["prompt_tokens"] is None:
                            sample["valid"] = False
                            sample["http_error"] = {"status": status, "body": body, "retry_count": 0}
                            run_status, stop_reason = "aborted_endpoint", "Endpoint failure; no implicit retry"
                        if not sample["valid"]:
                            run_status, stop_reason = "aborted_receipt", sample["error"]
                    sample["wall_ms"] = round((time.perf_counter()-started)*1000, 3)
                    append(output / "conversations.jsonl", conv)
                    append(samples_path, sample)
                    samples.append(sample)
                    completed.add(eid)
                    write(output / "report.json", {**manifest, "run_status": "running" if run_status == "completed" else run_status, "stop_reason": stop_reason,
                        "summary": summarize(samples, protocol["arms"], protocol["decision"]["thresholds"]), "samples": samples})
                    print(f"[episode] {case['id']} r{repeat} {arm} valid={sample['valid']} correct={sample['correct']} error={sample.get('error','')[:150]}", flush=True)
                    if run_status != "completed":
                        break
                    time.sleep(budget["delay_seconds"])
                if run_status != "completed":
                    break
            if run_status != "completed":
                break
        write(output / "report.json", {**manifest, "run_status": run_status, "stop_reason": stop_reason,
            "summary": summarize(samples, protocol["arms"], protocol["decision"]["thresholds"]), "samples": samples})
        print(f"[finished] status={run_status} samples={len(samples)} {stable(summarize(samples,protocol['arms'],protocol['decision']['thresholds']))}", flush=True)
    except Exception as exc:
        write(output / "failure.json", {"phase": "preflight_or_run", "error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        if client:
            client.close()
        driver.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=ROOT / "configs/ontology_mapping_pilot_v1.yaml")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--snapshot-manifest", type=Path, required=True)
    parser.add_argument("--seocho-src", type=Path, required=True)
    parser.add_argument("--seocho-archive", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
