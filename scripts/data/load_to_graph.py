#!/usr/bin/env python3
"""The index set a loaded FinBench graph needs, shared by every loader path.

`bulk_load.py` creates these after `neo4j-admin database import full` returns — the
offline importer builds the store but no indexes, and without them the anchor lookup in
every question (`MATCH (:Account {acct_no:$a, _workspace_id:$ws})`) degrades to a label
scan, which at SF100 is the difference between a millisecond and a full sweep. The
"hub defense" proposal in `bench_seocho_optimizations.py` assumes the `acct_no` index
exists: its whole claim is that an indexed lookup must precede variable-length expansion.

Kept as data, not as Cypher text, so `runmeta`/report code can name the indexes a run had.
"""

from __future__ import annotations

from typing import Any, List, Tuple

# (index name, label, property)
INDEX_SPECS: List[Tuple[str, str, str]] = [
    # The anchor lookup — the one index whose absence changes every plan.
    ("idx_account_acct_no", "Account", "acct_no"),
    ("idx_account_id", "Account", "id"),
    # Workspace scoping is a predicate on every match; without it the scope filter is
    # evaluated per row after expansion.
    ("idx_account_ws", "Account", "_workspace_id"),
    ("idx_person_id", "Person", "id"),
    ("idx_company_id", "Company", "id"),
    ("idx_loan_id", "Loan", "id"),
    ("idx_channel_code", "Channel", "code"),
    ("idx_medium_id", "Medium", "id"),
    # `risk_tier = 5` and `flagged = true` are the two global filters the internal
    # questions open with.
    ("idx_account_risk_tier", "Account", "risk_tier"),
    ("idx_account_flagged", "Account", "flagged"),
]


def create_indexes(session: Any, *, await_seconds: int = 600) -> List[str]:
    """Create every spec'd index if absent and wait for population. Returns names created."""
    created: List[str] = []
    for name, label, prop in INDEX_SPECS:
        session.run(
            f"CREATE INDEX {name} IF NOT EXISTS FOR (n:{label}) ON (n.{prop})"
        ).consume()
        created.append(name)
    session.run(f"CALL db.awaitIndexes({await_seconds})").consume()
    return created
