#!/usr/bin/env python3
"""Does the installed seocho still run this experiment? No database, no model — just the
surface agent_interaction.py actually imports, exercised the way main_async() exercises it.

Run it after installing seocho — the pinned tag from requirements.txt, or any newer one —
before spending an episode budget on an environment that would have failed at import:

    python scripts/smoke_seocho.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ONTOLOGY = Path(__file__).resolve().parent.parent / "ontology" / "finbench.ontology.yaml"

# A query shaped exactly as the experiment's rules demand: workspace scope on every node
# pattern, anchor bound to $a, LIMIT $limit. The guardrail must wave it through.
GOOD = (
    "MATCH (a:Account {_workspace_id: $workspace_id, acct_no: $a})"
    "<-[t:TRANSFER]-(b:Account {_workspace_id: $workspace_id}) "
    "RETURN b.acct_no, t.amount LIMIT $limit"
)

# The same query minus the tenant scope and against an undeclared label. The guardrail must
# refuse it — a smoke test that only checks the happy path would pass against a seocho whose
# validator had become a no-op.
BAD = "MATCH (a:Wallet)-[t:TRANSFER]->(b:Account) RETURN b LIMIT $limit"

PARAMS = {"workspace_id": "ws_smoke", "ws": "ws_smoke", "limit": 50, "a": 1, "acct_no": 1}


def main() -> int:
    from seocho.ontology import Ontology
    from seocho.query.hybrid_planner import policy_from_ontology, schema_for_prompt
    from seocho.query.workload_compiler import validate_text2cypher_fallback

    ontology = Ontology.from_dict(yaml.safe_load(ONTOLOGY.read_text()))
    policy = policy_from_ontology(ontology)
    schema = schema_for_prompt(ontology, policy)
    # Searched as the model sees it: build_instructions() renders the schema dict with
    # json.dumps, so string containment against that rendering is the right test.
    schema_text = json.dumps(schema, indent=2, default=str)

    failures = []

    # The two ontology facts findings 1 and 2 rest on, as rendered into the prompt schema.
    for needle in ("GUARANTEE", "channel_risk", "amount"):
        if needle not in schema_text:
            failures.append(f"schema_for_prompt lost {needle!r}")

    good_violations = list(validate_text2cypher_fallback(GOOD, params=PARAMS, policy=policy))
    if good_violations:
        failures.append(f"guardrail refuses a conforming query: {good_violations}")

    bad_violations = list(validate_text2cypher_fallback(BAD, params=PARAMS, policy=policy))
    if not bad_violations:
        failures.append("guardrail passed a query with no workspace scope and an "
                        "undeclared label — the validator is a no-op")

    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if not failures:
        print(f"ok: schema {len(schema_text)} chars, guardrail accepts GOOD, "
              f"refuses BAD with {len(bad_violations)} violation(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
