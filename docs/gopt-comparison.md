# The optimizer's side of the same problem — GOpt, read against this repo

Lyu, Zhou, Lai, Yang, Lou and Liu, *Enhancing Neo4j Query Efficiency with Seamless
Integration of the GOpt Optimization Framework*, VLDB 2024 workshop (LSGDA). Read
2026-08-17 because condition 4b hands an agent the planner's steering wheel, and this
paper is what happens when a database team takes the wheel instead.

## What the paper says

Three defects in Neo4j's optimizer, and a replacement for it:

1. **No type inference.** Neo4j applies only the type constraints the query states; where
   none is given it assumes every type, and scans candidates the schema already rules out.
2. **A closed, small rule set** — users cannot register their own.
3. **Low-order statistics under an edge-independence assumption**, which mis-estimates
   exactly the multi-hop patterns that matter.

GOpt replaces the optimizer while keeping Neo4j's runtime: a unified graph+relational IR,
type inference from an APOC-extracted schema, a rule set (FilterIntoMatchRule,
FieldTrimRule, ExpandGetVFusionRule, plus a new **TypeFilterRemovalRule**), and a CBO
driven by **high-order statistics** — precomputed motif counts (GLogue) rather than
per-type totals — with per-operator cost factors tuned to Neo4j's actual operators.

Measured on LDBC SNB SF1 (3M nodes, 17M relationships) with the dataset held entirely in
memory: type inference **5× mean, 17× best**; FilterIntoMatchRule **−33% latency**;
TypeFilterRemovalRule **≈2×**; CBO **19× mean on complex patterns, 16× on LSQB**. Neo4j's
own optimizer **runs out of memory** on a 4-clique, a 5-path and one LSQB query, because it
compiles cyclic patterns into nested joins and the intermediate results blow up.

## Reproducing its diagnosis on our graph — `figures/estimate-error.svg`

The plan arm EXPLAINs every query before executing it, so the planner's `EstimatedRows`
and the `DbHits` the same query then spent are both recorded. Over **186 calls**, actual
db hits run from **3×** to **1,067,333×** the estimate, median **383×**. The worst case:
the planner estimates **one row** for an `ext_hard_1` query at SF100 that goes on to touch
**1,067,333 db hits**.

That is the paper's diagnosis, in our data, and it is also the entire justification for a
design decision made before the paper was read: **the plan gate is drawn on the probe's
elapsed time, never on the estimate.** A budget written against this estimator passes
everything or blocks everything.

## Testing its rule on our workload — `scripts/bench_typefilter.py`

TypeFilterRemovalRule, applied by hand from our ontology to the queries the agents
actually settled on at SF100 (`results/bench/typefilter_sf100_*.json`). Every removal is
licensed by the schema — each relationship type here has one target type and one source
type — and only union endpoints written as the full `Person|Company` may lose their label,
because a bare `:Person` on that endpoint is a narrower filter doing real work.

Method notes that changed the numbers: db hits are read from the profile (the 6.x driver
returns plans as dicts); each pair alternates which spelling runs first, because whichever
runs second inherits a page cache the other just warmed; and the original is run twice
against itself before the comparison, so a query that is not row-stable is excluded rather
than credited to the rule.

Five pairs verified identical rows:

| question | db hits | latency |
|---|---|---|
| `int_hard_1` | 12.3M → 4.0M (**−68%**) | **2.52×** |
| `int_hard_1` | 15.8M → 12.8M (−19%) | 1.47× |
| `ext_hard_1` | 23.2M → 17.4M (−25%) | 1.07× |
| `ext_hard_1` | 28.9M → 23.1M (−20%) | 1.04× |
| `int_easy_2` | 24.2M → 28.0M (**+16% worse**) | **0.86× — slower** |

So the rule does land on this graph, and at its best (a four-relationship conjunction) it
beats the paper's headline for that rule. But **one of five verified pairs regressed**, and
that is the finding worth carrying: in GOpt the rule sits inside a cost-based optimizer
that can decline to apply it. Applied blindly, as a rewriting habit or a prompt
instruction, it costs 3.8M extra db hits on the one query where the label was serving as
an anchor. *A rule is not an optimization until something decides when to use it.*

A sixth pair (`int_hard_2`) returned different rows and was excluded. Adjudicated by hand:
its `ORDER BY account_count DESC LIMIT` has **8,099 owners tied** at the boundary value, so
its top-N is plan-dependent and both answers are correct for the query as written. The
rule is not at fault — the query has no total order. (Our own question set is shaped as
top-N under a total order for exactly this reason; the model's query was not.)

## What it means for the talk

- **The ontology is either in the prompt or in the planner.** GOpt derives type
  constraints inside the optimizer; our condition 2 puts the same schema in the prompt so
  the model writes better-typed Cypher (`ext_med_1`: 1/9 → 9/9). One source of truth, two
  places to spend it, and they are not exclusive.
- **Condition 4b is a third placement — the agent as optimizer.** Hints bought
  96 s → 26 s at SF1000 and 3.8M → 722k median db hits at SF100, on the anchored questions
  where an index seek is the obvious steer. The paper's 19× comes from the cyclic patterns
  the agent never touched. Read together: an LLM steering an existing planner captures the
  easy half; the hard half is a database-engineering problem.
- **Our unanswerable question is the paper's home turf.** `int_hard_1` — a cyclic
  conjunction of TRANSFER, reciprocal GUARANTEE and a shared SIGN_IN device — is where our
  agent burned eight round trips, overrode the gate, timed out at 60 s and returned
  nothing. The paper reports Neo4j OOM-ing on that query class and GOpt fixing it.
- **Caveat when quoting speedups side by side**: their SF1 dataset is held entirely in
  memory on a 512 GB machine; ours runs at SF100–SF1000 against a **512 MB page cache**
  (recorded in every manifest since 2026-08-17). Their numbers are CPU-bound, ours are
  IO-bound. The directions agree; the magnitudes are not transferable.
