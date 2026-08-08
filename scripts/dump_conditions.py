"""Write the seven experimental conditions out as documentation, from the code that runs them.

Generated rather than hand-written. A prose description of a prompt drifts from the prompt the
moment either changes, and the whole comparison rests on the conditions differing in exactly
the ways claimed — so the document has to be derived from `agent_interaction.py`, not written
alongside it.

Produces two things:

  ``docs/conditions.md``      the full instruction text of every condition, the exact diff
                              between each condition and the one it builds on, and what the
                              tool enforces and returns in each.
  ``figures/conditions.svg``  the same as a matrix: rows are the components a condition can
                              carry, columns are the conditions.

Usage:
  python scripts/dump_conditions.py --ontology ontology/finbench.ontology.yaml
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from agent_interaction import (
    ARMS,
    IN_CONTEXT_MAX_TURNS,
    IN_CONTEXT_ROW_CAP,
    MAX_TURNS,
    ROW_CAP,
    build_instructions,
    labels_only_schema,
)

# Which condition each one is a modification of. The first four are cumulative, so their diff
# against the previous row is the whole of what they add. The in-context pair is not part of
# that chain — it changes who does the arithmetic — and it is diffed against `ontology`, not
# `guardrail`, because conditions 5 and 6 carry the ontology schema but do *not* run the
# guardrail before execution. The matrix figure had this right while an earlier draft of this
# table did not, which is the argument for generating both from one source.
BUILDS_ON = {
    "labels": None,
    "ontology": "labels",
    "guardrail": "ontology",
    "plan": "guardrail",
    "in_context": "ontology",
    "in_context_blind": "in_context",
    "in_context_csv": "in_context",
}

TITLE = {
    "labels": "1 · labels only",
    "ontology": "2 · + ontology",
    "guardrail": "3 · + guardrail",
    "plan": "4 · + plan feedback",
    "in_context": "5 · in-context aggregation",
    "in_context_blind": "6 · in-context, blind (control)",
    "in_context_csv": "7 · in-context, CSV rows",
}

SUMMARY = {
    "labels": "Label and relationship names only. Plain text2cypher, and the baseline every "
              "other condition is measured against.",
    "ontology": "The same, plus what a list of labels cannot say: which end of a same-label "
                "relationship the anchor sits on, which types each relationship actually "
                "connects, which properties exist and where they live, and how heavy the "
                "degree tail is.",
    "guardrail": "The ontology is now *enforced*. Generated Cypher is checked against it "
                 "before the query reaches the database; a violation comes back to the model "
                 "as text, so a rejection becomes a repair rather than a failure.",
    "plan": "The query runs under a two-second probe first. One that does not finish comes "
            "back with its plan operators and an instruction to start from an index — with an "
            "`/* accept-cost */` override, because an investigator's questions are unanchored "
            "by nature and a gate with no escape hatch is a policy against asking them.",
    "in_context": "The arithmetic moves out of the database. Aggregate functions are refused "
                  "in Cypher, so the agent must fetch rows and compute the answer itself. The "
                  "tool caps each page and *reports* when more rows existed.",
    "in_context_blind": "The control for condition 5, withholding one thing: the tool does not "
                        "say whether more rows existed, and the instructions never mention "
                        "that a result can be bounded. Everything else is identical.",
    "in_context_csv": "Condition 5 with one thing changed: the rows come back as CSV instead "
                      "of JSON. Same fields, same cap, same `more_available` — but the column "
                      "names are paid for once in a header line rather than once per row, "
                      "which on 200 rows of this graph's shape is 58% of the JSON payload's "
                      "tokens. The question is whether the cheaper encoding costs anything in "
                      "in-context arithmetic or truncation disclosure.",
}

# Rows of the matrix figure. Each is a component a condition may or may not carry; the values
# come from the same source as the prompts, so a change to one moves both.
COMPONENTS: List[Tuple[str, Dict[str, bool]]] = [
    ("Label and relationship names",
     {a: True for a in ARMS}),
    ("Relationship endpoint types",
     {a: a != "labels" for a in ARMS}),
    ("Direction as named roles (sender → beneficiary)",
     {a: a != "labels" for a in ARMS}),
    ("Declared relationship properties",
     {a: a != "labels" for a in ARMS}),
    ("Measured degree hint (heavy-tailed, max 77k)",
     {a: a != "labels" for a in ARMS}),
    ("Tenant scope convention",
     {a: a != "labels" for a in ARMS}),
    ("Ontology enforced before execution",
     {a: a in ("guardrail", "plan") for a in ARMS}),
    ("Execution plan returned on a slow query",
     {a: a == "plan" for a in ARMS}),
    ("Aggregate functions refused in Cypher",
     {a: a.startswith("in_context") for a in ARMS}),
    ("Tool reports `more_available`",
     {a: a in ("in_context", "in_context_csv") for a in ARMS}),
    ("Told to say so when the view is bounded",
     {a: a in ("in_context", "in_context_csv") for a in ARMS}),
    ("Rows returned as CSV instead of JSON",
     {a: a == "in_context_csv" for a in ARMS}),
]

COLOR = {"labels": "#c2410c", "ontology": "#ca8a04", "guardrail": "#0e7490",
         "plan": "#15803d", "in_context": "#6d28d9", "in_context_blind": "#9f7aea",
         "in_context_csv": "#be185d"}
SHORT = {"labels": "1\nlabels\nonly", "ontology": "2\n+ ontology", "guardrail": "3\n+ guardrail",
         "plan": "4\n+ plan\nfeedback", "in_context": "5\nin-context",
         "in_context_blind": "6\nin-context\nblind", "in_context_csv": "7\nin-context\nCSV"}


def prompts(ontology_path: Path) -> Dict[str, str]:
    import yaml

    from seocho.ontology import Ontology
    from seocho.query.hybrid_planner import policy_from_ontology, schema_for_prompt

    ontology = Ontology.from_dict(yaml.safe_load(ontology_path.read_text()))
    policy = policy_from_ontology(ontology)
    thin, full = labels_only_schema(ontology), schema_for_prompt(ontology, policy)
    return {a: build_instructions(thin if a == "labels" else full, arm=a) for a in ARMS}


def rules_of(text: str) -> str:
    """The rules block only. The schema block is large and identical across five of six
    conditions, so diffing whole prompts would bury the differences that matter."""
    return "Rules:" + text.split("Rules:", 1)[1] if "Rules:" in text else text


def write_markdown(texts: Dict[str, str], out: Path) -> None:
    lines: List[str] = [
        "# The seven conditions, and exactly how their prompts differ",
        "",
        "Generated by `scripts/dump_conditions.py` from `agent_interaction.py`. Do not edit by "
        "hand — the point of generating it is that a description of a prompt cannot drift from "
        "the prompt.",
        "",
        "Conditions 1–4 are cumulative: each adds one thing to the previous, so the measured "
        "difference between two adjacent conditions is attributable to that one thing. "
        "Conditions 5–7 are not part of that chain. They move the arithmetic out of the "
        "database, and 6 and 7 each vary condition 5 along one axis: 6 withholds the "
        "truncation signal, 7 changes the row encoding from JSON to CSV.",
        "",
        "| | condition | schema | rules | row cap | turn budget |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for a in ARMS:
        schema_chars = len(texts[a]) - len(rules_of(texts[a]))
        cap = IN_CONTEXT_ROW_CAP if a.startswith("in_context") else ROW_CAP
        turns = IN_CONTEXT_MAX_TURNS if a.startswith("in_context") else MAX_TURNS
        lines.append(f"| {TITLE[a].split(' · ')[0]} | `{a}` | {schema_chars:,} chars "
                     f"| {len(rules_of(texts[a])):,} chars | {cap} | {turns} |")
    lines += ["", "_The matrix figure regenerates alongside this file:_ "
                  "`python scripts/dump_conditions.py`", ""]

    for a in ARMS:
        lines += ["---", "", f"## {TITLE[a]}", "", SUMMARY[a], ""]
        base = BUILDS_ON[a]
        if base is None:
            lines += ["This is the baseline. Its full rules block:", "",
                      "```", rules_of(texts[a]).strip(), "```", ""]
            continue
        diff = [l for l in difflib.unified_diff(
            rules_of(texts[base]).splitlines(), rules_of(texts[a]).splitlines(),
            lineterm="", n=0) if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
        lines.append(f"**Rules added over `{base}`:**")
        lines.append("")
        if diff:
            lines += ["```diff", *diff, "```", ""]
        else:
            lines += ["_None — the rules are identical._", ""]
        if a == "ontology":
            lines += [
                "The rules text does not change at all here. Everything condition 2 adds is in "
                "the **schema block**, which grows from 778 to 2,311 characters: endpoint "
                "types, a `__direction__` line per relationship naming its roles, a "
                "`__cardinality__` line carrying the measured degree tail, and the tenant "
                "scope convention. That is the entire intervention, and it is what takes "
                "`ext_med_1` from 1/9 to 9/9.", ""]
        if a == "guardrail":
            lines += [
                "Also not in the prompt: this condition adds enforcement *outside* it. Before "
                "any query executes, the generated Cypher is checked against the ontology — "
                "declared labels, relationship types, properties, bounded paths, tenant scope "
                "and a row budget. A violation is returned to the model as text and it "
                "rewrites.", ""]
        if a == "plan":
            lines += [
                "The gate is on **measured elapsed time**, not on the planner's row estimate. "
                "That was the first design and it fired zero times in 108 episodes: across the "
                "48 queries settled on at SF100, actual db hits ran from 2.9× to 4,617,254× "
                "the summed `EstimatedRows`, because that figure is per-operator output "
                "cardinality and the work is in the expansion.", ""]
        if a == "in_context_blind":
            lines += [
                "**The tool response also differs, in one field.** Condition 5 returns "
                "`{rows, row_count, row_cap, more_available}`; condition 6 returns "
                "`{rows, row_count}`. Nothing else about the two conditions differs — same row "
                "cap, same turn budget, same aggregate ban, same schema, same model, same "
                "temperature.", "",
                "The pair separates two explanations that were previously conflated: *the "
                "model does not disclose that its answer rests on a truncated view*, and *the "
                "model was never told the view was truncated*. Those have different fixes — "
                "one is a prompt or model problem, the other is one line of a tool response "
                "schema.", ""]
        if a == "in_context_csv":
            lines += [
                "**The tool response differs in encoding, not in content.** Condition 5 "
                "returns `{rows, row_count, row_cap, more_available}` as JSON; condition 7 "
                "returns the same rows as a CSV header line plus one line per row, and the "
                "same metadata as a trailing `# row_count=… row_cap=… more_available=…` "
                "line. Measured on 200 rows of this graph's shape with the o200k tokenizer: "
                "9,017 tokens as JSON, 5,211 as CSV (58%), because JSON pays for the column "
                "names once per row and CSV pays once per page.", ""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    print(f"wrote {out}")


def write_figure(out: Path) -> None:
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9,
                         "figure.facecolor": "white", "savefig.facecolor": "white"})
    nrows, ncols = len(COMPONENTS), len(ARMS)
    fig, ax = plt.subplots(figsize=(10.6, 0.44 * nrows + 2.7))
    fig.subplots_adjust(left=0.365, right=0.985, top=0.815, bottom=0.10)

    for r, (label, present) in enumerate(COMPONENTS):
        y = nrows - 1 - r
        ax.add_patch(plt.Rectangle((-0.5, y - 0.5), ncols, 1,
                                   facecolor="#f7f8fa" if r % 2 else "white",
                                   edgecolor="none", zorder=0))
        for c, a in enumerate(ARMS):
            if present[a]:
                ax.add_patch(plt.Rectangle((c - 0.34, y - 0.30), 0.68, 0.60,
                                           facecolor=COLOR[a], edgecolor="none",
                                           zorder=2, alpha=0.92))
        ax.text(-0.62, y, label, ha="right", va="center", fontsize=8.6, color="#2b3138")

    # The break between the cumulative chain and the pair that changes who aggregates.
    ax.axvline(3.5, color="#c9ced6", linewidth=1.2, linestyle=(0, (4, 3)), zorder=3)
    ax.text(1.5, nrows - 0.32, "cumulative — each adds one thing",
            ha="center", va="bottom", fontsize=8.6, color="#6b7684")
    ax.text(4.5, nrows - 0.32, "the agent does the arithmetic",
            ha="center", va="bottom", fontsize=8.6, color="#6b7684")

    ax.set_xlim(-0.5, ncols - 0.5)
    ax.set_ylim(-0.5, nrows - 0.5 + 0.55)
    ax.set_xticks(range(ncols))
    ax.set_xticklabels([SHORT[a] for a in ARMS], fontsize=8.4)
    for tick, a in zip(ax.get_xticklabels(), ARMS):
        tick.set_color(COLOR[a])
    ax.set_yticks([])
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0)

    fig.suptitle("What each condition gives the agent", fontsize=13, weight="bold",
                 x=0.028, ha="left", y=0.965, color="#12151a")
    fig.text(0.028, 0.905,
             "Filled = present. Conditions 1–4 are cumulative, so the measured difference "
             "between\nadjacent columns is attributable to the one row that changed. 6 and 7 "
             "each vary 5 by one\nrow: 6 withholds `more_available`, 7 changes the row "
             "encoding to CSV.",
             fontsize=8.4, color="#6b7684", ha="left", va="top")
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, format="svg")
    plt.close(fig)
    print(f"wrote {out}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ontology", default="ontology/finbench.ontology.yaml")
    p.add_argument("--markdown", default="docs/conditions.md")
    p.add_argument("--figure", default="figures/conditions.svg")
    args = p.parse_args()
    texts = prompts(Path(args.ontology))
    write_markdown(texts, Path(args.markdown))
    write_figure(Path(args.figure))


if __name__ == "__main__":
    main()
