# FIBO-anchored text2cypher: what the H200 run demands from the next design

2026-08-22. This is the local design step after the H200 e2e (commit ee53862), and it starts
with a correction to that commit's own explanation. The commit blamed the grammar arm's 7/26
mostly on subset inexpressibility. Measured attribution says otherwise:

    grammar-arm failures (19 episodes):
      6   outside the subset            (int_easy_1 CASE, int_med_2/int_hard_2 WITH — 3 questions x 2)
      13  INSIDE the subset, executed, wrong answer

    structural coverage of the 13 gold queries (covers(), LIMIT artifact removed):
      10/13 inside — WITH/CASE excludes only 3

So the subset is not the dominant term. Reading the settled Cypher of the inside-but-wrong
episodes against the no-grammar arm's on the same questions gives the real defect list:

| # | defect | evidence | design consequence |
|---|--------|----------|--------------------|
| 1 | **Union labels are inexpressible.** `label ::= <single>` cannot say `Person\|Company`, and the ontology *declares* endpoints that way. On int_med_1 the constrained model, unable to write the union, gave up into a trivial `RETURN a.acct_no` sample query. | int_med_1 | grammar's `label` rule must admit the unions the ontology itself declares — it is ontology-derived and skipped half the ontology's own syntax |
| 2 | **Comma-separated multi-patterns are inexpressible** (`MATCH (o)-[:OWN]->(a1), (o)-[:OWN]->(a2)`). The winning no-grammar query needed it. | int_med_1 | add bounded pattern lists to `match_clause` |
| 3 | **Bare-variable comparisons are admissible nonsense.** `ref ::= var ("." prop)?` admits `reach <> $acct_no` — a node compared to an integer. Executed, scored wrong. | ext_hard_1 | predicates compare `var "." prop` only; node identity goes through properties |
| 4 | **The params contract is the real ceiling.** Threshold questions (channel_risk >= X) need a bound parameter; the harness binds only `workspace_id/limit/acct_no`, and the policy forbids literals. Under grammar those questions are *provably* unwritable; without grammar the model inlines a literal and the validator rejects it. Same wall, different error. | ext_med_1, risky_senders class | **questions and bound params must be co-designed**: every threshold a question names ships as a parameter. This is a harness/ontology contract, not a grammar rule |
| 5 | Subset gaps proper (WITH pipelines, CASE). | 3 questions | widen deliberately with bounds, or route: these questions bypass the grammar |

Points 1-3 are grammar bugs fixable and membership-testable locally today. Point 4 is the
FIBO suite's design constraint. Point 5 is the routing decision the plan below measures.

## Why FIBO is the right vehicle

`ontology/fibo_finbench.ontology.yaml` already exists (FIBO FBC/BP-aligned: Account,
NaturalPerson, LegalEntity, LoanFacility, PaymentRail, LoginMedium; BENEFICIAL_OWNER_OF,
CONTROLS_ENTITY, SUBSIDIARY_OF, GUARANTEES_PARTY, ...), and `bench_fibo_robustness.py` defines
the three AML families the talk cares about: multi-tier UBO shell traversal, correspondent-rail
structuring/smurfing, loan-guarantee layering cycles. Those are exactly the WITH-pipeline
questions — the ones the current subset cannot express — so a FIBO suite is simultaneously the
stress test for point 5 and the realistic workload the routing decision needs.

## The plan, in order, all local until step 5

1. **Fix grammar defects 1-3**; regression = membership on every settled query both H200 arms
   produced (recorded in the two e2e jsons).
2. **Co-design the FIBO question suite with its params** (point 4): 12-15 questions across the
   three FIBO families + the anchored basics, each with gold Cypher, bound params for every
   threshold it names, and a `covers()` label. Target mix: ~60% inside-subset, ~40% outside,
   because that ratio is what makes routing measurable rather than assumed.
3. **Ontology conformance for FIBO**: extend `ontology_conformance.py` to the FIBO ontology's
   projection of the same graph (its relationship names differ from the store's — that mapping
   must be explicit or text2cypher against FIBO names returns empty).
4. **Route-by-coverage on MARA** (free): gold-labeled routing — subset questions get the
   grammar *prompt contract* (MARA can't enforce, but validation is identical), outside
   questions get the unconstrained path. Measures the routing win/loss on answer quality
   without a GPU.
5. **One GPU run** (pre-flight per [[gpu-run-wiring-lessons]]): the same suite with real
   constrained decoding, grammar-routed vs all-grammar vs no-grammar. This is the run that
   decides whether the seocho_native gap closes.

## What is already settled and does not need re-measuring

ontology arm 22/26 at 7.2 s/episode is the bar; seocho execution machinery costs 2.4 ms/call
(not the problem); prefix caching pays 78-89%; the engine/runtime is a dead end for our shapes;
the GIL fix (processes) and server-side aggregation are the graph-side levers.
