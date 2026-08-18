"""Structural signature of each question, as chart metadata — the operational axes the
easy/medium/hard label mixes together, made explicit so plots can slice by any of them.

Derived from the reference queries in agent_interaction.py and the label audit in the README
("How hard — the rule, and where the labels depart from it"). The labels themselves are kept
untouched: re-labelling would mean re-running every episode, while a signature costs nothing
and lets a chart average by the thing it is actually claiming.

Fields:
  hops              pattern hops in the reference query (0 = node scan)
  rel_types         relationship types the pattern touches
  variable_length   whether the pattern contains a *1..n expansion
  anchored          starts from an indexed point (the external class) or not
  aggregation       what the reference query computes server-side. Deliberately NOT part of
                    any difficulty axis: it is the one operation whose cost depends on the
                    arm — free in conditions 1-4 where the database does it, the whole
                    burden in conditions 5-7 where the model must.
  filter            the predicate class the pattern carries:
                      anchor-only | rel-property-range | node-property-case |
                      inequality-join | post-aggregation-threshold | none
  hard_kind         which kind of hard this actually is, per the audit:
                      traversal-cost | structural-depth | referential-ambiguity | None
  ambiguous         the question wording admits two readings (ext_med_1's channel_risk
                    placement; int_hard_1's reciprocal-vs-mutual guarantee)
"""
from __future__ import annotations

from typing import Any, Dict

SIGNATURES: Dict[str, Dict[str, Any]] = {
    "ext_easy_1": dict(hops=1, rel_types=["TRANSFER"], variable_length=False, anchored=True,
                       aggregation=["count", "sum"], filter="anchor-only",
                       hard_kind=None, ambiguous=False),
    "ext_easy_2": dict(hops=1, rel_types=["TRANSFER"], variable_length=False, anchored=True,
                       aggregation=["count", "max"], filter="anchor-only",
                       hard_kind=None, ambiguous=False),
    "ext_med_1": dict(hops=1, rel_types=["TRANSFER"], variable_length=False, anchored=True,
                      aggregation=["distinct", "top-5"], filter="rel-property-range",
                      hard_kind="referential-ambiguity", ambiguous=True),
    "ext_med_2": dict(hops=2, rel_types=["TRANSFER", "OWN"], variable_length=False,
                      anchored=True, aggregation=["distinct", "top-5"], filter="anchor-only",
                      hard_kind=None, ambiguous=False),
    "ext_hard_1": dict(hops=1, rel_types=["TRANSFER"], variable_length=True, anchored=True,
                       aggregation=["count-distinct", "max"], filter="anchor-only",
                       hard_kind="traversal-cost", ambiguous=False),
    "ext_hard_2": dict(hops=1, rel_types=["TRANSFER"], variable_length=True, anchored=True,
                       aggregation=["count-distinct"], filter="anchor-only",
                       hard_kind="traversal-cost", ambiguous=False),
    "int_easy_1": dict(hops=0, rel_types=[], variable_length=False, anchored=False,
                       aggregation=["count", "conditional-sum"], filter="node-property-case",
                       hard_kind=None, ambiguous=False),
    "int_easy_2": dict(hops=1, rel_types=["USES_CHANNEL"], variable_length=False,
                       anchored=False, aggregation=["sum", "top-5"], filter="none",
                       hard_kind=None, ambiguous=False),
    "int_med_1": dict(hops=3, rel_types=["OWN", "TRANSFER"], variable_length=False,
                      anchored=False, aggregation=["count-distinct-pairs"],
                      filter="inequality-join", hard_kind=None, ambiguous=False),
    "int_med_2": dict(hops=1, rel_types=["TRANSFER"], variable_length=False, anchored=False,
                      aggregation=["group-count", "top-5"],
                      filter="post-aggregation-threshold", hard_kind=None, ambiguous=False),
    "int_hard_1": dict(hops=6, rel_types=["TRANSFER", "OWN", "GUARANTEE", "SIGN_IN"],
                       variable_length=False, anchored=False,
                       aggregation=["distinct", "top-5"], filter="inequality-join",
                       hard_kind="structural-depth", ambiguous=True),
    "int_hard_1b": dict(hops=6, rel_types=["TRANSFER", "OWN", "GUARANTEE", "SIGN_IN"],
                        variable_length=False, anchored=False,
                        aggregation=["distinct", "top-5"], filter="inequality-join",
                        hard_kind="structural-depth", ambiguous=False),
    "int_hard_2": dict(hops=2, rel_types=["OWN", "TRANSFER"], variable_length=False,
                       anchored=False, aggregation=["group-count-distinct", "top-3"],
                       filter="rel-property-range", hard_kind="structural-depth",
                       ambiguous=False),
}


def attach(episode: Dict[str, Any]) -> Dict[str, Any]:
    """Return the episode with its question's signature merged in under `sig_*` keys."""
    sig = SIGNATURES.get(episode.get("question_id"), {})
    return {**episode, **{f"sig_{k}": v for k, v in sig.items()}}
